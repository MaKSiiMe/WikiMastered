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
from src.core import timeutils
from src.core.client import ApiError, RateLimited, UncertainMutation
from src.core.database import utcnow
from src.core.models import Auction, BidResult, Card
from src.core.notifier import Topic
from src.core.pricing import Quote
from src.core.text import normalize_title
from src.core.timeutils import in_window, local_dt, now_local
from src.workers.base import Worker
from src.workers.collection import KV_KEY as COLLECTION_KV
from src.workers.collection import collection_key

COLLECTION_PREFIX = "collection"  # mises de la règle par rareté : gardées, pas revendues
# Le site refuse plus de 50 enchères par page (403 dès limit=100, relevé du 15/10/2026).
MARKET_LIMIT = 50


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
    available: int        # pour une mise simulée : solde, moins la réserve et les engagements simulés
    position_budget: int  # mise maximale pour une position
    min_profit: int       # bénéfice attendu minimum
    open_positions: int   # positions détenues + enchères du bot en cours
    max_positions: int
    sale_room: int = 5            # places de vente libres pour le bot
    queue_min_profit: int = 0     # bénéfice exigé pour une position qui devra attendre
    real_available: int = 0       # pour une mise réelle : vrai solde moins la réserve (le simulé n'y touche pas)

    @property
    def can_open(self) -> bool:
        return self.open_positions < self.max_positions

    @property
    def required_profit(self) -> int:
        """Une position qui aura une place de vente tout de suite, ou qui devra attendre."""
        return self.min_profit if self.open_positions < self.sale_room else max(self.min_profit, self.queue_min_profit)


def position_cap(s: Settings, sale_slots: int | None, manual_sales: int) -> tuple[int, int]:
    """(positions autorisées, places de vente libres pour le bot).

    Au-delà des places libres, une position attend qu'une place se libère (file d'attente,
    PORTFOLIO_BUFFER au plus) : son capital est immobilisé, d'où un bénéfice exigé plus élevé."""
    if sale_slots is None:
        return s.portfolio_max_positions, s.portfolio_slots
    room = max(0, sale_slots - manual_sales)
    return min(s.portfolio_max_positions, room + s.portfolio_buffer), room


def portfolio_state(s: Settings, *, balance: int, held_count: int, held_real_cost: int, held_sim_cost: int,
                    engaged_real: int, engaged_sim: int, bot_engaged_count: int,
                    max_positions: int | None = None, sale_room: int | None = None) -> Portfolio:
    # Les mises réelles et positions réelles ont déjà quitté le solde : on les rajoute au capital.
    capital = balance + held_real_cost + engaged_real
    available = balance - held_sim_cost - engaged_sim - s.sniper_min_balance_reserve
    budget = int(capital * s.portfolio_capital_ratio / max(1, s.portfolio_slots))
    min_profit = max(s.portfolio_min_profit, int(budget * s.portfolio_min_profit_ratio))
    return Portfolio(capital, max(0, available), budget, min_profit, held_count + bot_engaged_count,
                     s.portfolio_max_positions if max_positions is None else max_positions,
                     s.portfolio_slots if sale_room is None else sale_room,
                     int(budget * s.portfolio_queue_profit_ratio),
                     max(0, balance - s.sniper_min_balance_reserve))


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
           portfolio: Portfolio | None = None, fee: float = 0.0, collection_cap: int | None = None) -> Decision | None:
    """Position à revendre (estimation, budget, bénéfice) ou achat « collection » (carte manquante, rareté)."""
    amount = auction.min_bid
    options: list[Decision] = []
    if collection_cap is not None and amount <= collection_cap:
        options.append(Decision(amount, collection_cap, f"{COLLECTION_PREFIX} : carte manquante ≤ {collection_cap}",
                                quote.average if quote else None))
    cap = rarity_ceiling(auction, params)
    if cap is not None and amount <= cap:
        label = f"{COLLECTION_PREFIX} : {auction.rarity}{' shiny' if auction.shiny else ''} ≤ {cap}"
        options.append(Decision(amount, cap, label, quote.average if quote else None))
    if quote is not None and quote.reliable(min_sales):
        net = quote.average * (1 - fee)  # type: ignore[operator]
        limit = math.floor(quote.average * params.deal_ratio)  # type: ignore[operator]
        if portfolio is not None:
            limit = min(limit, math.floor(net - portfolio.required_profit), portfolio.position_budget)
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


def refusal(auction: Auction, params: SnipeParams, quote: Quote | None, min_sales: int,
            portfolio: Portfolio | None = None, fee: float = 0.0) -> str:
    """Pourquoi `decide` n'a pas retenu une position (pour le récap) : la première règle qui bloque."""
    if quote is None or not quote.reliable(min_sales):
        return "pas assez vue vendue"
    amount = auction.min_bid
    if amount > math.floor(quote.average * params.deal_ratio):  # type: ignore[operator]
        return f"mise > {params.deal_ratio:.0%} de l'estimation"
    if portfolio is not None:
        if amount > math.floor(quote.average * (1 - fee) - portfolio.required_profit):  # type: ignore[operator]
            return "bénéfice attendu trop faible"
        if amount > portfolio.position_budget:
            return "au-dessus du budget d'une position"
    if params.max_bid and amount > params.max_bid:
        return "au-dessus de SNIPER_MAX_BID"
    return "autre"


def in_snipe_window(auction: Auction, me: str | None, window: int, now: datetime, min_left: int = 0) -> bool:
    """Fenêtre de mise : entre `window` et `min_left` secondes avant la fin (au-dessous, le site prolonge)."""
    left = auction.seconds_left(now)
    return (
        auction.status == "active"
        and max(0, min_left) < left <= window
        and (me is None or ((auction.seller_id or "").lower() != me and (auction.current_bidder_id or "").lower() != me))
    )


def is_own(auction: Auction, me: str | None) -> bool:
    return me is not None and (auction.seller_id or "").lower() == me


class AuctionSniper(Worker):
    name = "sniper"
    topic = Topic.SNIPER
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
        self._digest: list[tuple[bool, str]] = []  # (simulée, résumé)
        # Sort de chaque enchère entrée dans la fenêtre de mise depuis le dernier récap : id -> issue.
        self._outcomes: dict[str, str] = {}
        self._next_digest = utcnow() + timedelta(minutes=self.settings.notify_digest_minutes)
        # Tes mises manuelles (collections) : enchères et cartes que le bot ne touche jamais.
        self._manual_auctions: set[str] = set()
        self._manual_cards: set[str] = set()
        self._sale_slots: tuple[datetime, int, int] | None = None  # (lu à, max, ventes en cours)
        # Collection à compléter (module G) : clés des cartes manquantes, relues en base chaque minute.
        self._collection_missing: set[str] = set()
        # Jours manquants d'après la dernière lecture (avant de retirer ceux que le bot a gagnés), et
        # ventes de ces cartes déjà signalées dans les logs : id -> issue journalisée (None = repérée).
        self._collection_wanted: set[str] = set()
        self._collection_logged: dict[str, str | None] = {}
        self._market_warned_at: datetime | None = None
        await self._load_collection()
        await self._refresh_engaged()
        await self._check_config()
        if not self.api.user_id:
            self.notifier.warning("Sniper : identifiant joueur inconnu",
                                  "Renseigne WM_USER_ID pour ne jamais enchérir sur tes propres ventes.")

    async def _check_config(self) -> None:
        """Signale au démarrage les réglages qui contredisent la stratégie orientée capital."""
        s = self.settings
        if s.sniper_rarity_caps or s.sniper_shiny_cap:
            self.notifier.warning("Sniper : achats par rareté actifs",
                                  "SNIPER_RARITY_CAPS / SNIPER_SHINY_CAP achètent des cartes gardées, jamais "
                                  "revendues. Retire ces variables si tu ne le veux pas.")
        if s.sniper_daily_budget:
            budget = (await self.portfolio(utcnow())).position_budget
            if s.sniper_daily_budget < budget:
                self.notifier.warning("Sniper : budget du jour trop petit",
                                      f"SNIPER_DAILY_BUDGET={s.sniper_daily_budget} < budget d'une position "
                                      f"(~{budget} WB) : la plupart des achats seront bloqués. Mets 0 ou plus.")

    async def step(self) -> float:
        self._ticks += 1
        now = utcnow()
        night = in_window(now_local(), self.settings.night_start, self.settings.night_end)
        params = snipe_params(self.settings, night)
        me = self.api.user_id

        auctions = await self._market(params.window_seconds, now)
        # Chaque passage est mémorisé : le dernier prix vu avant la fin devient une vente observée.
        await self._remember(auctions, now)
        if self._ticks % 10 == 5:
            await self.db.harvest_ended_auctions(now - timedelta(seconds=30))
        await self._track_outbids(auctions, me)
        if self._ticks % 20 == 0:
            await self._refresh_engaged()
            await self._load_collection()

        self._log_collection_sales(auctions, now)
        in_window_now = [a for a in auctions if in_snipe_window(a, me, params.window_seconds, now,
                                                                self.settings.sniper_min_seconds_left)]
        if in_window_now:
            await self._play(in_window_now, params, now)
            self._log_collection_outcomes(in_window_now)
        else:
            await self._prefetch(auctions, params, me, now)

        if self._ticks % 10 == 0:
            await self._settle()
            await self._reconcile()
        self._flush_digest(now)
        return random.uniform(self.settings.sniper_poll_min_seconds, self.settings.sniper_poll_max_seconds)

    async def _market(self, window: int, now: datetime) -> list[Auction]:
        """Enchères qui finissent bientôt. Signale (une fois par heure) une page qui n'atteint pas
        la fenêtre de mise, par ex. remplie d'enchères déjà terminées."""
        auctions, more = await self.api.market_page(sort="ending_soon", limit=MARKET_LIMIT)
        left = [a.seconds_left(now) for a in auctions]
        if more and not any(sec > window for sec in left) and (
                self._market_warned_at is None or now - self._market_warned_at > timedelta(hours=1)):
            self._market_warned_at = now
            self.log.warning("marché : aucune des %d enchères lues ne finit dans plus de %d s (%d déjà terminées) : "
                             "les fins imminentes sont hors de la page", len(auctions), window,
                             sum(sec <= 0 for sec in left))
        return auctions

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
        collection_cards: set[str] = set()
        for _, auction, decision in candidates:
            if decision.collection:
                # Deux ventes de la même carte manquante dans le même passage : une seule mise.
                if auction.catalog_id in collection_cards:
                    continue
                collection_cards.add(auction.catalog_id)
            rebid = auction.id in self._engaged
            if not rebid:
                if free <= 0:
                    self.log.debug("plus de place pour %s (%d enchères en cours)", auction.title, len(self._engaged))
                    self._outcome(auction, "limite d'enchères en cours")
                    continue
                if not decision.collection:
                    open_now = portfolio.open_positions + opened
                    if open_now >= portfolio.max_positions:
                        self.log.debug("positions pleines, %s ignorée", auction.title)
                        self._outcome(auction, "plafond de positions atteint")
                        continue
                    # Les achats précédents du passage ont pris les places libres : celle-ci attendra.
                    queued = open_now >= portfolio.sale_room
                    if queued and (decision.profit or 0) < max(portfolio.min_profit, portfolio.queue_min_profit):
                        self._outcome(auction, "file d'attente : bénéfice trop faible")
                        continue
            # Le temps a passé depuis la lecture du marché (cotes, solde…) : on revérifie juste avant de miser.
            if auction.seconds_left() <= self.settings.sniper_min_seconds_left:
                self.log.debug("%s : trop tard pour miser sans prolonger l'enchère", auction.title)
                self._outcome(auction, "trop tard")
                continue
            if not await self._affordable(decision, params, portfolio):
                self._outcome(auction, "solde ou budget du jour insuffisant")
                continue
            if await self._bid(auction, decision, params):
                self._outcome(auction, "misée (collection)" if decision.collection else "misée")
                if not rebid:
                    free -= 1
                    opened += 1
            else:
                self._outcome(auction, "mise refusée par le site")

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
                await self.ctx.prices.quote(a.catalog_id, a.rarity, wait=False, shiny=a.shiny)
            except (RateLimited, ApiError):
                pass
            return

    async def portfolio(self, now: datetime) -> Portfolio:
        held = await self.db.positions_summary()
        engaged_real = sum(amount for _, amount in self._leading.values())
        pending = await self.db.pending_bids()  # mises pas encore réglées, même après la fin de l'enchère
        cap, room = await self._sale_capacity(now)
        return portfolio_state(
            self.settings, balance=await self._current_balance(), held_count=held["count"],
            held_real_cost=held["real_cost"], held_sim_cost=held["sim_cost"],
            engaged_real=engaged_real, engaged_sim=pending["sim_cost"], bot_engaged_count=pending["positions"],
            max_positions=cap, sale_room=room,
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
        """Relit tes enchères en cours : décompte (si limite) et repérage de tes mises manuelles."""
        if self.settings.sniper_max_concurrent_bids <= 0 and not self.settings.sniper_avoid_my_bids:
            return
        manual_auctions, manual_cards = set(), set()
        for a in await self.api.my_bids():
            if a.status != "active":
                continue
            if self.settings.sniper_max_concurrent_bids > 0:
                self._engaged[a.id] = a.end_at
            if a.id not in self._bot_engaged and await self.db.last_bid(a.id) is None:
                manual_auctions.add(a.id)
                manual_cards.add(a.catalog_id)
        self._manual_auctions, self._manual_cards = manual_auctions, manual_cards

    async def _load_collection(self) -> None:
        if not (self.settings.collection_sets or self.settings.collection_titles):
            return
        state = await self.db.kv_get(COLLECTION_KV) or {}
        # Gagnées par le bot (même en simulation) : plus manquantes, même avant la prochaine lecture.
        won = {collection_key(t) for t in await self.db.collection_cards(("won", "sim_won"))}
        self._collection_wanted = set(state.get("missing", []))
        self._collection_missing = self._collection_wanted - won

    def _log_collection_sales(self, auctions: list[Auction], now: datetime) -> None:
        """Journalise une fois chaque vente d'une carte de collection manquante repérée sur le marché."""
        for a in auctions:
            if a.id in self._collection_logged or collection_key(a.title) not in self._collection_wanted:
                continue
            self._collection_logged[a.id] = None
            left = a.seconds_left(now)
            when = f"dans {left / 60:.0f} min" if left >= 120 else (f"dans {left:.0f} s" if left > 0 else
                                                                    f"déjà terminée depuis {-left:.0f} s")
            self.log.info("collection : %s (%s) en vente, mise min %d WB, fin %s (%s)", a.title, a.rarity, a.min_bid,
                          f"{a.end_at.astimezone(timeutils.PARIS):%H:%M:%S}", when)
        if len(self._collection_logged) > 2000:
            self._collection_logged.clear()

    def _log_collection_outcomes(self, auctions: list[Auction]) -> None:
        """Journalise l'issue d'une carte de collection manquante dans la fenêtre de mise (une fois par issue)."""
        for a in auctions:
            key = collection_key(a.title)
            if key not in self._collection_wanted:
                continue
            if key not in self._collection_missing:
                outcome = "déjà gagnée par le bot (lance reset-simulation si c'était en simulation)"
            elif self.is_manual_interest(a):
                outcome = "tu mises sur cette carte"
            else:
                outcome = self._outcomes.get(a.id, "pas évaluée (mise déjà en cours sur une autre vente ?)")
            if self._collection_logged.get(a.id) != outcome:
                self._collection_logged[a.id] = outcome
                self.log.info("collection : %s à %d WB, %.0f s avant la fin : %s", a.title, a.min_bid,
                              a.seconds_left(), outcome)

    async def _collection_cap(self, auction: Auction) -> int | None:
        """Plafond si la carte manque à la collection et que le bot ne mise pas déjà sur une autre vente."""
        if collection_key(auction.title) not in self._collection_missing:
            return None
        engaged = await self.db.collection_auctions_engaged(auction.catalog_id)
        if engaged - {auction.id}:
            return None  # une autre vente de la même carte est déjà en cours pour le bot
        return self.settings.collection_max_price

    def is_manual_interest(self, auction: Auction) -> bool:
        """Carte que tu cherches toi-même (tu mises dessus) : le bot ne l'achète pas pour la revendre."""
        return self.settings.sniper_avoid_my_bids and (
            auction.id in self._manual_auctions or auction.catalog_id in self._manual_cards)

    async def _sale_capacity(self, now: datetime) -> tuple[int, int]:
        """(positions autorisées, places libres pour le bot), d'après les places de vente (relu toutes les 5 min)."""
        if self._sale_slots is None or now - self._sale_slots[0] > timedelta(minutes=5):
            try:
                selling, maximum = await self.api.listing_slots()
            except ApiError:
                return position_cap(self.settings, None, 0)
            self._sale_slots = (now, maximum, selling)
        _, maximum, selling = self._sale_slots
        manual_sales = max(0, selling - await self.db.posted_listings_count())
        return position_cap(self.settings, maximum, manual_sales)

    def _flush_digest(self, now: datetime) -> None:
        if now < self._next_digest:
            return
        self._next_digest = now + timedelta(minutes=self.settings.notify_digest_minutes)
        if not self._digest:
            return
        entries, self._digest = self._digest, []
        simulated = sum(sim for sim, _ in entries)
        real = len(entries) - simulated
        mixed = simulated and real
        lines = [f"{'[simulée] ' if mixed and sim else ''}{summary}" for sim, summary in entries]
        shown = "\n".join(lines[-15:])
        more = f"\n… et {len(lines) - 15} autre(s)" if len(lines) > 15 else ""
        if mixed:  # ex. mises de collection réelles, positions simulées (SIMULATE=sniper)
            title = f"Sniper : {len(lines)} enchères ({real} placée(s), {simulated} simulée(s))"
        else:
            title = f"Sniper : enchères {'simulées' if simulated else 'placées'} ({len(lines)})"
        self.notifier.info(title, shown + more)

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
        if last is None and self.is_manual_interest(auction):
            self._outcome(auction, "tu mises sur cette carte")
            return None
        if last is not None:
            if last["status"] in ("uncertain", "simulated"):
                return None  # déjà misée : l'issue est déjà comptée
            if last["status"] == "rejected" and last["amount"] >= auction.min_bid:
                return None
            if last["status"] in ("placed", "outbid") and not self.settings.sniper_allow_rebid:
                return None
        # Sans attendre : si la route des cotes est limitée, l'enchère est simplement ignorée.
        try:
            quote = await self.ctx.prices.quote(auction.catalog_id, auction.rarity, wait=False, shiny=auction.shiny)
        except (RateLimited, ApiError):
            quote = None
        collection_cap = await self._collection_cap(auction)
        decision = decide(auction, params, quote, self.settings.price_min_sales, portfolio, self.settings.sale_fee_pct,
                          collection_cap=collection_cap)
        if decision is None:
            self._outcome(auction, "collection : trop chère" if collection_cap is not None else
                          refusal(auction, params, quote, self.settings.price_min_sales, portfolio,
                                  self.settings.sale_fee_pct))
        return decision

    def _outcome(self, auction: Auction, outcome: str) -> None:
        """Retient l'issue d'une enchère ; une mise placée n'est plus remplacée par un refus ultérieur."""
        if self._outcomes.get(auction.id) not in ("misée", "misée (collection)"):
            self._outcomes[auction.id] = outcome

    def recap(self) -> list[str]:
        outcomes, self._outcomes = self._outcomes, {}
        if not outcomes:
            return ["Aucune enchère dans la fenêtre de mise."]
        counts: dict[str, int] = {}
        for outcome in outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return [f"{len(outcomes)} enchère(s) examinée(s) : " + " · ".join(f"{o} {n}" for o, n in ranked)]

    async def _affordable(self, decision: Decision, params: SnipeParams, portfolio: Portfolio) -> bool:
        amount = decision.amount
        # Une mise réelle (ex. collection avec SIMULATE=sniper) ne dépend que du vrai solde : les positions
        # simulées ne doivent pas la bloquer.
        live = self.settings.is_live("collection_bid" if decision.collection else "place_bid")
        if amount > (portfolio.real_available if live else portfolio.available):
            return False
        if not params.budget:
            return True
        # Appliqué aussi en DRY_RUN : la simulation doit montrer un budget trop serré.
        today = now_local().date()
        committed = await self.db.committed_since(local_dt(today, time(0, 0)), not self.settings.is_live("place_bid"))
        if committed + amount > params.budget:
            if self._budget_warned_day != str(today):
                self._budget_warned_day = str(today)
                self.notifier.info("Sniper : budget du jour atteint", f"{committed}/{params.budget} WikiBidous engagés.")
            return False
        return True

    async def _bid(self, auction: Auction, decision: Decision, params: SnipeParams, *, retried: bool = False) -> bool:
        """Place la mise ; renvoie True si elle est acceptée (ou simulée)."""
        # Mises de positions (« sniper ») et de collection (« collection ») : réelles ou simulées séparément.
        live = self.settings.is_live("collection_bid" if decision.collection else "place_bid")
        common = dict(
            auction_id=auction.id, card_id=auction.catalog_id, title=auction.title, rarity=auction.rarity,
            is_shiny=auction.shiny, amount=decision.amount, reference_price=decision.reference,
            reason=decision.reason, night=params.night, dry_run=not live, end_at=auction.end_at,
        )
        label = f"{auction.title} ({auction.rarity}{' ✨' if auction.shiny else ''})"
        try:
            result = await self.api.place_bid(auction.id, decision.amount, live=live)
        except UncertainMutation:
            await self.db.record_bid(**common, status="uncertain", note="réponse perdue")
            self._engaged[auction.id] = auction.end_at  # la mise a peut-être abouti : place occupée
            raise

        summary = f"{label} : {decision.amount} WB ({decision.reason}{', nuit' if params.night else ''})"
        if result.dry_run:
            await self.db.record_bid(**common, status="simulated")
            self._engaged[auction.id] = auction.end_at
            self._bot_engaged.add(auction.id)
            self._report_bid("Enchère simulée", summary, simulated=True)
            return True

        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.ok:
            try:
                confirmed = BidResult.model_validate(payload)
            except ValueError:
                confirmed = None
            if confirmed is None or confirmed.current_bid != decision.amount:
                await self.db.record_bid(**common, status="uncertain", note=str(payload)[:300])
                self.notifier.critical("Enchère non confirmée", f"{label} — réponse inattendue, le bot relira "
                                       "l'enchère sur le site d'ici une minute.")
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

    def _report_bid(self, title: str, summary: str, *, simulated: bool = False) -> None:
        self.log.info("%s : %s", title, summary)
        if self.settings.notify_each_bid:
            self.notifier.info(title, summary)
        else:
            self._digest.append((simulated, summary))

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
            else:
                winner = (auction.winner_id or auction.current_bidder_id or "").lower()
                won = auction.status == "settled_sold" and me is not None and winner == me
            await self._close_bid(row, won, simulated, auction.card)

    async def _close_bid(self, row, won: bool, simulated: bool, card: Card | None = None) -> None:
        """Enregistre le résultat d'une enchère terminée ; une carte gagnée devient une position."""
        status = ("sim_won" if won else "sim_lost") if simulated else ("won" if won else "lost")
        # Un achat « collection » est gardé : ce n'est pas une position à revendre.
        closed = int(won and str(row["reason"] or "").startswith(COLLECTION_PREFIX))
        await self.db.update_bid(row["id"], status=status, settled_at=utcnow(), closed=closed)
        self._leading.pop(row["auction_id"], None)
        self._engaged.pop(row["auction_id"], None)
        if not won:
            return
        link = card.page_url if card else None
        name = f"**[{row['title']}]({link})**" if link else f"**{row['title']}**"
        look = {"thumbnail": card.image_url if card else None, "url": link}
        if closed:
            notify = self.notifier.info if simulated else self.notifier.success
            title = "Collection : carte obtenue (simulé)" if simulated else "Collection : carte obtenue !"
            notify(title, f"{name}, gardée dans la collection", topic=Topic.COLLECTION, **look,
                   **{"rareté": row["rarity"] or "?", "prix": f"{row['amount']} WB"})
            return
        ref = row["reference_price"]
        fields = {"rareté": row["rarity"] or "?", "mise": f"{row['amount']} WB"}
        if ref:
            fields["revente visée"] = f"~{ref:.0f} WB"
            fields["bénéfice attendu"] = f"~{ref - row['amount']:.0f} WB"
        fields["nuit"] = "oui" if row["night"] else "non"
        if simulated:
            self.notifier.info("Achat simulé remporté", name, **look, **fields)
        else:
            self.notifier.success("Position achetée !", name, **look, **fields)

    async def _reconcile(self) -> None:
        """Mises au résultat incertain (réponse perdue ou inattendue) : relues sur le serveur, jamais rejouées."""
        me = self.api.user_id
        if me is None:
            return
        for row in await self.db.uncertain_bids(utcnow() - timedelta(seconds=30)):
            auction = await self.api.auction(row["auction_id"])
            leader = (auction.winner_id or auction.current_bidder_id or "").lower()
            label = f"{row['title']} ({row['rarity']}), {row['amount']} WB"
            if auction.status != "active":
                won = auction.status == "settled_sold" and leader == me
                await self._close_bid(row, won, simulated=False, card=auction.card)
                verdict = "enchère remportée" if won else "enchère perdue ou mise non passée"
            elif leader == me:
                amount = auction.current_bid or row["amount"]
                await self.db.update_bid(row["id"], status="placed", amount=amount, end_at=auction.end_at,
                                         note="vérifiée : en tête")
                self._leading[auction.id] = (row["id"], amount)
                self._engaged[auction.id] = auction.end_at
                self._bot_engaged.add(auction.id)
                verdict = f"mise bien placée ({amount} WB), en tête"
            else:
                await self.db.update_bid(row["id"], status="outbid", note="vérifiée : pas en tête")
                self._leading.pop(auction.id, None)
                verdict = "pas en tête (mise non passée ou dépassée)"
            self.notifier.info("Mise incertaine vérifiée", f"{label} : {verdict}.")
