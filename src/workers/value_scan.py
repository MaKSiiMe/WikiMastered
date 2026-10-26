"""Module I : cotes de toute la collection, pour la défausse des derniers exemplaires.

La vente automatique (AUTO_LIST) ne cote que les doublons. La défausse des derniers exemplaires
(DISCARD_LAST_COPY_RARITIES) a besoin de la cote de chaque carte, derniers exemplaires compris
(Database.known_values). Toutes les VALUE_SCAN_HOURS heures, ce worker, sans aucun message :
1. lit toute la collection ;
2. cote les cartes à la suite, en requêtes de fond (seulement quand aucune autre requête n'attend : le sniper passe
   toujours avant), au plus une toutes les VALUE_SCAN_LOOKUP_SECONDS s. La route des cotes est limitée par le site :
   refusée, le rythme ralentit de moitié, puis réaccélère après une série de cotes acceptées. Les cartes jamais
   cotées passent d'abord, les plus vues sur Wikipédia en tête, puis les cotes les plus anciennes. Une cote de moins
   de VALUE_SCAN_MAX_AGE_DAYS jours est gardée ; une carte sans aucune vente est recotée après
   VALUE_SCAN_UNSOLD_DAYS jours ;
3. fige pour toujours la cote d'une carte sous VALUE_SCAN_FREEZE_BELOW WB (toutes ses raretés possédées) : elle n'est
   plus jamais redemandée, ni ici ni par la vente des doublons.

Un exemplaire protégé (favori, étiquette PROTECTED_TAGS, en échange, déjà en vente) ne compte pas. Les cartes de
valeur ne sont plus signalées, surveillées sur le marché ni vendues : le 25/10, 15 mises en vente à 60 % de leur cote,
aucune vendue ni même misée (abandonné le 26/10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.account import store_collection
from src.core.client import ApiError, RateLimited
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.pricing import ANY, Quote
from src.core.timeutils import utcnow
from src.workers.base import Worker
from src.workers.inventory_manager import excluded_copies, group_by_card

KV_KEY = "value_scan"
ROUTE = "sales_summary"
START_DELAY = timedelta(minutes=10)  # après un démarrage, laisse d'abord le sniper prendre son rythme
MAX_SPACING = 300.0                  # espacement maximal des cotes quand la route est limitée
SPEED_UP_AFTER = 20                  # cotes acceptées de suite avant de réaccélérer…
SPEED_UP = 0.75                      # … doucement : chaque refus met la route en pause ≥ 60 s, sniper compris
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass
class Cycle:
    started: datetime
    groups: dict[str, list[OwnedCard]]
    candidates: list[str]   # cartes dont au moins un exemplaire est libre


def free_copies(copies: list[OwnedCard], excluded: set[str]) -> list[OwnedCard]:
    """Exemplaires que tu pourrais vendre : ni favori ni protégé (étiquette, échange, déjà en vente)."""
    return [c for c in copies if not c.starred and c.id not in excluded]


def owned_rarities(copies: list[OwnedCard]) -> set[str]:
    return {c.rarity or "?" for c in copies}


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


# ---------------------------------------------------------------------- worker
class ValueScan(Worker):
    name = "cotes"
    topic = Topic.SALES

    async def setup(self) -> None:
        s = self.settings
        state = await self.db.kv_get(KV_KEY) or {}
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
        if self._queue:
            return await self.lookup()
        await self.finish()
        return 60.0

    async def start_cycle(self, now: datetime) -> None:
        s = self.settings
        rows = await self.api.load_collection()
        await store_collection(self.ctx, rows)  # valeur du compte (/valeur), sans autre lecture
        groups = group_by_card(rows)
        excluded = await excluded_copies(self.ctx, rows)
        candidates = [cid for cid, copies in groups.items() if free_copies(copies, excluded)]
        views = {cid: max((c.card.pageviews or 0) for c in groups[cid]) for cid in candidates}
        frozen = await self.db.frozen_values()
        done = {cid for cid in candidates if owned_rarities(groups[cid]) <= frozen.get(cid, {}).keys()}
        todo = to_quote(views, await self.db.price_cache_state(), done, now, self._max_age,
                        timedelta(days=s.value_scan_unsold_days))
        self._cycle = Cycle(now, groups, candidates)
        self._queue = todo[:s.value_scan_lookups] if s.value_scan_lookups > 0 else todo
        self._looked_up = self._frozen_now = self._streak = 0
        self._spacing = s.value_scan_lookup_seconds
        self.log.info("collection : %d exemplaires, %d cartes libres (%d à cote figée), %d à coter à ce passage",
                      len(rows), len(candidates), len(done), len(self._queue))

    def _slow_down(self) -> None:
        self._spacing = min(MAX_SPACING, self._spacing * 2)
        self._streak = 0

    def _accepted(self) -> None:
        self._looked_up += 1
        self._streak += 1
        if self._streak >= SPEED_UP_AFTER and self._spacing > self.settings.value_scan_lookup_seconds:
            self._spacing = max(self.settings.value_scan_lookup_seconds, self._spacing * SPEED_UP)
            self._streak = 0

    async def lookup(self) -> float:
        """Une cote demandée au site. Route en pause : la cote attend. Limitée par le site (pas une panne : le 18/10 au
        soir, des 500 en série faisaient tomber l'analyse à une cote toutes les 182 s) : les suivantes sont plus
        espacées."""
        assert self._cycle is not None
        client = self.ctx.client
        if (paused := client.route_paused_for(ROUTE)) > 0:
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
            if client.route_refused(ROUTE):
                self._slow_down()
                self.log.info("cotes limitées par le site : une toutes les %.0f s au mieux désormais", self._spacing)
            return max(self._spacing, client.route_paused_for(ROUTE) + 5)
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

    async def finish(self) -> None:
        """Fin du passage (le suivant dans VALUE_SCAN_HOURS) : les cotes en cache sous le seuil (venues de la vente des
        doublons ou d'un passage interrompu) sont figées, sans aucune requête."""
        cycle = self._cycle
        assert cycle is not None
        frozen = await self.db.frozen_values()
        priced = 0
        for cid in cycle.candidates:
            if owned_rarities(cycle.groups[cid]) <= frozen.get(cid, {}).keys():
                priced += 1
                continue
            quotes = await self.ctx.prices.quotes(cid, max_age=self._max_age, fetch=False)
            priced += any(q.average is not None for q in quotes.values())
            await self._maybe_freeze(cid, quotes)
        self._last_start, self._cycle = cycle.started, None
        self._next_run = cycle.started + timedelta(hours=self.settings.value_scan_hours)
        await self.db.kv_set(KV_KEY, {"started_at": cycle.started.isoformat()})
        self.log.info("cotes de la collection : %d/%d cartes cotées ; %d cote(s) demandée(s) au site, %d figée(s)",
                      priced, len(cycle.candidates), self._looked_up, self._frozen_now)
