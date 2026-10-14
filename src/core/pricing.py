"""Cote moyenne d'une carte, par rareté, avec cache SQLite.

Source : /api/marketplace/cards/{id}/sales?scope=summary [KZ bridge/prices.js, WR api.js].
Pour un compte non PRO, la réponse réelle ne contient que `average` (pas de `count`) :
le nombre de ventes est alors inconnu et PRICE_MIN_SALES ne peut pas s'appliquer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from src.config import RARITIES
from src.core.api import WikiMastersAPI
from src.core.database import Database, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    average: float | None
    count: int | None  # None = inconnu (compte non PRO)

    def reliable(self, min_sales: int) -> bool:
        return self.average is not None and (self.count is None or self.count >= min_sales)


class PriceService:
    def __init__(self, api: WikiMastersAPI, db: Database, default_ttl_minutes: float):
        self.api = api
        self.db = db
        self.default_ttl = timedelta(minutes=default_ttl_minutes)

    async def quotes(self, card_id: str, *, max_age: timedelta | None = None, wait: bool = True) -> dict[str, Quote]:
        max_age = max_age or self.default_ttl
        cached = await self.db.price_get(card_id)
        if cached and utcnow() - cached[0] <= max_age:
            data = cached[1]
        else:
            summary = await self.api.sales_summary(card_id, wait=wait)
            data = {
                r: (s.average, s.count)
                for r, s in summary.summary.items()
                if r in RARITIES and (s.average is None or s.average >= 0)
            }
            await self.db.price_put(card_id, data)
        return {r: Quote(avg, None if cnt is None else int(cnt)) for r, (avg, cnt) in data.items() if r in RARITIES}

    async def quote(self, card_id: str, rarity: str | None, *, max_age: timedelta | None = None,
                    wait: bool = True) -> Quote:
        if not rarity:
            return Quote(None, 0)
        return (await self.quotes(card_id, max_age=max_age, wait=wait)).get(rarity, Quote(None, 0))
