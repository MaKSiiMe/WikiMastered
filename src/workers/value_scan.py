"""Module I : cartes de valeur oubliées dans l'inventaire.

La vente automatique (AUTO_LIST) ne vend que des doublons, et ne demande la cote que de ceux-là. Une carte Commune
ou Peu commune dont on n'a qu'un exemplaire peut pourtant valoir cher : le prix suit les tendances, pas la rareté.
Toutes les VALUE_SCAN_HOURS heures, ce worker :
1. lit toute la collection ;
2. cote les cartes, une toutes les VALUE_SCAN_LOOKUP_SECONDS s (la route des cotes est limitée et sert aussi au
   sniper), au plus VALUE_SCAN_LOOKUPS par passage, la cote la plus ancienne d'abord. Une cote de moins de
   VALUE_SCAN_MAX_AGE_DAYS jours est reprise du cache : d'un passage à l'autre, toute la collection finit cotée ;
3. signale sur Discord les cartes cotées au moins VALUE_SCAN_MIN_VALUE pas encore signalées (ou dont la cote a
   monté de moitié depuis), avec les ventes concurrentes en cours. Il ne vend rien : à toi de décider.

Un exemplaire protégé (favori, étiquette PROTECTED_TAGS, en échange, déjà en vente) ne compte pas. La cote d'une
carte Commune vient surtout de la moyenne du site (le bot n'observe que les ventes des raretés revendues), qui peut
être datée : les ventes concurrentes en cours disent ce qu'elle vaut aujourd'hui.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from src.core.client import ApiError, RateLimited
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.pricing import ANY, Quote
from src.core.timeutils import now_local, utcnow
from src.workers.base import Context, Worker
from src.workers.inventory_manager import excluded_copies, group_by_card, spare_copies

KV_KEY = "value_scan"
ROUTE = "sales_summary"
START_DELAY = timedelta(minutes=10)  # après un démarrage, laisse d'abord le sniper prendre son rythme
MAX_SPACING = 300.0                  # espacement maximal des cotes quand la route est limitée
RISE_TO_REPORT = 1.5                 # carte déjà signalée : de nouveau si sa cote a monté de moitié
MAX_LINES = 15                       # cartes détaillées dans le message
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Valuable:
    card_id: str
    title: str
    rarity: str
    value: float
    observed: int       # ventes observées par le bot (0 : moyenne du site seule)
    copies: int         # exemplaires de cette carte dans cette rareté
    spare: bool         # un doublon, que la vente automatique peut vendre ; sinon le dernier exemplaire
    shiny: bool         # seuls des exemplaires shiny sont libres (cote d'un exemplaire normal)
    url: str | None

    @property
    def key(self) -> str:
        return f"{self.card_id}|{self.rarity}"


# ---------------------------------------------------------------- logique pure
def free_copies(copies: list[OwnedCard], excluded: set[str]) -> list[OwnedCard]:
    """Exemplaires que tu pourrais vendre : ni favori ni protégé (étiquette, échange, déjà en vente)."""
    return [c for c in copies if not c.starred and c.id not in excluded]


def find_valuable(groups: dict[str, list[OwnedCard]], quotes: dict[str, dict[str, Quote]], excluded: set[str],
                  min_value: int, keep: int) -> list[Valuable]:
    """Cartes (par rareté) cotées au moins `min_value` et dont un exemplaire est libre, de la plus chère à la moins
    chère. `keep` : exemplaires toujours gardés par la vente automatique (SELL_KEEP_COPIES)."""
    out: list[Valuable] = []
    for card_id, copies in groups.items():
        spare = {c.id for c in spare_copies(copies, keep, excluded)}
        by_rarity: dict[str, list[OwnedCard]] = defaultdict(list)
        for copy in copies:
            by_rarity[copy.rarity or "?"].append(copy)
        for rarity, same in by_rarity.items():
            free = free_copies(same, excluded)
            q = quotes.get(card_id, {}).get(rarity) or quotes.get(card_id, {}).get(ANY)
            if not free or not q or q.average is None or q.average < min_value:
                continue
            out.append(Valuable(card_id, same[0].title, rarity, q.average, q.count or 0, len(same),
                                any(c.id in spare for c in same), all(c.is_shiny for c in free),
                                same[0].card.page_url))
    out.sort(key=lambda v: (-v.value, v.title))
    return out


def to_report(found: list[Valuable], reported: dict[str, float]) -> list[Valuable]:
    """Cartes jamais signalées, ou dont la cote a monté de moitié depuis le dernier signalement."""
    return [v for v in found if (before := reported.get(v.key)) is None or v.value >= before * RISE_TO_REPORT]


def remembered(found: list[Valuable], reported: dict[str, float]) -> dict[str, float]:
    """Cotes à retenir : la plus haute signalée de chaque carte encore de valeur (les autres sont oubliées, et
    signalées de nouveau si elles le redeviennent)."""
    return {v.key: max(v.value, reported.get(v.key, 0.0)) for v in found}


def stale_first(card_ids: list[str], fetched: dict[str, datetime], now: datetime, max_age: timedelta) -> list[str]:
    """Cartes à coter, jamais cotées puis à la cote la plus ancienne d'abord ; les cotes récentes sont gardées."""
    stale = [cid for cid in card_ids if now - fetched.get(cid, _EPOCH) > max_age]
    return sorted(stale, key=lambda cid: (fetched.get(cid, _EPOCH), cid))


def line(v: Valuable, rivals: tuple[int, int | None] | None, auto_list: bool) -> str:
    name = f"[{v.title}]({v.url})" if v.url else v.title
    source = f"{v.observed} vente(s) vue(s)" if v.observed else "moyenne du site"
    if v.spare:
        status = f"doublon ({v.copies} ex.), vendu par la vente auto" if auto_list else f"doublon ({v.copies} ex.)"
    else:
        status = "dernier exemplaire"
    text = f"• **{name}** ({v.rarity}{' ✨' if v.shiny else ''}) : ~{v.value:.0f} WB ({source}) · {status}"
    if rivals is not None:
        count, cheapest = rivals
        text += f" · {count} en vente, dès {cheapest} WB" if count else " · aucune autre en vente"
    return text


# ---------------------------------------------------------------------- worker
class ValueScan(Worker):
    name = "valeur"
    topic = Topic.VALUE

    async def setup(self) -> None:
        s = self.settings
        state = await self.db.kv_get(KV_KEY) or {}
        self._reported: dict[str, float] = state.get("reported", {})
        self._summary: str = state.get("summary", "")
        last = datetime.fromisoformat(state["started_at"]) if state.get("started_at") else None
        # Un passage interrompu (redémarrage) n'est pas compté : il reprend, sans perdre les cotes déjà demandées.
        self._next_run = max(last + timedelta(hours=s.value_scan_hours) if last else _EPOCH, utcnow() + START_DELAY)
        self._max_age = timedelta(days=s.value_scan_max_age_days)
        self._cycle: tuple[datetime, dict[str, list[OwnedCard]], set[str], list[str]] | None = None
        self._queue: list[str] = []
        self._looked_up = 0
        self._spacing = s.value_scan_lookup_seconds

    async def step(self) -> float:
        now = utcnow()
        if self._cycle is None:
            if now < self._next_run:
                return min(1800.0, (self._next_run - now).total_seconds())
            await self.start_cycle(now)
            return self._spacing
        if self._queue:
            return await self.lookup()
        await self.finish()
        return 60.0

    async def start_cycle(self, now: datetime) -> None:
        rows = await self.api.load_collection()
        groups = group_by_card(rows)
        excluded = await excluded_copies(self.ctx, rows)
        candidates = [cid for cid, copies in groups.items() if free_copies(copies, excluded)]
        stale = stale_first(candidates, await self.db.price_fetched_at(), now, self._max_age)
        self._cycle = (now, groups, excluded, candidates)
        self._queue = stale[:self.settings.value_scan_lookups]
        self._looked_up = 0
        self._spacing = self.settings.value_scan_lookup_seconds
        self.log.info("inventaire : %d exemplaires, %d cartes libres, %d sans cote récente, %d à coter à ce passage "
                      "(une toutes les %.0f s)", len(rows), len(candidates), len(stale), len(self._queue),
                      self._spacing)

    async def lookup(self) -> float:
        """Une cote demandée au site. Route limitée : la cote attend, et les suivantes sont plus espacées."""
        client = self.ctx.client
        if (paused := client.route_paused_for(ROUTE)) > 0:
            self._spacing = min(MAX_SPACING, self._spacing * 2)
            return paused + 5
        card_id = self._queue.pop(0)
        try:
            await self.ctx.prices.quotes(card_id, max_age=self._max_age, wait=False)
            # Refusée, mais la carte a des ventes observées : estimation faite sans la cote du site.
            limited = client.route_paused_for(ROUTE) > 0
        except RateLimited:
            limited = True
        except ApiError as exc:
            self.log.debug("cote indisponible pour %s (%s)", card_id, exc)
            limited = False
        if limited:  # limitée par cette requête : la cote sera redemandée
            self._queue.insert(0, card_id)
            self._spacing = min(MAX_SPACING, self._spacing * 2)
            self.log.info("cotes limitées par le site : une toutes les %.0f s désormais", self._spacing)
            return self._spacing
        self._looked_up += 1
        return self._spacing

    async def analyse(self) -> tuple[list[Valuable], int, int]:
        """(cartes de valeur, cartes cotées, cartes libres), sans aucune requête de cote."""
        assert self._cycle is not None
        _, groups, excluded, candidates = self._cycle
        quotes = {cid: await self.ctx.prices.quotes(cid, max_age=self._max_age, fetch=False) for cid in candidates}
        priced = sum(1 for q in quotes.values() if any(x.average is not None for x in q.values()))
        found = find_valuable(groups, quotes, excluded, self.settings.value_scan_min_value,
                              self.settings.sell_keep_copies)
        return found, priced, len(candidates)

    async def finish(self) -> None:
        assert self._cycle is not None
        s = self.settings
        started, *_ = self._cycle
        found, priced, total = await self.analyse()
        fresh = to_report(found, self._reported)
        if fresh:
            self.send_report(fresh, await self._rivals(fresh[:MAX_LINES]), priced, total)
        self._reported = remembered(found, self._reported)
        self._next_run = started + timedelta(hours=s.value_scan_hours)
        self._summary = (f"Dernière analyse le {now_local():%d/%m à %H:%M} : {priced}/{total} cartes cotées, "
                         f"{len(found)} à {s.value_scan_min_value} WB ou plus ({len(fresh)} nouvelle(s))")
        await self.db.kv_set(KV_KEY, {"started_at": started.isoformat(), "reported": self._reported,
                                      "summary": self._summary})
        self.log.info("%s ; %d cote(s) demandée(s) au site", self._summary, self._looked_up)
        self._cycle = None

    async def _rivals(self, cards: list[Valuable]) -> dict[str, tuple[int, int | None]]:
        """Ventes en cours de chaque carte (autres vendeurs, même rareté, pas shiny) : (nombre, moins chère)."""
        me = (self.api.user_id or "").lower()
        now = utcnow()
        out: dict[str, tuple[int, int | None]] = {}
        for v in cards:
            try:
                auctions, _ = await self.api.market_page(sort="ending_soon", q=v.title, limit=50)
            except ApiError as exc:
                self.log.debug("ventes de %s illisibles (%s)", v.title, exc)
                continue
            prices = [a.min_bid for a in auctions if a.catalog_id == v.card_id and a.rarity == v.rarity
                      and not a.shiny and a.status == "active" and a.end_at > now
                      and (a.seller_id or "").lower() != me]
            out[v.key] = (len(prices), min(prices) if prices else None)
        return out

    def send_report(self, fresh: list[Valuable], rivals: dict[str, tuple[int, int | None]], priced: int,
                    total: int) -> None:
        s = self.settings
        lines = [line(v, rivals.get(v.key), s.auto_list) for v in fresh[:MAX_LINES]]
        if len(fresh) > MAX_LINES:
            lines.append(f"… et {len(fresh) - MAX_LINES} autre(s) : `python -m src.main value`")
        coverage = (f"{priced}/{total} cartes cotées (cote de moins de {s.value_scan_max_age_days:g} j), "
                    f"{self._looked_up} demandée(s) au site à ce passage. Rien n'est vendu automatiquement, sauf "
                    "les doublons par la vente auto.")
        self.notifier.info(f"{len(fresh)} carte(s) de valeur dans ton inventaire", "\n".join(lines),
                           sections=[("Cotes", coverage)], important=True)

    def recap(self) -> list[str]:
        if self._cycle is not None:
            return [f"Analyse en cours : {self._looked_up} cote(s) demandée(s), {len(self._queue)} restante(s)"]
        return [self._summary or "Pas encore d'analyse"]


# ------------------------------------------------------------------- CLI
async def cli(ctx: Context, lookups: int) -> str:
    """Toutes les cartes de valeur de l'inventaire, d'après les cotes en cache (+ `lookups` demandées au site)."""
    worker = ValueScan(ctx)
    await worker.setup()
    await worker.start_cycle(utcnow())
    worker._queue = worker._queue[:lookups]
    while worker._queue:
        if (paused := ctx.client.route_paused_for(ROUTE)) > 0:
            await asyncio.sleep(paused + 1)
        await worker.lookup()  # espacées par le client, comme toute requête
    found, priced, total = await worker.analyse()
    s = ctx.settings
    head = (f"{priced}/{total} cartes libres cotées (cote de moins de {s.value_scan_max_age_days:g} j), "
            f"{len(found)} à {s.value_scan_min_value} WB ou plus :")
    return "\n".join([head, *(line(replace(v, url=None), None, s.auto_list).replace("**", "") for v in found)])
