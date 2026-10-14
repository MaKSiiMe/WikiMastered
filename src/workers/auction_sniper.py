"""Module D : sniper d'enchères (plafond par rareté + écart de cote) et arbitrage nocturne.

- Scrute /api/marketplace?sort=ending_soon toutes les SNIPER_POLL_MIN..MAX secondes.
- Mise toujours le minimum natif du site : prix de départ, puis max(ceil(1,1 × mise), mise + 1)
  [WR market-api.js]. Le plafond est la mise finale maximale acceptée sur une enchère.
- Le site prolonge de 60 s une enchère disputée dans les 10 dernières secondes
  [WR TECHNIQUE.md] : le bot ré-évalue donc à chaque passage, sans dépasser le plafond.
- Une mise au résultat incertain n'est jamais rejouée.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from src.config import Settings
from src.core.client import RateLimited, UncertainMutation
from src.core.database import utcnow
from src.core.models import Auction, BidResult
from src.core.pricing import Quote
from src.core.text import normalize_title
from src.core.timeutils import in_window, local_dt, now_local
from src.workers.base import Worker


@dataclass(frozen=True)
class SnipeParams:
    window_seconds: int
    rarity_caps: dict[str, int]
    shiny_cap: int
    deal_ratio: float
    max_bid: int
    budget: int
    night: bool


def snipe_params(s: Settings, night: bool) -> SnipeParams:
    mult = s.night_caps_multiplier if night else 1.0
    return SnipeParams(
        window_seconds=s.night_window_seconds if night else s.sniper_window_seconds,
        rarity_caps={r: int(v * mult) for r, v in s.sniper_rarity_caps.items()},
        shiny_cap=int(s.sniper_shiny_cap * mult),
        deal_ratio=s.night_deal_ratio_max if night else s.sniper_deal_ratio_max,
        max_bid=s.sniper_max_bid,
        budget=s.sniper_daily_budget + (s.night_budget_bonus if night else 0),
        night=night,
    )


@dataclass(frozen=True)
class Decision:
    amount: int
    ceiling: int
    reason: str
    reference: float | None


def rarity_ceiling(auction: Auction, params: SnipeParams) -> int | None:
    caps = []
    if auction.rarity in params.rarity_caps:
        caps.append(params.rarity_caps[auction.rarity])
    if auction.shiny and params.shiny_cap > 0:
        caps.append(params.shiny_cap)
    return max(caps) if caps else None


def decide(auction: Auction, params: SnipeParams, quote: Quote | None, min_sales: int) -> Decision | None:
    """Plafond par rareté OU cote : on retient la règle qui autorise la mise la plus haute."""
    amount = auction.min_bid
    options: list[tuple[int, str]] = []
    cap = rarity_ceiling(auction, params)
    if cap is not None and amount <= cap:
        options.append((cap, f"plafond {auction.rarity}{' shiny' if auction.shiny else ''} ≤ {cap}"))
    if quote is not None and quote.reliable(min_sales):
        deal_limit = math.floor(quote.average * params.deal_ratio)  # type: ignore[operator]
        if amount <= deal_limit:
            options.append((deal_limit, f"deal {amount} ≤ {params.deal_ratio:.0%} de la cote {quote.average:.0f}"))
    if not options:
        return None
    ceiling, reason = max(options)
    ceiling = min(ceiling, params.max_bid)
    if amount > ceiling:
        return None
    return Decision(amount, ceiling, reason, quote.average if quote else None)


def in_snipe_window(auction: Auction, me: str | None, window: int, now: datetime) -> bool:
    left = auction.seconds_left(now)
    return (
        auction.status == "active"
        and 0 < left <= window
        and (me is None or ((auction.seller_id or "").lower() != me and (auction.current_bidder_id or "").lower() != me))
    )


class AuctionSniper(Worker):
    name = "sniper"
    alert_after_errors = 20

    async def setup(self) -> None:
        self._balance: int | None = None
        self._balance_at = utcnow()
        self._ticks = 0
        self._budget_warned_day: str | None = None
        # Mises en tête connues : auction_id -> (bid_id, montant)
        self._leading: dict[str, tuple[int, int]] = {}
        for row in await self.db.placed_bids():
            self._leading[row["auction_id"]] = (row["id"], row["amount"])
        # Enchères engagées (le site en limite le nombre) : auction_id -> fin.
        # Réelles (lues sur le serveur) + celles du bot, y compris simulées en DRY_RUN.
        self._engaged: dict[str, datetime] = {}
        self._digest: list[str] = []
        self._next_digest = utcnow() + timedelta(minutes=self.settings.notify_digest_minutes)
        await self._refresh_engaged()
        if not self.api.user_id:
            self.notifier.warning("Sniper : identifiant joueur inconnu",
                                  "Renseigne WM_USER_ID pour ne jamais enchérir sur tes propres ventes.")

    async def step(self) -> float:
        self._ticks += 1
        now = utcnow()
        night = in_window(now_local(), self.settings.night_start, self.settings.night_end)
        params = snipe_params(self.settings, night)
        me = self.api.user_id

        auctions, _ = await self.api.market_page(sort="ending_soon")
        if self._ticks % 5 == 1:
            await self._remember(auctions, now)
        await self._track_outbids(auctions, me)
        if self._ticks % 20 == 0:
            await self._refresh_engaged()

        # Toutes les opportunités du passage sont évaluées, puis seules les meilleures
        # sont jouées dans la limite des enchères simultanées.
        candidates: list[tuple[float, Auction, Decision]] = []
        for auction in auctions:
            if in_snipe_window(auction, me, params.window_seconds, now):
                decision = await self._evaluate(auction, params)
                if decision:
                    margin = (decision.reference or decision.ceiling) - decision.amount
                    candidates.append((margin, auction, decision))
        candidates.sort(key=lambda c: c[0], reverse=True)
        free = self.free_slots(now)
        for _, auction, decision in candidates:
            rebid = auction.id in self._engaged
            if not rebid and free <= 0:
                self.log.debug("plus de place pour %s (%d enchères en cours)", auction.title, len(self._engaged))
                continue
            if not await self._affordable(decision.amount, params):
                continue
            if await self._bid(auction, decision, params) and not rebid:
                free -= 1

        if self._ticks % 10 == 0:
            await self._settle()
        self._flush_digest(now)
        return random.uniform(self.settings.sniper_poll_min_seconds, self.settings.sniper_poll_max_seconds)

    def free_slots(self, now: datetime) -> int:
        grace = timedelta(seconds=self.settings.sniper_settle_grace_seconds)
        self._engaged = {aid: end for aid, end in self._engaged.items() if end + grace > now}
        return max(0, self.settings.sniper_max_concurrent_bids - len(self._engaged))

    async def _refresh_engaged(self) -> None:
        """Ajoute les enchères en cours vues par le serveur (y compris tes mises manuelles)."""
        for a in await self.api.my_bids():
            if a.status == "active":
                self._engaged[a.id] = a.end_at

    def _flush_digest(self, now: datetime) -> None:
        if now < self._next_digest:
            return
        self._next_digest = now + timedelta(minutes=self.settings.notify_digest_minutes)
        if not self._digest:
            return
        lines, self._digest = self._digest, []
        shown = "\n".join(lines[-15:])
        more = f"\n… et {len(lines) - 15} autre(s)" if len(lines) > 15 else ""
        title = "Sniper : enchères simulées" if self.settings.dry_run else "Sniper : enchères placées"
        self.notifier.info(f"{title} ({len(lines)})", shown + more)

    async def _remember(self, auctions: list[Auction], now: datetime) -> None:
        await self.db.upsert_auctions([{
            "auction_id": a.id, "card_id": a.catalog_id, "title": a.title, "norm_title": normalize_title(a.title),
            "rarity": a.rarity, "is_shiny": int(a.shiny), "current_bid": a.current_bid,
            "base_amount": a.base_amount, "min_bid": a.min_bid, "end_at": a.end_at.isoformat(),
            "seen_at": now.isoformat(),
        } for a in auctions])

    async def _track_outbids(self, auctions: list[Auction], me: str | None) -> None:
        for a in auctions:
            mine = self._leading.get(a.id)
            if mine and me and (a.current_bidder_id or "").lower() != me:
                await self.db.update_bid(mine[0], status="outbid")
                del self._leading[a.id]
                self.log.info("surenchéri sur %s (%s), mise %d remboursée", a.title, a.rarity, mine[1])

    async def _evaluate(self, auction: Auction, params: SnipeParams) -> Decision | None:
        last = await self.db.last_bid(auction.id)
        if last is not None:
            if last["status"] in ("uncertain", "simulated"):
                return None
            if last["status"] == "rejected" and last["amount"] >= auction.min_bid:
                return None
            if last["status"] in ("placed", "outbid") and not self.settings.sniper_allow_rebid:
                return None

        # La cote n'est demandée que si le plafond de rareté ne suffit pas : la route des
        # cotes est limitée par le site, et le sniper ne doit jamais attendre qu'elle se libère.
        cap = rarity_ceiling(auction, params)
        quote = None
        if cap is None or auction.min_bid > cap:
            try:
                quote = await self.ctx.prices.quote(auction.catalog_id, auction.rarity, wait=False)
            except RateLimited:
                quote = None
        return decide(auction, params, quote, self.settings.price_min_sales)

    async def _affordable(self, amount: int, params: SnipeParams) -> bool:
        if self.settings.dry_run:
            return True
        today = now_local().date()
        committed = await self.db.committed_since(local_dt(today, time(0, 0)))
        if committed + amount > params.budget:
            if self._budget_warned_day != str(today):
                self._budget_warned_day = str(today)
                self.notifier.info("Sniper : budget du jour atteint", f"{committed}/{params.budget} WikiBidous engagés.")
            return False
        if self._balance is None or utcnow() - self._balance_at > timedelta(seconds=60):
            self._balance, self._balance_at = await self.api.balance(), utcnow()
        return self._balance - amount >= self.settings.sniper_min_balance_reserve

    async def _bid(self, auction: Auction, decision: Decision, params: SnipeParams, *, retried: bool = False) -> bool:
        """Place la mise ; renvoie True si elle est acceptée (ou simulée)."""
        common = dict(
            auction_id=auction.id, card_id=auction.catalog_id, title=auction.title, rarity=auction.rarity,
            is_shiny=auction.shiny, amount=decision.amount, reference_price=decision.reference,
            reason=decision.reason, night=params.night, dry_run=self.settings.dry_run, end_at=auction.end_at,
        )
        label = f"{auction.title} ({auction.rarity}{' ✨' if auction.shiny else ''})"
        try:
            result = await self.api.place_bid(auction.id, decision.amount)
        except UncertainMutation:
            await self.db.record_bid(**common, status="uncertain", note="réponse perdue")
            self._engaged[auction.id] = auction.end_at  # la mise a peut-être abouti : place occupée
            raise

        summary = f"{label} : {decision.amount} WB ({decision.reason}{', nuit' if params.night else ''})"
        if result.dry_run:
            await self.db.record_bid(**common, status="simulated")
            self._engaged[auction.id] = auction.end_at
            self._report_bid("Enchère simulée", summary)
            return True

        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.ok:
            try:
                confirmed = BidResult.model_validate(payload)
            except ValueError:
                confirmed = None
            if confirmed is None or confirmed.current_bid != decision.amount:
                await self.db.record_bid(**common, status="uncertain", note=str(payload)[:300])
                self.notifier.critical("Enchère non confirmée", f"{label} — vérifie sur le site.")
                return False
            await self.db.mark_outbid(auction.id)
            bid_id = await self.db.record_bid(**common, status="placed")
            self._leading[auction.id] = (bid_id, decision.amount)
            self._balance, self._balance_at = confirmed.bidder_balance, utcnow()
            self._engaged[auction.id] = auction.end_at
            self._report_bid("Enchère placée", f"{summary}, solde {confirmed.bidder_balance}")
            return True

        server_min = payload.get("min")
        error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
        await self.db.record_bid(**common, status="rejected", note=str(error)[:300])
        self.log.info("mise refusée sur %s : %s", label, error)
        if not retried and isinstance(server_min, int) and decision.amount < server_min <= decision.ceiling:
            return await self._bid(auction, Decision(server_min, decision.ceiling, decision.reason,
                                                     decision.reference), params, retried=True)
        return False

    def _report_bid(self, title: str, summary: str) -> None:
        self.log.info("%s : %s", title, summary)
        if self.settings.notify_each_bid:
            self.notifier.info(title, summary)
        else:
            self._digest.append(summary)

    async def _settle(self) -> None:
        cutoff = utcnow() - timedelta(seconds=self.settings.sniper_settle_grace_seconds)
        me = self.api.user_id
        for row in await self.db.bids_to_settle(cutoff, limit=2):
            auction = await self.api.auction(row["auction_id"])
            if auction.status == "active":
                await self.db.update_bid(row["id"], end_at=auction.end_at)  # prolongée
                continue
            winner = (auction.winner_id or auction.current_bidder_id or "").lower()
            won = auction.status == "settled_sold" and me is not None and winner == me
            await self.db.update_bid(row["id"], status="won" if won else "lost", settled_at=utcnow())
            self._leading.pop(row["auction_id"], None)
            if won:
                ref = f" (cote {row['reference_price']:.0f})" if row["reference_price"] else ""
                self.notifier.success("Snipe réussi !", f"{row['title']} ({row['rarity']}) pour {row['amount']} WB{ref}",
                                      arbitrage="oui" if row["night"] else "non")
