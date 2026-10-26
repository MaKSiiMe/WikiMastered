"""Cartes gardées (/garder sur Discord) : jamais vendues ni défaussées, comme une étiquette de PROTECTED_TAGS.

Une demande = un titre et, au choix, une rareté (kv « kept ») ; tous les exemplaires correspondants sont protégés.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from src.core.database import Database
from src.core.models import OwnedCard
from src.core.timeutils import utcnow
from src.workers.collection import collection_key

KV_KEY = "kept"


@dataclass(frozen=True)
class Keep:
    title: str
    rarity: str = ""  # "" = toutes raretés
    added_at: str = ""

    @property
    def key(self) -> str:
        return f"{collection_key(self.title)}|{self.rarity}"

    def matches(self, title: str, rarity: str | None) -> bool:
        return collection_key(title) == collection_key(self.title) and (not self.rarity or rarity == self.rarity)

    @property
    def label(self) -> str:
        return f"{self.title}{f' ({self.rarity})' if self.rarity else ''}"


async def load(db: Database) -> list[Keep]:
    return [Keep(**row) for row in await db.kv_get(KV_KEY, [])]


async def add(db: Database, title: str, rarity: str = "") -> Keep:
    kept = {k.key: k for k in await load(db)}
    keep = Keep(title.strip(), rarity.upper(), utcnow().isoformat())
    kept.setdefault(keep.key, keep)
    await db.kv_set(KV_KEY, [asdict(k) for k in kept.values()])
    return kept[keep.key]


async def remove(db: Database, label: str) -> Keep | None:
    """Retire la demande affichée sous ce libellé (« Titre » ou « Titre (L) »)."""
    kept = await load(db)
    found = next((k for k in kept if k.label.casefold() == label.strip().casefold()), None)
    if found:
        await db.kv_set(KV_KEY, [asdict(k) for k in kept if k is not found])
    return found


def protected(rows: list[OwnedCard], kept: list[Keep]) -> set[str]:
    """Exemplaires de ces lignes protégés par une carte gardée."""
    return {r.id for r in rows if any(k.matches(r.title, r.rarity) for k in kept)}
