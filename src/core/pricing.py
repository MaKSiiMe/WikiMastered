"""Estimation de la valeur de revente d'une carte.

Le prix suit surtout les tendances (actualité, pop culture, mèmes) plus que la rareté, et
la moyenne du site peut être très datée (relevé réel : GMMTV vendue 157 WB, moyenne 391).
Sources, de la plus fiable à la moins fiable :
1. **Ventes observées** par le sniper lui-même (table observed_sales) : médiane des
   PRICE_RECENT_SALES dernières. Gratuit, sans requête, et à jour des tendances.
2. Historique du site /api/marketplace/cards/{id}/sales [WP market.js] — refusé (403)
   aux comptes non PRO, donc rarement disponible.
3. Moyenne ?scope=summary [KZ bridge/prices.js], sans nombre de ventes hors PRO.

Estimation = médiane observée, plafonnée par la moyenne du site quand elle existe (prudence).
`count` = nombre de ventes observées : sans observation, l'estimation n'est pas fiable.
Les ventes observées sont comparées à rareté égale, et shiny à part : un exemplaire Commun ne
s'estime pas sur les ventes de la même carte en UR ou en shiny.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.config import RARITIES
from src.core.api import WikiMastersAPI
from src.core.client import ApiError, RateLimited
from src.core.database import Database, utcnow
from src.core.models import SaleRow, SalesHistory

log = logging.getLogger(__name__)

ANY = "*"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Quote:
    average: float | None  # estimation de revente (médiane récente ou moyenne)
    count: int | None      # ventes observées (0 = aucune) ; None = inconnu

    def reliable(self, min_sales: int) -> bool:
        return self.average is not None and (self.count is None or self.count >= min_sales)


def combine(observed: list[int], n_observed: int, site_average: float | None) -> Quote:
    """Médiane des ventes observées, plafonnée par la moyenne du site ; sinon la moyenne seule."""
    if observed:
        estimate = float(statistics.median(observed))
        if site_average is not None:
            estimate = min(estimate, site_average)
        return Quote(estimate, n_observed)
    return Quote(site_average, 0)


def recent_estimates(rows: list[SaleRow], recent_n: int) -> dict[str, tuple[float | None, int | None]]:
    """Médiane des `recent_n` dernières ventes, par rareté et toutes raretés confondues."""
    priced = [r for r in rows if r.final_price and r.final_price > 0]
    if any(r.settled_at for r in priced):
        priced.sort(key=lambda r: r.settled_at or _EPOCH, reverse=True)  # plus récentes d'abord
    groups: dict[str, list[int]] = {ANY: [r.final_price for r in priced]}  # type: ignore[misc]
    for r in priced:
        if r.snapshot_rarity in RARITIES:
            groups.setdefault(r.snapshot_rarity, []).append(r.final_price)  # type: ignore[arg-type]
    return {k: (float(statistics.median(v[:recent_n])), len(v)) for k, v in groups.items() if v}


class PriceService:
    def __init__(self, api: WikiMastersAPI, db: Database, default_ttl_minutes: float, recent_n: int = 5):
        self.api = api
        self.db = db
        self.default_ttl = timedelta(minutes=default_ttl_minutes)
        self.recent_n = recent_n

    async def _fetch(self, card_id: str, wait: bool) -> dict[str, tuple[float | None, int | None]]:
        if self.api.client.endpoints.has("sales_history"):
            try:
                history = SalesHistory.model_validate(
                    await self.api.client.get("sales_history", card_id=card_id, wait=wait))
                data = recent_estimates(history.rows(), self.recent_n)
                if data:
                    return data
            except RateLimited:
                raise
            except (ApiError, ValueError) as exc:
                log.debug("historique indisponible pour %s (%s), repli sur la moyenne", card_id, exc)
        summary = await self.api.sales_summary(card_id, wait=wait)
        return {
            r: (s.average, s.count)
            for r, s in summary.summary.items()
            if r in RARITIES and (s.average is None or s.average >= 0)
        }

    async def _market(self, card_id: str, max_age: timedelta, wait: bool) -> dict[str, tuple[float | None, int | None]]:
        cached = await self.db.price_get(card_id)
        if cached and utcnow() - cached[0] <= max_age:
            return cached[1]
        data = await self._fetch(card_id, wait)
        await self.db.price_put(card_id, data)
        return data

    async def quotes(self, card_id: str, *, max_age: timedelta | None = None, wait: bool = True,
                     shiny: bool = False) -> dict[str, Quote]:
        """Estimation par rareté (clé ANY : toutes raretés, seulement si le site ne les distingue pas)."""
        observed = await self.db.observed_by_rarity(card_id, self.recent_n, shiny)
        try:
            market = await self._market(card_id, max_age or self.default_ttl, wait)
        except (RateLimited, ApiError):
            if not observed:
                raise
            market = {}
        keys = {r for r in market if r in RARITIES} | set(observed)
        quotes = {r: combine(*observed.get(r, ([], 0)), market.get(r, (None, None))[0]) for r in keys}
        if not quotes and ANY in market:
            quotes[ANY] = combine([], 0, market[ANY][0])
        return quotes

    async def observed_quote(self, card_id: str) -> dict[str, Quote]:
        """Estimation sans aucune requête : ventes observées seules, par rareté (vide si aucune)."""
        return {r: combine(prices, n, None) for r, (prices, n) in
                (await self.db.observed_by_rarity(card_id, self.recent_n)).items()}

    async def is_fresh(self, card_id: str, max_age: timedelta) -> bool:
        cached = await self.db.price_get(card_id)
        return bool(cached and utcnow() - cached[0] <= max_age)

    async def is_cached(self, card_id: str) -> bool:
        cached = await self.db.price_get(card_id)
        return bool(cached and utcnow() - cached[0] <= self.default_ttl)

    async def quote(self, card_id: str, rarity: str | None, *, max_age: timedelta | None = None,
                    wait: bool = True, shiny: bool = False) -> Quote:
        """Estimation pour cette rareté (et shiny ou non) ; jamais celle d'une autre rareté."""
        quotes = await self.quotes(card_id, max_age=max_age, wait=wait, shiny=shiny)
        return (quotes.get(rarity) if rarity else None) or quotes.get(ANY) or Quote(None, 0)
