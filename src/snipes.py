"""Cartes demandées sur Discord (/snipe) : le sniper les mise comme une carte de collection manquante.

Chaque demande = un titre exact, une mise maximale et, au choix, une rareté (kv « snipes »). Le sniper
cherche la carte par son titre (comme un jour manquant), la relit en priorité dans sa fenêtre de mise et
mise le minimum, jusqu'à la mise maximale. Une carte gagnée est gardée (pas revendue) et la demande est
retirée ; /annuler la retire avant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from src.core.database import Database
from src.core.models import Auction
from src.core.timeutils import utcnow
from src.workers.collection import collection_key

KV_KEY = "snipes"
# Motif des mises d'une carte demandée : « collection » en tête, la carte est gardée comme une carte de collection.
REASON = "collection : demandée sur Discord"


@dataclass(frozen=True)
class Snipe:
    title: str
    max_price: int
    rarity: str = ""  # "" = toutes raretés
    added_at: str = ""

    @property
    def key(self) -> str:
        return collection_key(self.title)

    def matches(self, auction: Auction) -> bool:
        return collection_key(auction.title) == self.key and (not self.rarity or auction.rarity == self.rarity)

    @property
    def label(self) -> str:
        return f"{self.title}{f' ({self.rarity})' if self.rarity else ''} ≤ {self.max_price} WB"


async def load(db: Database) -> dict[str, Snipe]:
    """Demandes en cours, par clé de titre."""
    return {s.key: s for s in (Snipe(**row) for row in await db.kv_get(KV_KEY, []))}


async def _save(db: Database, snipes: dict[str, Snipe]) -> None:
    await db.kv_set(KV_KEY, [asdict(s) for s in sorted(snipes.values(), key=lambda s: s.added_at)])


async def add(db: Database, title: str, max_price: int, rarity: str = "") -> Snipe:
    """Ajoute (ou remplace) la demande de cette carte."""
    snipes = await load(db)
    snipe = Snipe(title.strip(), max_price, rarity.upper(), utcnow().isoformat())
    snipes[snipe.key] = snipe
    await _save(db, snipes)
    return snipe


async def remove(db: Database, title: str) -> Snipe | None:
    snipes = await load(db)
    removed = snipes.pop(collection_key(title), None)
    if removed:
        await _save(db, snipes)
    return removed
