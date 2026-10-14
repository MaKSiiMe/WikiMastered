"""Estimation de la valeur de revente d'une carte, avec cache SQLite.

Le prix suit surtout les tendances (actualité, pop culture, mèmes) plus que la rareté :
l'estimation privilégie donc la **médiane des dernières ventes réelles**
(/api/marketplace/cards/{id}/sales, [WP market.js]), plus réactive qu'une moyenne.
Repli : la moyenne par rareté de ?scope=summary [KZ bridge/prices.js] ; pour un compte
non PRO elle ne contient pas `count`, le nombre de ventes est alors inconnu.

Clés du cache : une rareté (C…L) quand les ventes la précisent, sinon "*" (toutes raretés).
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
    count: int | None      # nombre de ventes connues ; None = inconnu (compte non PRO)

    def reliable(self, min_sales: int) -> bool:
        return self.average is not None and (self.count is None or self.count >= min_sales)


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

    async def quotes(self, card_id: str, *, max_age: timedelta | None = None, wait: bool = True) -> dict[str, Quote]:
        max_age = max_age or self.default_ttl
        cached = await self.db.price_get(card_id)
        if cached and utcnow() - cached[0] <= max_age:
            data = cached[1]
        else:
            data = await self._fetch(card_id, wait)
            await self.db.price_put(card_id, data)
        return {r: Quote(avg, None if cnt is None else int(cnt)) for r, (avg, cnt) in data.items()
                if r in RARITIES or r == ANY}

    async def is_cached(self, card_id: str) -> bool:
        cached = await self.db.price_get(card_id)
        return bool(cached and utcnow() - cached[0] <= self.default_ttl)

    async def quote(self, card_id: str, rarity: str | None, *, max_age: timedelta | None = None,
                    wait: bool = True) -> Quote:
        quotes = await self.quotes(card_id, max_age=max_age, wait=wait)
        return quotes.get(rarity or "") or quotes.get(ANY) or Quote(None, 0)
