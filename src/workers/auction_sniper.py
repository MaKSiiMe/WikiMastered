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

Lecture du marché (relevés du 15/10/2026) : environ 12 enchères finissent chaque seconde et le site
laisse les enchères terminées en tête du tri « ending_soon » jusqu'à leur clôture (parfois 3 min) :
une page de 50 n'atteint jamais la fenêtre de mise. Le sniper repère donc les ventes à l'avance :
- nouvelles ventes des raretés suivies (SNIPER_RESALE_RARITIES, tri « recent ») ;
- ventes tout juste terminées de ces raretés (prix finaux = ventes observées, pour les cotes) ;
- jours manquants de la collection, cherchés un par un (q=« 14 octobre »), toutes raretés.
Une vente intéressante devient une cible ; dans sa fenêtre de mise, l'enchère est relue
(/api/marketplace/{id}) puis misée. Une cible misée est relue à sa fin : prolongée, elle est rejouée.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta

from src.config import Settings
from src.core import timeutils
from src.core.client import ApiError, RateLimited, UncertainMutation
from src.core.models import Auction, BidResult, Card
from src.core.notifier import Topic
from src.core.pricing import Quote, combine, trusted
from src.core.timeutils import in_window, local_dt, now_local, utcnow
from src.workers.base import Worker
from src.workers.collection import KV_KEY as COLLECTION_KV
from src.workers.collection import collection_key, record_win, targets as collection_targets

COLLECTION_PREFIX = "collection"  # mises de la règle par rareté : gardées, pas revendues
# Le site refuse plus de 50 enchères par page (403 dès limit=100, relevé du 15/10/2026).
MARKET_LIMIT = 50
# Une enchère lue il y a plus longtemps est relue avant d'y miser.
FRESH = timedelta(seconds=3)
# Une carte de collection manquante entre dans la fenêtre de mise d'ici N s : le sniper ne lance plus rien de lent.
COLLECTION_GUARD_SECONDS = 10
# Mise en tête : relue une dernière fois quand il reste au plus SNIPER_MIN_SECONDS_LEFT + N s. Une surenchère avant
# les 10 dernières secondes ne prolonge pas l'enchère : sans cette relecture, elle gagnait (rapport du 16/10 :
# Chris Pratt, William de Galles… parties sous notre plafond).
LAST_LOOK_SECONDS = 10
# Issues d'une mise acceptée (ou simulée) : la vente est relue à sa fin.
SHADOW_PLACED = "simulée (sniper de test)"
PLACED = ("misée", "misée (collection)", SHADOW_PLACED)
# Refus d'une mise tant que la vérification humaine du site n'est pas faite : « Vérification anti-bot
# requise. » (relevé du 15/10/2026, en même temps que celle des paquets).
ANTIBOT_HINT = "anti-bot"
# Tant qu'elle attend, une seule mise réelle part par période : elle teste si la vérification est levée. Le 16/10,
# ~45 mises refusées en 1 h 30 (toutes inutiles), puis vérification des paquets demandée à son tour.
ANTIBOT_RETRY = timedelta(minutes=5)
ANTIBOT_REMIND = timedelta(hours=1)  # alerte renouvelée tant que la vérification n'est pas faite
PENDING_ANTIBOT = "pas tentée : vérification anti-bot en attente"
QUOTA_FOR_L = "collection : quota de la vérification gardé pour les L"
# Mises surenchéries relues à la fin de leur enchère (gagnées malgré tout ?) : celles des 2 derniers jours,
# pas tout l'historique (enchères anciennes souvent disparues du site).
OUTBID_SETTLE_WINDOW = timedelta(days=2)
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass
class Target:
    """Vente suivie : la dernière lecture de l'enchère, et si elle est misée (relue à sa fin)."""
    auction: Auction
    seen_at: datetime
    recheck: bool = False
    last_look: bool = False  # mise en tête relue avant la fin (une fois par mise)


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


def pushed(minimum: int, ceiling: int, share: float) -> int:
    """Mise minimale + `share` de l'écart jusqu'au plafond (0 : le minimum, 1 : le plafond)."""
    return min(ceiling, minimum + math.ceil(max(0, ceiling - minimum) * max(0.0, share)))


def decide(auction: Auction, params: SnipeParams, quote: Quote | None, min_sales: int,
           portfolio: Portfolio | None = None, fee: float = 0.0, collection_cap: int | None = None,
           resale: bool = True, push: float = 0.0) -> Decision | None:
    """Position à revendre (estimation, budget, bénéfice) ou achat « collection » (carte manquante, rareté).
    Une position est misée au-dessus du minimum (`push`, voir SNIPER_BID_PUSH) ; la collection, au minimum."""
    amount = auction.min_bid
    options: list[Decision] = []
    if collection_cap is not None and amount <= collection_cap:
        options.append(Decision(amount, collection_cap, f"{COLLECTION_PREFIX} : carte manquante ≤ {collection_cap}",
                                quote.average if quote else None))
    cap = rarity_ceiling(auction, params)
    if cap is not None and amount <= cap:
        label = f"{COLLECTION_PREFIX} : {auction.rarity}{' shiny' if auction.shiny else ''} ≤ {cap}"
        options.append(Decision(amount, cap, label, quote.average if quote else None))
    if resale and quote is not None and quote.reliable(min_sales):
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
    if best.profit is not None and push > 0:
        bid = pushed(amount, ceiling, push)
        profit = best.profit - (bid - amount)
        return Decision(bid, ceiling, f"revente ~{best.reference:.0f} WB, bénéfice ~{profit:.0f}", best.reference,
                        profit)
    return Decision(amount, ceiling, best.reason, best.reference, best.profit)


def refusal(auction: Auction, params: SnipeParams, quote: Quote | None, min_sales: int,
            portfolio: Portfolio | None = None, fee: float = 0.0, resale: bool = True) -> str:
    """Pourquoi `decide` n'a pas retenu une position (pour le récap) : la première règle qui bloque."""
    if not resale:
        return "rareté non revendue"
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
        # Ventes suivies (repérées à l'avance) : auction_id -> cible.
        self._targets: dict[str, Target] = {}
        # Dernière lecture de chaque source de repérage, et ids de la dernière page « recent » par rareté
        # (sans recoupement avec la précédente, des ventes ont pu passer entre deux lectures).
        self._last_read: dict[tuple[str, str], datetime] = {}
        self._recent_ids: dict[str, set[str]] = {}
        self._day_queue: list[str] = []
        self._day_titles: dict[str, str] = {}
        self._gap_warned_at = _EPOCH
        # Depuis le dernier récap : trous dans les nouvelles ventes, ventes de jours manquants trouvées.
        self._gaps = self._days_found = 0
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
        self._antibot_since: datetime | None = None  # mises refusées : vérification anti-bot à faire
        self._antibot_retry_at = _EPOCH  # prochaine mise réelle permise tant que la vérification attend
        self._antibot_remind_at = _EPOCH  # prochain rappel d'alerte tant que la vérification attend
        self._antibot_missed = 0  # mises non placées depuis le début du blocage
        self._collection_since_check = 0  # mises réelles de collection depuis la dernière vérification
        self._buys_left: int | None = None  # achats réels encore permis (SNIPER_MAX_BUYS), None = illimité
        # Collection à compléter (module G) : clés des cartes manquantes, relues en base chaque minute.
        self._collection_missing: set[str] = set()
        # Jours manquants d'après la dernière lecture (avant de retirer ceux que le bot a gagnés), et
        # ventes de ces cartes déjà signalées dans les logs : id -> issue journalisée (None = repérée).
        self._collection_wanted: set[str] = set()
        self._collection_logged: dict[str, str | None] = {}
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
        # Une carte de collection manquante va entrer dans sa fenêtre de mise : aucune requête lente d'ici là
        # (cote, règlements…), sinon le passage en cours la ferait manquer (« 7 octobre … pas relue à temps »).
        guard = self._collection_soon(params, now) is not None
        if self._ticks % 20 == 0 and not guard:
            await self._refresh_engaged()
            await self._load_collection()

        # Une requête par passage, par priorité : carte de collection dans sa fenêtre de mise ou cible misée à
        # relire à sa fin ; repérage très en retard ; revente dans sa fenêtre ; cote à l'avance ; repérage.
        self._prune(now)
        target = self._next_due(params, now)
        if target is not None and (target.recheck or self._wanted(target.auction)):
            await self._read_target(target, now)
        elif guard:
            await self.portfolio(now)  # solde et places de vente en cache pour la mise qui vient
        elif target is not None and not self._discovery_late(params, now):
            await self._read_target(target, now)
        elif self._discovery_late(params, now) or not await self._prefetch(now):
            await self._discover(params, now)

        now = utcnow()
        ready = [t.auction for t in self._targets.values()
                 if not t.recheck and now - t.seen_at <= FRESH
                 and in_snipe_window(t.auction, me, params.window_seconds, now, self.settings.sniper_min_seconds_left)]
        if ready:
            for a in ready:
                # Surenchérie puis rejouée : la nouvelle issue (ex. « trop chère ») remplace la mise précédente,
                # sinon elle n'est ni journalisée ni comptée, et la vente est encore relue à sa fin pour rien.
                if self._outcomes.get(a.id) in PLACED:
                    del self._outcomes[a.id]
            await self._play(ready, params, now)
            self._log_collection_outcomes(ready)
            for a in ready:
                if self._outcomes.get(a.id) in PLACED:
                    self._targets[a.id].recheck = True  # relue à sa fin : prolongée, elle sera rejouée
                else:
                    self._targets.pop(a.id, None)
        for t in self._targets.values():
            if not t.recheck and me and (t.auction.current_bidder_id or "").lower() == me:
                t.recheck = True  # déjà en tête : relue à sa fin seulement

        if self._ticks % 10 == 5:
            await self.db.harvest_ended_auctions(now - timedelta(seconds=30))
        if self._ticks % 10 == 0 and not guard:
            await self._settle()
            await self._reconcile()
        self._flush_digest(now)
        return self._delay(params, utcnow())

    # ------------------------------------------------------------ repérage
    def _watched_rarities(self, params: SnipeParams) -> list[str]:
        return list(dict.fromkeys([*self._resale_rarities, *params.rarity_caps]))

    @property
    def _shadow_on(self) -> bool:
        """Sniper de test actif : mises de positions réelles, et simulation en plus."""
        return self.settings.sniper_shadow and self.settings.is_live("place_bid")

    @property
    def _resale_rarities(self) -> list[str]:
        """Raretés suivies pour la revente : celles du sniper réel, plus celles du sniper de test."""
        extra = self.settings.sniper_shadow_rarities if self._shadow_on else []
        return list(dict.fromkeys([*self.settings.sniper_resale_rarities, *extra]))

    def _lateness(self, params: SnipeParams, now: datetime) -> list[tuple[float, float, tuple[str, str]]]:
        """(retard en s, période visée, source) de chaque source de repérage : nouvelles ventes et ventes
        terminées de chaque rareté suivie, jours manquants de la collection."""
        sources = [((kind, rarity), every) for rarity in self._watched_rarities(params)
                   for kind, every in (("recent", self.settings.sniper_discovery_seconds),
                                       ("ended", self.settings.sniper_observe_seconds))]
        if self._collection_missing:
            sources.append((("day", ""), self.settings.collection_search_seconds))
        return [((now - self._last_read.get(key, _EPOCH)).total_seconds() - every, every, key)
                for key, every in sources]

    def _discovery_late(self, params: SnipeParams, now: datetime) -> bool:
        """Une source attend depuis plus de deux fois sa période : elle passe avant les reventes à relire,
        qui sinon prennent toutes les requêtes (le 15/10 : 2622 s entre deux lectures des nouvelles UR)."""
        return any(late >= every for late, every, _ in self._lateness(params, now))

    async def _discover(self, params: SnipeParams, now: datetime) -> None:
        """Lit la source de repérage la plus en retard : nouvelles ventes, ventes terminées ou jour manquant."""
        overdue = [(late, key) for late, _, key in self._lateness(params, now) if late >= 0]
        if not overdue:
            return
        kind, rarity = max(overdue)[1]
        previous = self._last_read.get((kind, rarity))
        self._last_read[(kind, rarity)] = now
        if kind == "recent":
            auctions, more = await self.api.market_page(sort="recent", rarity=rarity, limit=MARKET_LIMIT)
            self._check_gap(rarity, auctions, more, now, (now - previous).total_seconds() if previous else None)
            await self._ingest(auctions, params, now)
        elif kind == "ended":
            # Ventes terminées pas encore clôturées : leur dernier prix devient une vente observée.
            auctions, _ = await self.api.market_page(sort="ending_soon", rarity=rarity, limit=MARKET_LIMIT)
            await self._remember(auctions, now)
            await self._ingest(auctions, params, now)
        else:
            auctions, _ = await self.api.market_page(sort="ending_soon", q=self._next_day(), limit=MARKET_LIMIT)
            await self._ingest(auctions, params, now)

    def _next_day(self) -> str:
        """Prochain jour manquant à chercher (rotation sur tous les jours manquants)."""
        while self._day_queue and self._day_queue[0] not in self._collection_missing:
            self._day_queue.pop(0)
        if not self._day_queue:
            self._day_queue = sorted(self._collection_missing)
        key = self._day_queue.pop(0)
        return self._day_titles.get(key, key)

    def _check_gap(self, rarity: str, auctions: list[Auction], more: bool, now: datetime,
                   elapsed: float | None = None) -> None:
        """Deux lectures « recent » sans vente commune : des ventes créées entre les deux ont été manquées.

        L'avertissement n'est écrit qu'une fois par heure ; le récap compte tous les trous."""
        ids = {a.id for a in auctions}
        previous = self._recent_ids.get(rarity)
        self._recent_ids[rarity] = ids
        if not previous or not more or ids & previous:
            return
        self._gaps += 1
        if now - self._gap_warned_at > timedelta(hours=1):
            self._gap_warned_at = now
            # Le délai réel dépasse souvent SNIPER_DISCOVERY_SECONDS : toutes les requêtes du bot se partagent
            # un créneau toutes les HTTP_MIN_SPACING_SECONDS, et relire une cible passe avant le repérage.
            gap = f"{elapsed:.0f} s" if elapsed is not None else "?"
            self.log.warning("repérage %s : plus de %d nouvelles ventes en %s entre deux lectures, certaines ont "
                             "été manquées (visé : %s s ; avertissement suivant dans 1 h au plus tôt)", rarity,
                             len(auctions), gap, f"{self.settings.sniper_discovery_seconds:g}")

    async def _ingest(self, auctions: list[Auction], params: SnipeParams, now: datetime) -> None:
        """Met à jour les cibles connues et en ajoute : jour manquant, rareté achetée, revente possible."""
        await self._track_outbids(auctions, self.api.user_id)
        self._log_collection_sales(auctions, now)
        for a in auctions:
            known = self._targets.get(a.id)
            if known is not None:
                if not known.recheck:
                    self._targets[a.id] = Target(a, now)
            elif await self._worth_watching(a, params, now):
                self._targets[a.id] = Target(a, now)

    async def _worth_watching(self, a: Auction, params: SnipeParams, now: datetime) -> bool:
        """Sans requête : la vente peut-elle intéresser le sniper à sa fin (prix actuel sous le plafond) ?"""
        if a.status != "active" or is_own(a, self.api.user_id) \
                or a.seconds_left(now) <= self.settings.sniper_min_seconds_left:
            return False
        if collection_key(a.title) in self._collection_missing:
            return a.min_bid <= self.settings.collection_max_price
        cap = rarity_ceiling(a, params)
        if cap is not None and a.min_bid <= cap:
            return True
        if a.rarity not in self._resale_rarities or (self._buys_left == 0 and not self._shadow_on):
            return False  # achats réels épuisés, sans sniper de test : les relectures servent à la collection
        observed = trusted(await self.db.observed_by_rarity(a.catalog_id, self.settings.price_recent_sales, a.shiny))
        quote = combine(*observed.get(a.rarity or "", ([], 0)), None)
        return quote.reliable(self.settings.price_min_sales) and \
            a.min_bid <= math.floor(quote.average * params.deal_ratio)  # type: ignore[operator]

    # ------------------------------------------------------------ cibles
    def _prune(self, now: datetime) -> None:
        """Oublie les cibles passées ; une cible jamais relue dans sa fenêtre de mise est comptée."""
        for aid, t in list(self._targets.items()):
            if t.recheck or t.auction.seconds_left(now) > self.settings.sniper_min_seconds_left:
                continue
            del self._targets[aid]
            if aid not in self._outcomes:
                # Séparées au récap : une carte de collection manquée compte, une revente simulée beaucoup moins.
                self._outcome(t.auction, "collection : pas relue à temps" if self._wanted(t.auction)
                              else "pas relue à temps")
                self._log_collection_outcomes([t.auction])
        if len(self._targets) > 5000:  # garde-fou mémoire : les plus lointaines d'abord
            for aid, _ in sorted(self._targets.items(), key=lambda kv: kv[1].auction.end_at)[4000:]:
                del self._targets[aid]

    def _next_due(self, params: SnipeParams, now: datetime) -> Target | None:
        """Cible à relire maintenant : dans sa fenêtre de mise (cartes de collection manquantes d'abord,
        puis la plus proche de la fin), sinon une cible misée dont la fin est passée (prolongée ou terminée ?)."""
        low = self.settings.sniper_min_seconds_left + 1
        in_window_ = [t for t in self._targets.values() if not t.recheck and now - t.seen_at > FRESH
                      and low < t.auction.seconds_left(now) <= params.window_seconds]
        # Nos mises en tête, relues juste avant la fin : surenchéries, elles repassent dans la fenêtre de mise.
        in_window_ += [t for t in self._targets.values() if t.recheck and not t.last_look
                       and t.auction.id in self._leading
                       and low < t.auction.seconds_left(now) <= low + LAST_LOOK_SECONDS]
        if in_window_:
            return min(in_window_, key=lambda t: (not self._wanted(t.auction), t.auction.end_at))
        ended = [t for t in self._targets.values() if t.recheck and t.auction.seconds_left(now) < -3]
        return min(ended, key=lambda t: t.auction.end_at) if ended else None

    def _wanted(self, auction: Auction) -> bool:
        """Carte manquante à la collection (gardée, pas revendue)."""
        return collection_key(auction.title) in self._collection_missing

    def _collection_soon(self, params: SnipeParams, now: datetime) -> float | None:
        """Secondes avant qu'une carte de collection manquante entre dans sa fenêtre de mise (si ≤ la garde)."""
        soon = [left - params.window_seconds for t in self._targets.values()
                if not t.recheck and self._wanted(t.auction)
                and 0 < (left := t.auction.seconds_left(now)) - params.window_seconds <= COLLECTION_GUARD_SECONDS]
        return min(soon) if soon else None

    async def _read_target(self, target: Target, now: datetime) -> None:
        if not target.recheck:
            await self._prepare(target.auction, now)
        await self._refresh_target(target)

    async def _prepare(self, auction: Auction, now: datetime) -> None:
        """Avant la relecture d'une cible : solde, places de vente et cote mis en cache, pour miser juste
        après la relecture sans autre requête (chacune coûte ~1,5 s, l'enchère vieillirait sous le seuil).
        Pas de cote pour une carte de collection : sa mise n'en dépend pas, et la route des cotes est lente."""
        await self.portfolio(now)
        if auction.rarity in self._resale_rarities and not self._wanted(auction):
            try:
                await self.ctx.prices.quote(auction.catalog_id, auction.rarity, wait=False, shiny=auction.shiny)
            except (RateLimited, ApiError):
                pass

    async def _refresh_target(self, target: Target) -> None:
        aid = target.auction.id
        try:
            # Relecture juste avant de miser, ou d'une mise en tête juste avant la fin : urgente. Une cible misée
            # relue après sa fin ne l'est pas.
            last_look = target.recheck and target.auction.seconds_left() > 0
            target.last_look = target.last_look or last_look
            auction = await self.api.auction(aid, urgent=not target.recheck or last_look)
        except ApiError as exc:
            self.log.debug("cible %s illisible (%s) : abandonnée", target.auction.title, exc)
            self._targets.pop(aid, None)
            self._outcome(target.auction, "illisible")
            self._log_collection_outcomes([target.auction])
            return
        now = utcnow()
        await self._remember([auction], now)
        await self._track_outbids([auction], self.api.user_id)
        if auction.status != "active" or auction.seconds_left(now) <= 0:
            self._targets.pop(aid, None)  # terminée : le règlement (_settle) s'en charge
            return
        me = self.api.user_id
        if target.recheck and me and (auction.current_bidder_id or "").lower() == me:
            self._targets[aid] = Target(auction, now, recheck=True, last_look=target.last_look)  # toujours en tête
            return
        if target.recheck:
            if auction.end_at > target.auction.end_at + timedelta(seconds=5):  # prolongée (60 s par mise tardive)
                self.log.info("%s prolongée jusqu'à %s : rejouée", auction.title,
                              f"{auction.end_at.astimezone(timeutils.PARIS):%H:%M:%S}")
            else:
                self.log.info("%s surenchérie à %.0f s de la fin : rejouée", auction.title, auction.seconds_left(now))
        self._targets[aid] = Target(auction, now)

    async def _prefetch(self, now: datetime) -> bool:
        """Cote à l'avance une cible de revente qui finit dans les 15 min (route des cotes limitée).
        Renvoie True si une requête est partie."""
        if now < self._next_prefetch:
            return False
        self._next_prefetch = now + timedelta(seconds=self.settings.sniper_prefetch_seconds)
        soon = sorted((t.auction for t in self._targets.values()
                       if not t.recheck and t.auction.rarity in self._resale_rarities
                       and t.auction.seconds_left(now) <= 900), key=lambda a: a.end_at)
        for a in soon:
            if await self.ctx.prices.is_cached(a.catalog_id):
                continue
            try:
                await self.ctx.prices.quote(a.catalog_id, a.rarity, wait=False, shiny=a.shiny)
            except (RateLimited, ApiError):
                pass
            return True
        return False

    def _delay(self, params: SnipeParams, now: datetime) -> float:
        """Rythme normal, raccourci pour relire une cible dès son entrée dans la fenêtre de mise."""
        due = self._next_due(params, now)
        guard = self._collection_soon(params, now)
        if due is not None and (guard is None or due.recheck or self._wanted(due.auction)):
            return 0.0  # une autre cible attend (les requêtes restent espacées par le client)
        if guard is not None:
            return guard + 0.1  # réveil à l'entrée de la carte de collection dans sa fenêtre de mise
        delay = random.uniform(self.settings.sniper_poll_min_seconds, self.settings.sniper_poll_max_seconds)
        last_look = self.settings.sniper_min_seconds_left + 1 + LAST_LOOK_SECONDS
        for t in self._targets.values():
            left = t.auction.seconds_left(now)
            if not t.recheck and left > params.window_seconds:
                delay = min(delay, left - params.window_seconds + 0.5)
            elif t.recheck and not t.last_look and t.auction.id in self._leading and left > last_look:
                delay = min(delay, left - last_look + 0.5)  # réveil pour la dernière relecture
        return delay

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
            if self._check_quota_spent(decision):
                self._outcome(auction, QUOTA_FOR_L)
                continue
            if self._antibot_pending(decision):
                self.log.debug("%s : vérification anti-bot en attente, mise pas tentée", auction.title)
                self._outcome(auction, PENDING_ANTIBOT)
                self._antibot_missed += 1
                self._antibot_remind()
                continue
            if await self._bid(auction, decision, params):
                self._outcome(auction, "misée (collection)" if decision.collection else "misée")
                if not rebid:
                    free -= 1
                    opened += 1
            else:
                self._outcome(auction, "mise refusée : vérification anti-bot" if self._antibot_since
                              else "mise refusée par le site")
        if self._shadow_on:
            await self._play_shadow([a for a in auctions if self._outcomes.get(a.id) not in PLACED], params, now)

    async def _play_shadow(self, auctions: list[Auction], params: SnipeParams, now: datetime) -> None:
        """Sniper de test : simule les achats que le sniper réel n'a pas faits (plafonds du test), avec ses propres
        limites, pour continuer à mesurer la stratégie. Jamais une carte de collection ni une vente déjà misée."""
        s = self.settings
        portfolio = await self.shadow_portfolio(now)
        shadow = replace(params, max_bid=s.sniper_shadow_max_bid)
        candidates: list[tuple[float, Auction, Decision]] = []
        for auction in auctions:
            if auction.rarity not in self._resale_rarities or self._wanted(auction) \
                    or self.is_manual_interest(auction) or await self.db.last_bid(auction.id) is not None:
                continue
            quote = await self.ctx.prices.quote(auction.catalog_id, auction.rarity, shiny=auction.shiny, fetch=False)
            decision = decide(auction, shadow, quote, s.price_min_sales, portfolio, s.sale_fee_pct, resale=True,
                              push=s.sniper_bid_push)
            if decision is not None and not decision.collection:
                candidates.append((decision.profit or 0.0, auction, decision))
        opened = spent = 0
        for _, auction, decision in sorted(candidates, key=lambda c: c[0], reverse=True):
            open_now = portfolio.open_positions + opened
            if open_now >= portfolio.max_positions or auction.seconds_left() <= s.sniper_min_seconds_left:
                continue
            if open_now >= portfolio.sale_room and \
                    (decision.profit or 0) < max(portfolio.min_profit, portfolio.queue_min_profit):
                continue
            if decision.amount > portfolio.available - spent:  # solde simulé : réel moins les engagements simulés
                continue
            if await self._bid(auction, decision, shadow, simulate=True):
                self._outcomes[auction.id] = SHADOW_PLACED
                opened += 1
                spent += decision.amount

    async def portfolio(self, now: datetime) -> Portfolio:
        held = await self.db.positions_summary()
        engaged_real = sum(amount for _, amount in self._leading.values())
        pending = await self.db.pending_bids()  # mises pas encore réglées, même après la fin de l'enchère
        cap, room = await self._sale_capacity(now)
        # Mises réelles : seules les positions réelles comptent dans le plafond. Les positions simulées d'avant
        # le passage en réel (6 le 16/10) bloqueraient sinon tout achat avec PORTFOLIO_MAX_POSITIONS=5.
        live = self.settings.is_live("place_bid")
        held_count = held["real_count"] if live else held["count"]
        engaged_count = pending["real_positions"] if live else pending["positions"]
        self._buys_left = None
        if live and self.settings.sniper_max_buys > 0:
            # Achats permis épuisés : plus aucune nouvelle position, même quand une revente libère une place.
            bought = await self.db.real_position_buys() + pending["real_positions"]
            self._buys_left = max(0, self.settings.sniper_max_buys - bought)
            cap = min(cap, held_count + engaged_count + self._buys_left)
        return portfolio_state(
            self.settings, balance=await self._current_balance(), held_count=held_count,
            held_real_cost=held["real_cost"], held_sim_cost=held["sim_cost"],
            engaged_real=engaged_real, engaged_sim=pending["sim_cost"], bot_engaged_count=engaged_count,
            max_positions=cap, sale_room=room,
        )

    async def shadow_portfolio(self, now: datetime) -> Portfolio:
        """Positions du sniper de test : simulées seulement, avec leur propre plafond."""
        held = await self.db.positions_summary()
        pending = await self.db.pending_bids()
        engaged_real = sum(amount for _, amount in self._leading.values())
        return portfolio_state(
            self.settings, balance=await self._current_balance(), held_count=held["count"] - held["real_count"],
            held_real_cost=held["real_cost"], held_sim_cost=held["sim_cost"], engaged_real=engaged_real,
            engaged_sim=pending["sim_cost"], bot_engaged_count=pending["positions"] - pending["real_positions"],
            max_positions=self.settings.sniper_shadow_max_positions, sale_room=self.settings.portfolio_slots,
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
        self._day_titles = collection_targets(self.ctx)

    def _log_collection_sales(self, auctions: list[Auction], now: datetime) -> None:
        """Journalise une fois chaque vente d'une carte de collection manquante repérée sur le marché."""
        for a in auctions:
            if a.id in self._collection_logged or collection_key(a.title) not in self._collection_wanted:
                continue
            self._collection_logged[a.id] = None
            left = a.seconds_left(now)
            self._days_found += left > 0
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
        # Compteur absent de la réponse (le 16/10, site surchargé : TypeError à chaque passage pendant 5 min) :
        # pas de vente manuelle déduite, comme l'inventaire qui compte alors les ventes du bot.
        manual_sales = 0 if selling is None else max(0, selling - await self.db.posted_listings_count())
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
            "auction_id": a.id, "card_id": a.catalog_id, "title": a.title,
            "rarity": a.rarity, "is_shiny": int(a.shiny), "current_bid": a.current_bid,
            "base_amount": a.base_amount, "min_bid": a.min_bid, "end_at": a.end_at.isoformat(),
            "seen_at": now.isoformat(), "seller_id": a.seller_id, "bidder_id": a.current_bidder_id,
            "is_gif": int(bool(a.card and a.card.is_gif)),
        } for a in auctions])

    async def _track_outbids(self, auctions: list[Auction], me: str | None) -> None:
        """Mise réelle dépassée : un autre meneur, connu, au-dessus de notre mise. Meneur absent, ou lecture plus
        ancienne que notre mise (prix affiché sous le nôtre) : rien, le règlement à la fin tranchera."""
        for a in auctions:
            mine = self._leading.get(a.id)
            bidder = (a.current_bidder_id or "").lower()
            if mine and me and bidder and bidder != me and (a.current_bid is None or a.current_bid > mine[1]):
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
        resale = auction.rarity in self.settings.sniper_resale_rarities  # SNIPER_SHADOW_RARITIES : test seulement
        if resale:
            # Sans requête : la cote a été mise en cache avant la relecture (_prepare, _prefetch). Une requête
            # ici ferait vieillir l'enchère, et les suivantes du même passage, sous le seuil de mise.
            quote = await self.ctx.prices.quote(auction.catalog_id, auction.rarity, shiny=auction.shiny, fetch=False)
        else:
            # Rareté jamais revendue : la cote ne sert que de repère, prise dans les ventes observées (sans requête).
            quote = (await self.ctx.prices.observed_quote(auction.catalog_id)).get(auction.rarity or "")
        collection_cap = await self._collection_cap(auction)
        # Déjà surenchérie : un rival suit nos mises d'un pas, la suivante va droit au plafond (une seule de plus).
        rebid = last is not None and last["status"] in ("placed", "outbid")
        decision = decide(auction, params, quote, self.settings.price_min_sales, portfolio, self.settings.sale_fee_pct,
                          collection_cap=collection_cap, resale=resale,
                          push=1.0 if rebid else self.settings.sniper_bid_push)
        if decision is None:
            self._outcome(auction, "collection : trop chère" if collection_cap is not None else
                          refusal(auction, params, quote, self.settings.price_min_sales, portfolio,
                                  self.settings.sale_fee_pct, resale))
        return decision

    def _outcome(self, auction: Auction, outcome: str) -> None:
        """Retient l'issue d'une enchère ; une mise placée n'est plus remplacée par un refus ultérieur."""
        if self._outcomes.get(auction.id) not in PLACED:
            self._outcomes[auction.id] = outcome

    def _check_quota_spent(self, decision: Decision) -> bool:
        """Mise de collection à ne pas faire : sa part des mises permises par la vérification est prise.
        Le 17/10, chaque vérification a laissé passer 9 ou 10 mises, puis le site l'a redemandée."""
        cap = self.settings.collection_bids_per_check
        return (decision.collection and cap > 0 and self.settings.is_live("collection_bid")
                and self._collection_since_check >= cap)

    def _antibot_pending(self, decision: Decision) -> bool:
        """Mise réelle à ne pas tenter : la vérification anti-bot attend et la dernière tentative est trop récente."""
        live = self.settings.is_live("collection_bid" if decision.collection else "place_bid")
        return live and self._antibot_since is not None and utcnow() < self._antibot_retry_at

    def _antibot_remind(self) -> None:
        """Renouvelle l'alerte toutes les ANTIBOT_REMIND tant que la vérification n'est pas faite (la première,
        la nuit, passe facilement inaperçue : le 17/10 le blocage a duré plus de 11 h)."""
        now = utcnow()
        if not self._antibot_since or now < self._antibot_remind_at:
            return
        self._antibot_remind_at = now + ANTIBOT_REMIND
        minutes = int((now - self._antibot_since).total_seconds() // 60)
        since = self._antibot_since.astimezone(timeutils.PARIS)
        self.notifier.critical(
            "Sniper : vérification anti-bot toujours en attente",
            f"Mises refusées depuis {since:%d/%m %H:%M} ({minutes // 60} h {minutes % 60:02d}) : "
            f"{self._antibot_missed} mise(s) non placée(s) depuis. Fais la vérification dans ton navigateur "
            "(mise à la main sur le marché, ou page Paquets) : les mises repartent dès qu'elle est faite.")

    @property
    def blocked(self) -> str | None:
        if not self._antibot_since:
            return None
        return (f"mises refusées depuis {self._antibot_since.astimezone(timeutils.PARIS):%d/%m %H:%M} : "
                "vérification anti-bot à faire dans ton navigateur")

    def recap(self) -> list[str]:
        outcomes, self._outcomes = self._outcomes, {}
        watched = "/".join(self._resale_rarities)
        found = f"Repérage : {len(self._targets)} vente(s) suivie(s) ({watched} et jours manquants), " \
                f"{self._days_found} vente(s) de jours manquants trouvée(s)"
        if self._gaps:
            found += f", {self._gaps} trou(s) dans les nouvelles ventes"
        self._gaps = self._days_found = 0
        if not outcomes:
            return [found, "Aucune enchère dans la fenêtre de mise."]
        counts: dict[str, int] = {}
        for outcome in outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return [found, f"{len(outcomes)} enchère(s) examinée(s) : " + " · ".join(f"{o} {n}" for o, n in ranked)]

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

    async def _bid(self, auction: Auction, decision: Decision, params: SnipeParams, *, retried: bool = False,
                   simulate: bool = False) -> bool:
        """Place la mise ; renvoie True si elle est acceptée (ou simulée). `simulate` : sniper de test."""
        # Mises de positions (« sniper ») et de collection (« collection ») : réelles ou simulées séparément.
        live = not simulate and self.settings.is_live("collection_bid" if decision.collection else "place_bid")
        common = dict(
            auction_id=auction.id, card_id=auction.catalog_id, title=auction.title, rarity=auction.rarity,
            is_shiny=auction.shiny, amount=decision.amount, reference_price=decision.reference,
            reason=decision.reason, night=params.night, dry_run=not live, end_at=auction.end_at,
            ceiling=decision.ceiling,
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
            if self._antibot_since:
                self._antibot_since, self._antibot_missed = None, 0
                self.notifier.success("Sniper : vérification levée", "Les mises passent de nouveau.")
            if decision.collection and live:
                self._collection_since_check += 1
            self._engaged[auction.id] = auction.end_at
            self._bot_engaged.add(auction.id)
            self._report_bid("Enchère placée", f"{summary}, solde {confirmed.bidder_balance}")
            return True

        server_min = payload.get("min")
        error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
        await self.db.record_bid(**common, status="rejected", note=str(error)[:300])
        self.log.info("mise refusée sur %s : %s", label, error)
        if ANTIBOT_HINT in str(error).lower() or payload.get("human_verification_required"):
            self._antibot_retry_at = utcnow() + ANTIBOT_RETRY
            self._antibot_missed += 1
            if self._antibot_since:
                self._antibot_remind()
            else:
                self._antibot_since = utcnow()
                self._antibot_remind_at = self._antibot_since + ANTIBOT_REMIND
                self._collection_since_check = 0  # la prochaine vérification ouvre un nouveau quota
                # Le 16/10, les paquets s'ouvraient encore pendant que les mises étaient refusées : la page
                # Paquets ne la propose pas forcément.
                self.notifier.critical(
                    "Sniper : vérification anti-bot demandée",
                    f"Le site refuse les mises tant qu'elle n'est pas faite ({label} : {decision.amount} WB "
                    "refusée). Fais-la toi-même dans ton navigateur : mise à la main sur le marché, ou page "
                    f"Paquets si elle la demande. En attendant, le bot ne tente qu'une mise toutes les "
                    f"{ANTIBOT_RETRY.seconds // 60} min et te prévient dès qu'une mise repasse.")
            return False
        if not retried and isinstance(server_min, int) and decision.amount < server_min <= decision.ceiling:
            # Un rival a misé entre la lecture et la mise : une position repart au-dessus du nouveau minimum.
            if decision.profit is None:
                retry = Decision(server_min, decision.ceiling, decision.reason, decision.reference)
            else:
                amount = pushed(server_min, decision.ceiling, self.settings.sniper_bid_push)
                profit = decision.profit - (amount - decision.amount)
                retry = Decision(amount, decision.ceiling, f"revente ~{decision.reference:.0f} WB, bénéfice "
                                 f"~{profit:.0f}", decision.reference, profit)
            return await self._bid(auction, retry, params, retried=True)
        return False

    def _report_bid(self, title: str, summary: str, *, simulated: bool = False) -> None:
        self.log.info("%s : %s", title, summary)
        if simulated and not self.settings.notify_simulated:
            return  # sniper de test : logs, récap et bilan seulement
        if self.settings.notify_each_bid:
            self.notifier.info(title, summary)
        else:
            self._digest.append((simulated, summary))

    async def _settle(self) -> None:
        """Résultat des enchères échues. En DRY_RUN : gagnée si personne n'a payé plus que notre mise."""
        cutoff = utcnow() - timedelta(seconds=self.settings.sniper_settle_grace_seconds)
        me = self.api.user_id
        for row in await self.db.bids_to_settle(cutoff, limit=3, outbid_since=utcnow() - OUTBID_SETTLE_WINDOW):
            auction = await self._settled_auction(row)
            if auction is None:
                continue
            if auction.status == "active":
                await self.db.update_bid(row["id"], end_at=auction.end_at)  # prolongée
                continue
            simulated = row["status"] == "simulated"
            final = auction.final_price if auction.final_price is not None else auction.current_bid
            if simulated:
                won = auction.status == "settled_unsold" or final is None or final < row["amount"]
            else:
                winner = (auction.winner_id or auction.current_bidder_id or "").lower()
                won = auction.status == "settled_sold" and me is not None and winner == me
            await self._close_bid(row, won, simulated, auction.card, final=final)

    async def _settled_auction(self, row) -> Auction | None:
        """Relit l'enchère d'une mise. Disparue du site (404 « Enchère introuvable », relevé du 15/10 au soir) :
        la mise est close comme perdue, au lieu de faire échouer le passage du sniper à chaque règlement."""
        try:
            return await self.api.auction(row["auction_id"])
        except ApiError as exc:
            if exc.status != 404:
                raise
        simulated = row["status"] == "simulated"
        label = f"{row['title']} ({row['rarity']}), {row['amount']} WB"
        self.log.info("%s : enchère introuvable sur le site, mise close comme perdue", label)
        await self._close_bid(row, False, simulated)
        await self.db.update_bid(row["id"], note="enchère introuvable sur le site")
        if not simulated and row["status"] != "outbid":  # déjà comptée perdue : pas d'alerte
            self.notifier.warning("Mise à vérifier", f"{label} : enchère introuvable sur le site, comptée perdue. "
                                  "Vérifie dans ta collection si la carte est arrivée.")
        return None

    async def _close_bid(self, row, won: bool, simulated: bool, card: Card | None = None,
                         final: int | None = None) -> None:
        """Enregistre le résultat d'une enchère terminée (et son prix final) ; une carte gagnée devient une position."""
        status = ("sim_won" if won else "sim_lost") if simulated else ("won" if won else "lost")
        # Un achat « collection » est gardé : ce n'est pas une position à revendre.
        closed = int(won and str(row["reason"] or "").startswith(COLLECTION_PREFIX))
        await self.db.update_bid(row["id"], status=status, settled_at=utcnow(), closed=closed, final_price=final)
        self._leading.pop(row["auction_id"], None)
        self._engaged.pop(row["auction_id"], None)
        if not won:
            return
        link = card.page_url if card else None
        name = f"**[{row['title']}]({link})**" if link else f"**{row['title']}**"
        look = {"thumbnail": card.image_url if card else None, "url": link}
        if closed:
            # Plus manquante dès maintenant : une autre vente de la même carte ne doit pas être misée d'ici
            # la prochaine lecture de la collection.
            self._collection_missing.discard(collection_key(row["title"]))
            if not simulated:
                await record_win(self.ctx, row["title"])
            notify = self.notifier.info if simulated else self.notifier.success
            title = "Collection : carte obtenue (simulé)" if simulated else "Collection : carte obtenue !"
            if simulated and not self.settings.notify_simulated:
                self.log.info("%s : %s", title, row["title"])
                return
            notify(title, f"{name}, gardée dans la collection", topic=Topic.COLLECTION, **look,
                   rareté=row["rarity"] or "?", prix=f"{row['amount']} WB")
            return
        ref = row["reference_price"]
        fields = {"rareté": row["rarity"] or "?", "mise": f"{row['amount']} WB"}
        if ref:
            fields["revente visée"] = f"~{ref:.0f} WB"
            fields["bénéfice attendu"] = f"~{ref - row['amount']:.0f} WB"
        fields["nuit"] = "oui" if row["night"] else "non"
        if simulated:
            if self.settings.notify_simulated:
                self.notifier.info("Achat simulé remporté", name, **look, **fields)
            else:
                self.log.info("achat simulé remporté : %s (%s), %d WB", row["title"], row["rarity"], row["amount"])
        else:
            self.notifier.success("Position achetée !", name, important=True, **look, **fields)

    async def _reconcile(self) -> None:
        """Mises au résultat incertain (réponse perdue ou inattendue) : relues sur le serveur, jamais rejouées."""
        me = self.api.user_id
        if me is None:
            return
        for row in await self.db.uncertain_bids(utcnow() - timedelta(seconds=30)):
            auction = await self._settled_auction(row)
            if auction is None:
                continue
            leader = (auction.winner_id or auction.current_bidder_id or "").lower()
            label = f"{row['title']} ({row['rarity']}), {row['amount']} WB"
            if auction.status != "active":
                won = auction.status == "settled_sold" and leader == me
                await self._close_bid(row, won, simulated=False, card=auction.card,
                                      final=auction.final_price if auction.final_price is not None
                                      else auction.current_bid)
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
            self.notifier.info("Mise incertaine vérifiée", f"{label} : {verdict}.", important=True)
