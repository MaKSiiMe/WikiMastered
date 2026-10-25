"""Modèles des réponses JSON WikiMasters.

Les noms de champs sont ceux relevés par les extensions communautaires (voir
config/endpoints.yaml). `extra="allow"` : un champ ajouté par le site ne casse rien.
Un élément de liste invalide est ignoré (et loggé) au lieu de faire échouer la page.
`python -m src.main inspect` compare ces modèles aux vraies réponses.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import RARITY_RANK
from src.core.timeutils import utcnow

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


def parse_items(model: type[M], items: Any, *, label: str) -> list[M]:
    if not isinstance(items, list):
        return []
    parsed: list[M] = []
    for raw in items:
        try:
            parsed.append(model.model_validate(raw))
        except ValidationError as exc:
            log.debug("%s ignoré (%s) : %s", label, exc.error_count(), str(raw)[:200])
    return parsed


def min_next_bid(current_bid: int | None, base_amount: int) -> int:
    """Règle native du site [WR market-api.js] : prix de départ, puis +10 % arrondi au-dessus (min +1)."""
    if current_bid is None:
        return base_amount
    return max(math.ceil(1.1 * current_bid), current_bid + 1)


class Card(_Model):
    id: str
    wikipedia_title: str = ""
    rarity: str | None = None
    category: str | None = None
    pageviews: int | None = None
    image_url: str | None = None
    wikipedia_url: str | None = None
    is_shiny: bool = False

    @property
    def is_gif(self) -> bool:
        """Image animée : le fichier Wikimedia est un GIF (« …/330px-Franks_expansion.gif?utm_source=… », relevé du
        16/10). Rares sur le marché (aucune sur 283 ventes), elles se vendraient mieux : à mesurer."""
        return urlsplit(self.image_url or "").path.lower().endswith(".gif")

    @property
    def page_url(self) -> str | None:
        """Article Wikipédia de la carte (fr.wikipedia d'après son titre si le site n'en donne pas)."""
        if self.wikipedia_url:
            return self.wikipedia_url
        if not self.wikipedia_title:
            return None
        return "https://fr.wikipedia.org/wiki/" + quote(self.wikipedia_title.replace(" ", "_"))


class OwnedCard(_Model):
    """Un exemplaire possédé : `id` est l'identifiant de l'exemplaire, `card.id` celui du catalogue."""

    id: str
    card_id: str | None = None
    card: Card
    is_shiny: bool = False
    starred: bool = False
    snapshot_rarity: str | None = None
    user_id: str | None = None
    obtained_at: datetime | None = None
    tags: list[Any] = []

    @property
    def tag_names(self) -> set[str]:
        names = set()
        for tag in self.tags:
            name = tag.get("name") if isinstance(tag, dict) else tag
            if isinstance(name, str) and name.strip():
                names.add(name.strip().casefold())
        return names

    @property
    def catalog_id(self) -> str:
        return self.card_id or self.card.id

    @property
    def rarity(self) -> str | None:
        return self.snapshot_rarity or self.card.rarity

    @property
    def title(self) -> str:
        return self.card.wikipedia_title


class CollectionPage(_Model):
    collection: list[Any] = []
    total: int | None = None

    def rows(self) -> list[OwnedCard]:
        return parse_items(OwnedCard, self.collection, label="exemplaire")


class CollectionStats(_Model):
    total: int | None = None
    rarities: list[dict[str, Any]] | None = None


class Auction(_Model):
    id: str
    card: Card
    card_id: str | None = None
    seller_id: str | None = None
    status: str = "active"
    end_at: datetime
    base_amount: int
    current_bid: int | None = None
    current_bidder_id: str | None = None
    winner_id: str | None = None
    final_price: int | None = None
    snapshot_rarity: str | None = None
    is_shiny: bool = False

    @property
    def catalog_id(self) -> str:
        return self.card_id or self.card.id

    @property
    def rarity(self) -> str | None:
        return self.snapshot_rarity or self.card.rarity

    @property
    def rarity_rank(self) -> int:
        return RARITY_RANK.get(self.rarity or "", -1)

    @property
    def shiny(self) -> bool:
        return self.is_shiny or self.card.is_shiny

    @property
    def title(self) -> str:
        return self.card.wikipedia_title

    @property
    def min_bid(self) -> int:
        return min_next_bid(self.current_bid, self.base_amount)

    def seconds_left(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        end = self.end_at if self.end_at.tzinfo else self.end_at.replace(tzinfo=UTC)
        return (end - now).total_seconds()


class MarketPage(_Model):
    auctions: list[Any] = []
    hasMore: bool = False

    def items(self) -> list[Auction]:
        return parse_items(Auction, self.auctions, label="enchère")


class AuctionDetail(_Model):
    auction: Auction


class RaritySales(_Model):
    average: float | None = None
    count: int | None = None


class SalesSummary(_Model):
    summary: dict[str, RaritySales] = {}
    wikipedia_title: str | None = None
    is_pro: bool = Field(False, alias="isPro")


class SaleRow(_Model):
    final_price: int | None = None
    snapshot_rarity: str | None = None
    is_shiny: bool = False
    settled_at: datetime | None = None


class SalesHistory(_Model):
    """/api/marketplace/cards/{id}/sales [WP market.js] : `recent` d'abord, sinon `sales`."""
    recent: list[Any] = []
    sales: list[Any] = []

    def rows(self) -> list[SaleRow]:
        return parse_items(SaleRow, self.recent or self.sales, label="vente")

    @property
    def total(self) -> int:
        return len(self.sales) or len(self.recent)


class Balance(_Model):
    balance: int


class BidResult(_Model):
    current_bid: int
    bidder_balance: int


class OwnedCopy(_Model):
    id: str | None = None
    card_id: str | None = None
    is_shiny: bool = False


class DiscardResult(_Model):
    discarded_count: int
    failed: list[Any] = []


class TradeItem(_Model):
    card_id: str | None = None
    user_card_id: str | None = None
    offered_by: str | None = None


class Trade(_Model):
    status: str | None = None
    items: list[TradeItem] = []


class TradesList(_Model):
    trades: list[Trade] = []


class MyBids(_Model):
    """/api/marketplace?mine=1 (relevé du 14/10/2026) : `bidding` = mes enchères [KZ my-bids-logic.js],
    `selling` = mes ventes. `auctions` est le marché général : il ne faut PAS s'en servir ici."""
    bidding: list[Any] = []
    selling: list[Any] = []

    def bid_items(self) -> list[Auction]:
        return parse_items(Auction, self.bidding, label="enchère")


class MyListingsInfo(_Model):
    """/api/marketplace/mine : seulement des compteurs (relevé du 14/10/2026).

    `sellingCount` vaut parfois null (relevé du 14/10/2026, après des ventes invendues)."""
    sellingCount: int | None = None
    maxConcurrentAuctions: int | None = None
