"""Façade typée au-dessus du client : les workers ne manipulent que des modèles."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from src.core.client import MutationResult, WikiMastersClient
from src.core.models import (
    Auction, AuctionDetail, Balance, CollectionPage, CollectionStats, MarketPage, MyBids, MyListingsInfo,
    OwnedCard, SalesSummary, TradesList,
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

    async def iter_collection(self, q: str = "") -> AsyncIterator[OwnedCard]:
        """Parcourt toute la collection (pages de 50 à partir de 0) [WR api.js loadCollection].

        Lire 8 000 exemplaires prend plusieurs minutes : une carte gagnée ou vendue entre-temps décale
        les pages. Un exemplaire revu est ignoré ; un exemplaire manqué ne sera simplement ni vendu ni
        défaussé à ce passage (on ne vend jamais que des exemplaires vus, le dernier est toujours gardé)."""
        seen: set[str] = set()
        repeated = 0
        for page in range(MAX_COLLECTION_PAGES):
            result = await self.collection_page(page, q=q)
            raw_count = len(result.collection)
            for row in result.rows():
                if row.id in seen:
                    repeated += 1
                    continue
                seen.add(row.id)
                yield row
            if raw_count < COLLECTION_PAGE_SIZE:
                break
        else:
            log.warning("Collection tronquée à %d pages", MAX_COLLECTION_PAGES)
        if repeated:
            log.info("collection modifiée pendant la lecture : %d exemplaire(s) revu(s) ignoré(s)", repeated)

    async def load_collection(self, q: str = "") -> list[OwnedCard]:
        return [row async for row in self.iter_collection(q)]

    async def market_page(self, *, page: int = 1, sort: str = "ending_soon", q: str = "",
                          limit: int = 50) -> tuple[list[Auction], bool]:
        result = MarketPage.model_validate(
            await self.client.get("market_page", page=page, sort=sort, q=q, limit=limit)
        )
        return result.items(), result.hasMore

    async def auction(self, auction_id: str) -> Auction:
        return AuctionDetail.model_validate(await self.client.get("auction_detail", auction_id=auction_id)).auction

    async def sales_summary(self, card_id: str, *, wait: bool = True) -> SalesSummary:
        return SalesSummary.model_validate(await self.client.get("sales_summary", card_id=card_id, wait=wait))

    async def my_bids(self) -> list[Auction]:
        """Enchères sur lesquelles j'ai misé (/api/marketplace?mine=1, clé `bidding`)."""
        return MyBids.model_validate(await self.client.get("my_bids")).bid_items()

    async def active_trades(self) -> TradesList:
        return TradesList.model_validate(await self.client.get("active_trades"))

    async def my_listings_raw(self) -> Any:
        return await self.client.get("my_listings")

    async def listing_slots(self) -> tuple[int | None, int]:
        """(ventes en cours, maximum simultané). Le site limite à 5 ventes (relevé du 14/10/2026).
        Ventes en cours : None si le site ne les compte pas."""
        info = MyListingsInfo.model_validate(await self.my_listings_raw())
        return info.sellingCount, info.maxConcurrentAuctions or 5

    async def protected_owned_ids(self, owned_ids: set[str]) -> set[str]:
        """Exemplaires engagés dans un échange en attente : jamais vendus ni défaussés.

        /api/marketplace/mine ne renvoie que des compteurs : les exemplaires déjà en vente
        sont suivis par la base locale (table listings) et refusés par le serveur sinon."""
        protected: set[str] = set()
        for trade in (await self.active_trades()).trades:
            if trade.status == "pending":
                protected.update(i.user_card_id for i in trade.items if i.user_card_id)
        return protected & owned_ids

    # ------------------------------------------------------------ écriture
    async def open_pack(self) -> MutationResult:
        return await self.client.mutate("open_pack")

    async def place_bid(self, auction_id: str, amount: int, *, live: bool | None = None) -> MutationResult:
        return await self.client.mutate("place_bid", live=live, auction_id=auction_id, amount=int(amount))

    async def create_listing(self, owned_card_id: str, base_amount: int, duration_minutes: int) -> MutationResult:
        return await self.client.mutate(
            "create_listing", owned_card_id=owned_card_id,
            base_amount=int(base_amount), duration_minutes=int(duration_minutes),
        )

    async def bulk_discard(self, owned_card_ids: list[str]) -> MutationResult:
        if not 0 < len(owned_card_ids) <= BULK_DISCARD_MAX:
            raise ValueError(f"1 à {BULK_DISCARD_MAX} exemplaires par lot")
        return await self.client.mutate("bulk_discard", owned_card_ids=list(owned_card_ids))
