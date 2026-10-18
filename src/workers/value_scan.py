"""Module I : cartes de valeur oubliées dans l'inventaire.

La vente automatique (AUTO_LIST) ne vend que des doublons, et ne demande la cote que de ceux-là. Une carte Commune
ou Peu commune dont on n'a qu'un exemplaire peut pourtant valoir cher : le prix suit les tendances, pas la rareté.
Toutes les VALUE_SCAN_HOURS heures, ce worker :
1. lit toute la collection ;
2. cote les cartes à la suite, en requêtes de fond (seulement quand aucune autre requête n'attend : le sniper passe
   toujours avant), au plus une toutes les VALUE_SCAN_LOOKUP_SECONDS s. La route des cotes est limitée par le site :
   refusée, le rythme ralentit de moitié, puis réaccélère après une série de cotes acceptées. Les cartes jamais
   cotées passent d'abord, les plus vues sur Wikipédia en tête (le prix suit la popularité), puis les cotes les
   plus anciennes. Une cote de moins de VALUE_SCAN_MAX_AGE_DAYS jours est gardée ; une carte sans aucune vente est
   recotée après VALUE_SCAN_UNSOLD_DAYS jours ;
3. fige pour toujours la cote d'une carte sous VALUE_SCAN_FREEZE_BELOW WB (toutes ses raretés possédées) : elle n'est
   plus jamais redemandée, ni ici ni par la vente des doublons ;
4. signale sur Discord les cartes cotées au moins VALUE_SCAN_MIN_VALUE pas encore signalées (ou dont la cote a
   monté de moitié depuis), avec les ventes concurrentes en cours, à la fin du passage et toutes les 3 h pendant
   un long passage. Il ne vend rien : à toi de décider.

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
SPEED_UP_AFTER = 20                  # cotes acceptées de suite avant de réaccélérer
INTERIM_REPORT = timedelta(hours=3)  # passage long (première analyse) : cartes trouvées signalées en chemin
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


@dataclass
class Cycle:
    started: datetime
    groups: dict[str, list[OwnedCard]]
    excluded: set[str]
    candidates: list[str]   # cartes dont au moins un exemplaire est libre
    report_due: datetime


# ---------------------------------------------------------------- logique pure
def free_copies(copies: list[OwnedCard], excluded: set[str]) -> list[OwnedCard]:
    """Exemplaires que tu pourrais vendre : ni favori ni protégé (étiquette, échange, déjà en vente)."""
    return [c for c in copies if not c.starred and c.id not in excluded]


def owned_rarities(copies: list[OwnedCard]) -> set[str]:
    return {c.rarity or "?" for c in copies}


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


def remembered(found: list[Valuable], reported: dict[str, float], unpriced: set[str]) -> dict[str, float]:
    """Cotes à retenir : la plus haute signalée de chaque carte encore de valeur. Une carte qui ne l'est plus est
    oubliée (signalée de nouveau si elle le redevient), sauf si elle attend sa nouvelle cote (`unpriced`)."""
    kept = {k: v for k, v in reported.items() if k.split("|", 1)[0] in unpriced}
    return kept | {v.key: max(v.value, reported.get(v.key, 0.0)) for v in found}


def frozen_value(quotes: dict[str, Quote], rarities: set[str], below: int) -> dict[str, float] | None:
    """Cotes à figer : chaque rareté possédée a une cote, et toutes sont sous `below` WB ; sinon None."""
    values: dict[str, float] = {}
    for rarity in rarities:
        q = quotes.get(rarity) or quotes.get(ANY)
        if below <= 0 or q is None or q.average is None or q.average >= below:
            return None
        values[rarity] = q.average
    return values or None


def to_quote(cards: dict[str, int], cache: dict[str, tuple[datetime, bool]], frozen: set[str], now: datetime,
             max_age: timedelta, unsold_age: timedelta) -> list[str]:
    """Cartes à coter (`cards` : carte -> vues Wikipédia) : jamais cotées d'abord, les plus vues en tête, puis la cote
    la plus ancienne. Gardées : les cotes figées, récentes, ou sans vente depuis moins de `unsold_age`."""
    def due(cid: str) -> bool:
        if cid in frozen:
            return False
        if cid not in cache:
            return True
        fetched, priced = cache[cid]
        return now - fetched > (max_age if priced else unsold_age)

    return sorted((cid for cid in cards if due(cid)),
                  key=lambda cid: (cid in cache, cache.get(cid, (_EPOCH, False))[0], -cards[cid], cid))


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
        self._last_start = datetime.fromisoformat(state["started_at"]) if state.get("started_at") else None
        # Un passage interrompu (redémarrage) n'est pas compté : il reprend, sans perdre les cotes déjà demandées.
        last = self._last_start + timedelta(hours=s.value_scan_hours) if self._last_start else _EPOCH
        self._next_run = max(last, utcnow() + START_DELAY)
        self._max_age = timedelta(days=s.value_scan_max_age_days)
        self._cycle: Cycle | None = None
        self._queue: list[str] = []
        self._looked_up = 0
        self._frozen_now = 0
        self._spacing = s.value_scan_lookup_seconds
        self._streak = 0

    async def step(self) -> float:
        now = utcnow()
        if self._cycle is None:
            if now < self._next_run:
                return min(1800.0, (self._next_run - now).total_seconds())
            await self.start_cycle(now)
            return self._spacing
        if self._queue and now >= self._cycle.report_due:
            await self.report(final=False)
        if self._queue:
            return await self.lookup()
        await self.report(final=True)
        return 60.0

    async def start_cycle(self, now: datetime) -> None:
        s = self.settings
        rows = await self.api.load_collection()
        groups = group_by_card(rows)
        excluded = await excluded_copies(self.ctx, rows)
        candidates = [cid for cid, copies in groups.items() if free_copies(copies, excluded)]
        views = {cid: max((c.card.pageviews or 0) for c in groups[cid]) for cid in candidates}
        frozen = await self.db.frozen_values()
        done = {cid for cid in candidates if owned_rarities(groups[cid]) <= frozen.get(cid, {}).keys()}
        todo = to_quote(views, await self.db.price_cache_state(), done, now, self._max_age,
                        timedelta(days=s.value_scan_unsold_days))
        self._cycle = Cycle(now, groups, excluded, candidates, now + INTERIM_REPORT)
        self._queue = todo[:s.value_scan_lookups] if s.value_scan_lookups > 0 else todo
        self._looked_up = self._frozen_now = self._streak = 0
        self._spacing = s.value_scan_lookup_seconds
        self.log.info("inventaire : %d exemplaires, %d cartes libres (%d à cote figée), %d à coter à ce passage",
                      len(rows), len(candidates), len(done), len(self._queue))

    def _slow_down(self) -> None:
        self._spacing = min(MAX_SPACING, self._spacing * 2)
        self._streak = 0

    def _accepted(self) -> None:
        self._looked_up += 1
        self._streak += 1
        if self._streak >= SPEED_UP_AFTER and self._spacing > self.settings.value_scan_lookup_seconds:
            self._spacing = max(self.settings.value_scan_lookup_seconds, self._spacing / 2)
            self._streak = 0

    async def lookup(self) -> float:
        """Une cote demandée au site. Route limitée : la cote attend, et les suivantes sont plus espacées."""
        assert self._cycle is not None
        client = self.ctx.client
        if (paused := client.route_paused_for(ROUTE)) > 0:
            self._slow_down()
            return paused + 5
        card_id = self._queue.pop(0)
        try:
            quotes = await self.ctx.prices.quotes(card_id, max_age=self._max_age, wait=False, background=True)
            # Refusée, mais l'estimation est faite (ventes observées, ancienne cote) : cote redemandée.
            limited = client.route_paused_for(ROUTE) > 0
        except RateLimited:
            limited = True
        except ApiError as exc:
            self.log.debug("cote indisponible pour %s (%s)", card_id, exc)
            self._accepted()
            return self._spacing
        if limited:
            self._queue.insert(0, card_id)
            self._slow_down()
            self.log.info("cotes limitées par le site : une toutes les %.0f s au mieux désormais", self._spacing)
            return self._spacing
        self._accepted()
        await self._maybe_freeze(card_id, quotes)
        return self._spacing

    async def _maybe_freeze(self, card_id: str, quotes: dict[str, Quote]) -> bool:
        assert self._cycle is not None
        values = frozen_value(quotes, owned_rarities(self._cycle.groups[card_id]),
                              self.settings.value_scan_freeze_below)
        if values:
            await self.db.freeze_values(card_id, values)
            self._frozen_now += 1
        return bool(values)

    async def analyse(self) -> tuple[list[Valuable], int, set[str]]:
        """(cartes de valeur, cartes cotées, cartes encore sans cote), sans aucune requête de cote. Les cotes en
        cache sous le seuil (venues de la vente des doublons ou d'un passage interrompu) sont figées au passage."""
        cycle = self._cycle
        assert cycle is not None
        frozen = await self.db.frozen_values()
        quotes: dict[str, dict[str, Quote]] = {}
        for cid in cycle.candidates:
            if owned_rarities(cycle.groups[cid]) <= frozen.get(cid, {}).keys():
                quotes[cid] = {r: Quote(v, 0) for r, v in frozen[cid].items()}
                continue
            quotes[cid] = await self.ctx.prices.quotes(cid, max_age=self._max_age, fetch=False)
            await self._maybe_freeze(cid, quotes[cid])
        unpriced = {cid for cid, q in quotes.items() if not any(x.average is not None for x in q.values())}
        found = find_valuable(cycle.groups, quotes, cycle.excluded, self.settings.value_scan_min_value,
                              self.settings.sell_keep_copies)
        return found, len(quotes) - len(unpriced), unpriced

    async def report(self, *, final: bool) -> None:
        """Signale les nouvelles cartes de valeur ; `final` : fin du passage (le suivant dans VALUE_SCAN_HOURS)."""
        cycle = self._cycle
        assert cycle is not None
        s = self.settings
        found, priced, unpriced = await self.analyse()
        fresh = to_report(found, self._reported)
        if fresh:
            self.send_report(fresh, await self._rivals(fresh[:MAX_LINES]), priced, len(cycle.candidates))
        self._reported = remembered(found, self._reported, unpriced)
        self._summary = (f"{'Dernière analyse' if final else 'Analyse en cours'} le {now_local():%d/%m à %H:%M} : "
                         f"{priced}/{len(cycle.candidates)} cartes cotées, {len(found)} à {s.value_scan_min_value} "
                         f"WB ou plus ({len(fresh)} nouvelle(s))")
        if final:
            self._last_start, self._cycle = cycle.started, None
            self._next_run = cycle.started + timedelta(hours=s.value_scan_hours)
        else:
            cycle.report_due = utcnow() + INTERIM_REPORT
        await self.db.kv_set(KV_KEY, {"started_at": self._last_start.isoformat() if self._last_start else None,
                                      "reported": self._reported, "summary": self._summary})
        self.log.info("%s ; %d cote(s) demandée(s) au site, %d figée(s), %d restante(s)", self._summary,
                      self._looked_up, self._frozen_now, len(self._queue))

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
        coverage = f"{priced}/{total} cartes cotées"
        if self._queue:
            coverage += f", {len(self._queue)} encore à coter (analyse en cours)"
        coverage += ". Rien n'est vendu automatiquement, sauf les doublons par la vente auto."
        self.notifier.info(f"{len(fresh)} carte(s) de valeur dans ton inventaire", "\n".join(lines),
                           sections=[("Cotes", coverage)], important=True)

    def recap(self) -> list[str]:
        if self._cycle is not None:
            return [f"Analyse en cours : {self._looked_up} cote(s) demandée(s), {len(self._queue)} restante(s), "
                    f"une toutes les {self._spacing:.0f} s au mieux"]
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
    found, priced, unpriced = await worker.analyse()
    s = ctx.settings
    head = (f"{priced}/{priced + len(unpriced)} cartes libres cotées, {len(found)} à {s.value_scan_min_value} WB "
            "ou plus :")
    return "\n".join([head, *(line(replace(v, url=None), None, s.auto_list).replace("**", "") for v in found)])
