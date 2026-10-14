"""Façade typée au-dessus du client : les workers ne manipulent que des modèles."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from src.core.client import MutationResult, WikiMastersClient
from src.core.models import (
    Auction, AuctionDetail, Balance, CollectionPage, CollectionStats, MarketPage, OwnedCard,
    SalesSummary, TradesList,
)

log = logging.getLogger(__name__)

COLLECTION_PAGE_SIZE = 50
MAX_COLLECTION_PAGES = 400
BULK_DISCARD_MAX = 50


class WikiMastersAPI:
    def __init__(self, client: WikiMastersClient):
        self.client = client

    @property
    def user_id(self) -> str | None:
        return self.client.user_id

    # ------------------------------------------------------------- lecture
    async def balance(self) -> int:
        return Balance.model_validate(await self.client.get("balance")).balance

    async def collection_stats(self) -> CollectionStats:
        return CollectionStats.model_validate(await self.client.get("collection_stats"))

    async def collection_page(self, page: int, sort: str = "added", q: str = "") -> CollectionPage:
        return CollectionPage.model_validate(await self.client.get("collection_page", page=page, sort=sort, q=q))

    async def iter_collection(self) -> AsyncIterator[OwnedCard]:
        """Parcourt toute la collection (pages de 50 à partir de 0) [WR api.js loadCollection]."""
        seen: set[str] = set()
        for page in range(MAX_COLLECTION_PAGES):
            result = await self.collection_page(page)
            raw_count = len(result.collection)
            for row in result.rows():
                if row.id in seen:
                    # Un doublon entre pages signifie que la collection a bougé pendant la lecture.
                    raise RuntimeError("la collection a changé pendant la lecture, réessayer plus tard")
                seen.add(row.id)
                yield row
            if raw_count < COLLECTION_PAGE_SIZE:
                return
        log.warning("Collection tronquée à %d pages", MAX_COLLECTION_PAGES)

    async def load_collection(self) -> list[OwnedCard]:
        return [row async for row in self.iter_collection()]

    async def market_page(self, *, page: int = 1, sort: str = "ending_soon", q: str = "",
                          limit: int = 50) -> tuple[list[Auction], bool]:
        result = MarketPage.model_validate(
            await self.client.get("market_page", page=page, sort=sort, q=q, limit=limit)
        )
        return result.items(), result.hasMore

    async def auction(self, auction_id: str) -> Auction:
        return AuctionDetail.model_validate(await self.client.get("auction_detail", auction_id=auction_id)).auction

    async def sales_summary(self, card_id: str) -> SalesSummary:
        return SalesSummary.model_validate(await self.client.get("sales_summary", card_id=card_id))

    async def active_trades(self) -> TradesList:
        return TradesList.model_validate(await self.client.get("active_trades"))

    async def my_listings_raw(self) -> Any:
        return await self.client.get("my_listings")

    async def protected_owned_ids(self, owned_ids: set[str]) -> set[str]:
        """Exemplaires engagés dans un échange en attente ou déjà en vente : jamais vendus ni défaussés."""
        protected: set[str] = set()
        for trade in (await self.active_trades()).trades:
            if trade.status == "pending":
                protected.update(i.user_card_id for i in trade.items if i.user_card_id)
        # Comme KZ bridge/marketplace.js : la structure de /mine n'est pas documentée,
        # on cherche donc les identifiants d'exemplaires dans le texte de la réponse.
        mine_text = str(await self.my_listings_raw())
        protected.update(oid for oid in owned_ids if oid in mine_text)
        return protected

    # ------------------------------------------------------------ écriture
    async def open_pack(self) -> MutationResult:
        return await self.client.mutate("open_pack")

    async def place_bid(self, auction_id: str, amount: int) -> MutationResult:
        return await self.client.mutate("place_bid", auction_id=auction_id, amount=int(amount))

    async def create_listing(self, owned_card_id: str, base_amount: int, duration_minutes: int) -> MutationResult:
        return await self.client.mutate(
            "create_listing", owned_card_id=owned_card_id,
            base_amount=int(base_amount), duration_minutes=int(duration_minutes),
        )

    async def bulk_discard(self, owned_card_ids: list[str]) -> MutationResult:
        if not 0 < len(owned_card_ids) <= BULK_DISCARD_MAX:
            raise ValueError(f"1 à {BULK_DISCARD_MAX} exemplaires par lot")
        return await self.client.mutate("bulk_discard", owned_card_ids=list(owned_card_ids))
