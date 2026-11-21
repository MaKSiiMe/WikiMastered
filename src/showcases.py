"""Vitrines du profil (commandes Discord /vitrines et /vitrine) : lues et enregistrées comme l'éditeur du site.

Routes relevées dans le code du site le 21/11 : GET /api/showcase -> {galleries: [{gallery_index, name, layout}],
showcase: [{position, gallery_index, caption, user_card: {id, snapshot_rarity, is_shiny, card{...}}}]} ;
PUT /api/showcase/gallery {gallery_index, layout, name, user_card_ids, captions}. Une vitrine de plus tous les 5 000
exemplaires (10 au plus). Pour chaque titre demandé, le bot prend ton meilleur exemplaire : la plus haute rareté, shiny
d'abord, puis en favori.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config import RARITY_RANK
from src.core.models import OwnedCard
from src.workers.base import Context
from src.workers.collection import collection_key

MAX_GALLERIES = 10
COPY_SEARCH_PAGES = 3  # pages de la collection lues par titre (50 exemplaires chacune)


@dataclass(frozen=True)
class Layout:
    label: str
    min: int
    max: int
    captions: bool = False  # légende sous chaque carte (date de la frise)


# Dispositions de l'éditeur du site (relevé du 21/11), dans son ordre.
LAYOUTS: dict[str, Layout] = {
    "classique": Layout("Classique", 4, 4),
    "podium": Layout("Podium", 3, 3),
    "eventail": Layout("Éventail", 3, 5),
    "vedette": Layout("Vedette", 5, 7),
    "grille": Layout("Grille", 6, 12),
    "onze": Layout("Onze de départ", 11, 11),
    "frise": Layout("Ligne du temps", 3, 8, captions=True),
    "chaine": Layout("Chaîne de causalité", 3, 6),
    "cycle": Layout("Cycle", 3, 6),
    "pyramide": Layout("Pyramide", 6, 6),
}


@dataclass
class Gallery:
    index: int
    name: str | None = None
    layout: str = "classique"
    cards: list[tuple[str, str, bool, str | None]] = field(default_factory=list)  # titre, rareté, shiny, légende


def parse(data: dict[str, Any]) -> list[Gallery]:
    """Réponse de GET /api/showcase : les vitrines dans l'ordre, cartes rangées par position (comme le site)."""
    galleries = {i: Gallery(i) for i in range(MAX_GALLERIES)}
    for g in data.get("galleries") or []:
        if isinstance(g, dict) and isinstance(g.get("gallery_index"), int) and g["gallery_index"] in galleries:
            gallery = galleries[g["gallery_index"]]
            gallery.name = (g.get("name") or "").strip() or None
            gallery.layout = g.get("layout") if g.get("layout") in LAYOUTS else "classique"
    slots = []
    for item in data.get("showcase") or []:
        card = (item.get("user_card") or {}) if isinstance(item, dict) else {}
        if not isinstance(card.get("card"), dict) or not isinstance(item.get("position"), int):
            continue
        index = item.get("gallery_index")
        index = item["position"] // 4 if index is None else index
        if index in galleries:
            slots.append((index, item["position"], (str(card["card"].get("wikipedia_title") or "?"),
                                                     str(card.get("snapshot_rarity") or card["card"].get("rarity") or "?"),
                                                     bool(card.get("is_shiny")), item.get("caption"))))
    for index, _, entry in sorted(slots, key=lambda s: (s[0], s[1])):
        galleries[index].cards.append(entry)
    return [galleries[i] for i in range(MAX_GALLERIES)]


def overview(galleries: list[Gallery]) -> str:
    """Texte de /vitrines : chaque vitrine remplie, sa disposition et ses cartes."""
    lines = []
    for g in galleries:
        if not g.cards and not g.name:
            continue
        layout = LAYOUTS.get(g.layout, LAYOUTS["classique"]).label
        cards = ", ".join(f"{t} ({r}{' ✨' if s else ''}){f' — {c}' if c else ''}" for t, r, s, c in g.cards)
        lines.append(f"**{g.index + 1}. {g.name or 'Sans nom'}** · {layout}\n{cards or 'vide'}")
    return "\n\n".join(lines) or "Aucune vitrine remplie."


def rank(copy: OwnedCard) -> tuple[int, bool, bool]:
    """Meilleur exemplaire d'abord : rareté, shiny, favori."""
    return RARITY_RANK.get(copy.rarity or "", -1), copy.is_shiny, copy.starred


async def best_copy(ctx: Context, title: str) -> OwnedCard | None:
    """Ton meilleur exemplaire de cette carte (titre exact, sans casse ni « 1er »), ou None si tu ne l'as pas."""
    key, found = collection_key(title), []
    for number in range(COPY_SEARCH_PAGES):
        page = await ctx.api.collection_page(number, sort="rarity", q=title)
        found += [c for c in page.rows() if collection_key(c.title) == key]
        if len(page.collection) < 50:
            break
    return max(found, key=rank) if found else None


def check(layout: str, titles: list[str], captions: list[str]) -> str | None:
    """Erreur de saisie (nombre de cartes de la disposition, légendes), ou None."""
    spec = LAYOUTS[layout]
    if not spec.min <= len(titles) <= spec.max:
        count = str(spec.min) if spec.min == spec.max else f"{spec.min} à {spec.max}"
        return f"{spec.label} : {count} cartes, pas {len(titles)}."
    if captions and len(captions) != len(titles):
        return f"{len(captions)} légende(s) pour {len(titles)} carte(s)."
    return None


def split(text: str) -> list[str]:
    """« Khéops ; Khéphren ; Sphinx » -> titres (séparés par des points-virgules)."""
    return [part.strip() for part in (text or "").split(";") if part.strip()]
