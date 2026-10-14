"""Module D : sniper d'enchères orienté capital, et arbitrage nocturne.

Le site limite à 5 ventes simultanées (les mises sont illimitées) : chaque carte achetée doit
attendre une de ces 5 places pour être revendue, donc le bot achète peu, mais gros.
- Une carte n'est achetée que pour être revendue (une « position ») : mise ≤ budget d'une
  position ≈ capital × PORTFOLIO_CAPITAL_RATIO / PORTFOLIO_SLOTS, et bénéfice attendu
  (estimation de revente − mise) ≥ max(PORTFOLIO_MIN_PROFIT, budget × PORTFOLIO_MIN_PROFIT_RATIO).
- Estimation = médiane des dernières ventes réelles de la carte (voir core/pricing.py).
- Au plus PORTFOLIO_MAX_POSITIONS positions détenues + enchères du bot en cours.
- Mise toujours le minimum natif du site : prix de départ, puis max(ceil(1,1 × mise), mise + 1)
  [WR market-api.js]. Le site prolonge de 60 s une enchère disputée dans ses 10 dernières
  secondes [WR TECHNIQUE.md] : le bot ré-évalue à chaque passage, sans dépasser son plafond.
- Une mise au résultat incertain n'est jamais rejouée.
- En DRY_RUN, gains et reventes sont simulés pour mesurer le résultat de la stratégie.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from src.config import Settings
from src.core.client import ApiError, RateLimited, UncertainMutation
from src.core.database import utcnow
from src.core.models import Auction, BidResult
from src.core.pricing import Quote
from src.core.text import normalize_title
from src.core.timeutils import in_window, local_dt, now_local
from src.workers.base import Worker

COLLECTION_PREFIX = "collection"  # mises de la règle par rareté : gardées, pas revendues


@dataclass(frozen=True)
class SnipeParams:
    window_seconds: int
    rarity_caps: dict[str, int]
    shiny_cap: int
    deal_ratio: float
    max_bid: int   # 0 = pas de plafond absolu
    budget: int    # 0 = pas de budget quotidien
    night: bool


def snipe_params(s: Settings, night: bool) -> SnipeParams:
    mult = s.night_caps_multiplier if night else 1.0
    return SnipeParams(
        window_seconds=s.night_window_seconds if night else s.sniper_window_seconds,
        rarity_caps={r: int(v * mult) for r, v in s.sniper_rarity_caps.items()},
        shiny_cap=int(s.sniper_shiny_cap * mult),
        deal_ratio=s.night_deal_ratio_max if night else s.sniper_deal_ratio_max,
        max_bid=s.sniper_max_bid,
        budget=(s.sniper_daily_budget + (s.night_budget_bonus if night else 0)) if s.sniper_daily_budget else 0,
        night=night,
    )


@dataclass(frozen=True)
class Portfolio:
    capital: int          # WikiBidous + capital déjà engagé en positions et enchères réelles
    available: int        # mobilisable tout de suite (réserve et engagements simulés déduits)
    position_budget: int  # mise maximale pour une position
    min_profit: int       # bénéfice attendu minimum
    open_positions: int   # positions détenues + enchères du bot en cours
    max_positions: int

    @property
    def can_open(self) -> bool:
        return self.open_positions < self.max_positions


def portfolio_state(s: Settings, *, balance: int, held_count: int, held_real_cost: int, held_sim_cost: int,
                    engaged_real: int, engaged_sim: int, bot_engaged_count: int) -> Portfolio:
    # Les mises réelles et positions réelles ont déjà quitté le solde : on les rajoute au capital.
    capital = balance + held_real_cost + engaged_real
    available = balance - held_sim_cost - engaged_sim - s.sniper_min_balance_reserve
    budget = int(capital * s.portfolio_capital_ratio / max(1, s.portfolio_slots))
    min_profit = max(s.portfolio_min_profit, int(budget * s.portfolio_min_profit_ratio))
    return Portfolio(capital, max(0, available), budget, min_profit,
                     held_count + bot_engaged_count, s.portfolio_max_positions)


@dataclass(frozen=True)
class Decision:
    amount: int
    ceiling: int
    reason: str
    reference: float | None
    profit: float | None = None  # bénéfice attendu à la revente

    @property
    def collection(self) -> bool:
        return self.reason.startswith(COLLECTION_PREFIX)


def rarity_ceiling(auction: Auction, params: SnipeParams) -> int | None:
    caps = []
    if auction.rarity in params.rarity_caps:
        caps.append(params.rarity_caps[auction.rarity])
    if auction.shiny and params.shiny_cap > 0:
        caps.append(params.shiny_cap)
    return max(caps) if caps else None


def decide(auction: Auction, params: SnipeParams, quote: Quote | None, min_sales: int,
           portfolio: Portfolio | None = None, fee: float = 0.0) -> Decision | None:
    """Position à revendre (estimation, budget, bénéfice) ou achat « collection » par rareté."""
    amount = auction.min_bid
    options: list[Decision] = []
    cap = rarity_ceiling(auction, params)
    if cap is not None and amount <= cap:
        label = f"{COLLECTION_PREFIX} : {auction.rarity}{' shiny' if auction.shiny else ''} ≤ {cap}"
        options.append(Decision(amount, cap, label, quote.average if quote else None))
    if quote is not None and quote.reliable(min_sales):
        net = quote.average * (1 - fee)  # type: ignore[operator]
        limit = math.floor(quote.average * params.deal_ratio)  # type: ignore[operator]
        if portfolio is not None:
            limit = min(limit, math.floor(net - portfolio.min_profit), portfolio.position_budget)
        if amount <= limit:
            options.append(Decision(amount, limit, f"revente ~{quote.average:.0f} WB, bénéfice ~{net - amount:.0f}",
                                    quote.average, net - amount))
    if not options:
        return None
    best = max(options, key=lambda d: d.ceiling)
    ceiling = min(best.ceiling, params.max_bid) if params.max_bid else best.ceiling
    if amount > ceiling:
        return None
    return Decision(amount, ceiling, best.reason, best.reference, best.profit)


def in_snipe_window(auction: Auction, me: str | None, window: int, now: datetime) -> bool:
    left = auction.seconds_left(now)
    return (
        auction.status == "active"
        and 0 < left <= window
        and (me is None or ((auction.seller_id or "").lower() != me and (auction.current_bidder_id or "").lower() != me))
    )


def is_own(auction: Auction, me: str | None) -> bool:
    return me is not None and (auction.seller_id or "").lower() == me


class AuctionSniper(Worker):
    name = "sniper"
    alert_after_errors = 20

    async def setup(self) -> None:
        self._balance: int | None = None
        self._balance_at = utcnow()
        self._ticks = 0
        self._budget_warned_day: str | None = None
        self._next_prefetch = utcnow()
        # Mises réelles en tête : auction_id -> (bid_id, montant)
        self._leading: dict[str, tuple[int, int]] = {}
        for row in await self.db.placed_bids():
            self._leading[row["auction_id"]] = (row["id"], row["amount"])
        # Enchères engagées (le site en limite le nombre) : auction_id -> fin.
        # Réelles (lues sur le serveur, mises manuelles comprises) + celles du bot, même simulées.
        self._engaged: dict[str, datetime] = {}
        # Enchères engagées par le bot lui-même (pour le décompte des positions).
        self._bot_engaged: set[str] = set(self._leading)
        self._digest: list[str] = []
        self._next_digest = utcnow() + timedelta(minutes=self.settings.notify_digest_minutes)
        if self.settings.sniper_max_concurrent_bids > 0:
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
        # Chaque passage est mémorisé : le dernier prix vu avant la fin devient une vente observée.
        await self._remember(auctions, now)
        if self._ticks % 10 == 5:
            await self.db.harvest_ended_auctions(now - timedelta(seconds=30))
        await self._track_outbids(auctions, me)
        if self._ticks % 20 == 0 and self.settings.sniper_max_concurrent_bids > 0:
            await self._refresh_engaged()

        in_window_now = [a for a in auctions if in_snipe_window(a, me, params.window_seconds, now)]
        if in_window_now:
            await self._play(in_window_now, params, now)
        else:
            await self._prefetch(auctions, params, me, now)

        if self._ticks % 10 == 0:
            await self._settle()
        self._flush_digest(now)
        return random.uniform(self.settings.sniper_poll_min_seconds, self.settings.sniper_poll_max_seconds)

    async def _play(self, auctions: list[Auction], params: SnipeParams, now: datetime) -> None:
        """Évalue toutes les opportunités du passage, puis joue les meilleures dans les places libres."""
        portfolio = await self.portfolio(now)
        candidates: list[tuple[float, Auction, Decision]] = []
        for auction in auctions:
            decision = await self._evaluate(auction, params, portfolio)
            if decision:
                score = decision.profit if decision.profit is not None else decision.ceiling - decision.amount
                candidates.append((score, auction, decision))
        candidates.sort(key=lambda c: c[0], reverse=True)
        free = self.free_slots(now)
        opened = 0
        for _, auction, decision in candidates:
            rebid = auction.id in self._engaged
            if not rebid:
                if free <= 0:
                    self.log.debug("plus de place pour %s (%d enchères en cours)", auction.title, len(self._engaged))
                    continue
                if not decision.collection and portfolio.open_positions + opened >= portfolio.max_positions:
                    self.log.debug("positions pleines, %s ignorée", auction.title)
                    continue
            if not await self._affordable(decision.amount, params, portfolio):
                continue
            if await self._bid(auction, decision, params) and not rebid:
                free -= 1
                opened += 1

    async def _prefetch(self, auctions: list[Auction], params: SnipeParams, me: str | None, now: datetime) -> None:
        """Hors fenêtre de mise, cote à l'avance une enchère proche de sa fin (route des cotes limitée)."""
        if now < self._next_prefetch:
            return
        self._next_prefetch = now + timedelta(seconds=self.settings.sniper_prefetch_seconds)
        for a in auctions:
            if a.status != "active" or is_own(a, me) or a.seconds_left(now) <= params.window_seconds:
                continue
            if await self.ctx.prices.is_cached(a.catalog_id):
                continue
            try:
                await self.ctx.prices.quote(a.catalog_id, a.rarity, wait=False)
            except (RateLimited, ApiError):
                pass
            return

    async def portfolio(self, now: datetime) -> Portfolio:
        held = await self.db.positions_summary()
        engaged_real = sum(amount for _, amount in self._leading.values())
        engaged_sim = await self.db.simulated_engaged_cost(now) if self.settings.dry_run else 0
        bot_engaged = sum(1 for aid in self._bot_engaged if aid in self._engaged)
        return portfolio_state(
            self.settings, balance=await self._current_balance(), held_count=held["count"],
            held_real_cost=held["real_cost"], held_sim_cost=held["sim_cost"],
            engaged_real=engaged_real, engaged_sim=engaged_sim, bot_engaged_count=bot_engaged,
        )

    async def _current_balance(self) -> int:
        if self._balance is None or utcnow() - self._balance_at > timedelta(seconds=60):
            self._balance, self._balance_at = await self.api.balance(), utcnow()
        return self._balance

    def free_slots(self, now: datetime) -> int:
        grace = timedelta(seconds=self.settings.sniper_settle_grace_seconds)
        self._engaged = {aid: end for aid, end in self._engaged.items() if end + grace > now}
        self._bot_engaged &= set(self._engaged) | set(self._leading)
        limit = self.settings.sniper_max_concurrent_bids
        return max(0, limit - len(self._engaged)) if limit > 0 else 1_000_000

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

    async def _evaluate(self, auction: Auction, params: SnipeParams, portfolio: Portfolio) -> Decision | None:
        last = await self.db.last_bid(auction.id)
        if last is not None:
            if last["status"] in ("uncertain", "simulated"):
                return None
            if last["status"] == "rejected" and last["amount"] >= auction.min_bid:
                return None
            if last["status"] in ("placed", "outbid") and not self.settings.sniper_allow_rebid:
                return None
        # Sans attendre : si la route des cotes est limitée, l'enchère est simplement ignorée.
        try:
            quote = await self.ctx.prices.quote(auction.catalog_id, auction.rarity, wait=False)
        except (RateLimited, ApiError):
            quote = None
        return decide(auction, params, quote, self.settings.price_min_sales, portfolio, self.settings.sale_fee_pct)

    async def _affordable(self, amount: int, params: SnipeParams, portfolio: Portfolio) -> bool:
        if amount > portfolio.available:
            return False
        if not params.budget or self.settings.dry_run:
            return True
        today = now_local().date()
        committed = await self.db.committed_since(local_dt(today, time(0, 0)))
        if committed + amount > params.budget:
            if self._budget_warned_day != str(today):
                self._budget_warned_day = str(today)
                self.notifier.info("Sniper : budget du jour atteint", f"{committed}/{params.budget} WikiBidous engagés.")
            return False
        return True

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
            self._bot_engaged.add(auction.id)
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
            self._bot_engaged.add(auction.id)
            self._report_bid("Enchère placée", f"{summary}, solde {confirmed.bidder_balance}")
            return True

        server_min = payload.get("min")
        error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
        await self.db.record_bid(**common, status="rejected", note=str(error)[:300])
        self.log.info("mise refusée sur %s : %s", label, error)
        if not retried and isinstance(server_min, int) and decision.amount < server_min <= decision.ceiling:
            return await self._bid(auction, Decision(server_min, decision.ceiling, decision.reason, decision.reference,
                                                     decision.profit), params, retried=True)
        return False

    def _report_bid(self, title: str, summary: str) -> None:
        self.log.info("%s : %s", title, summary)
        if self.settings.notify_each_bid:
            self.notifier.info(title, summary)
        else:
            self._digest.append(summary)

    async def _settle(self) -> None:
        """Résultat des enchères échues. En DRY_RUN : gagnée si personne n'a payé plus que notre mise."""
        cutoff = utcnow() - timedelta(seconds=self.settings.sniper_settle_grace_seconds)
        me = self.api.user_id
        for row in await self.db.bids_to_settle(cutoff, limit=3):
            auction = await self.api.auction(row["auction_id"])
            if auction.status == "active":
                await self.db.update_bid(row["id"], end_at=auction.end_at)  # prolongée
                continue
            simulated = row["status"] == "simulated"
            if simulated:
                final = auction.final_price if auction.final_price is not None else auction.current_bid
                won = auction.status == "settled_unsold" or final is None or final < row["amount"]
                status = "sim_won" if won else "sim_lost"
            else:
                winner = (auction.winner_id or auction.current_bidder_id or "").lower()
                won = auction.status == "settled_sold" and me is not None and winner == me
                status = "won" if won else "lost"
            # Un achat « collection » est gardé : ce n'est pas une position à revendre.
            closed = int(won and str(row["reason"] or "").startswith(COLLECTION_PREFIX))
            await self.db.update_bid(row["id"], status=status, settled_at=utcnow(), closed=closed)
            self._leading.pop(row["auction_id"], None)
            self._engaged.pop(row["auction_id"], None)
            if won:
                ref = row["reference_price"]
                detail = f"{row['title']} ({row['rarity']}) pour {row['amount']} WB"
                if ref:
                    detail += f", revente visée ~{ref:.0f} WB (bénéfice ~{ref - row['amount']:.0f})"
                if simulated:
                    self.notifier.info("Achat simulé remporté", detail)
                else:
                    self.notifier.success("Position achetée !", detail, nuit="oui" if row["night"] else "non")
