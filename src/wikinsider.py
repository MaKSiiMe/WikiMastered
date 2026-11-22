"""Salon #wikinsider : les achats et ventes du bot à noter à la main sur Wikinsider (site d'un groupe de traders).

Le bot ne se connecte pas à Wikinsider : il prépare seulement, pour chaque opération réelle, les champs de son
formulaire (« Noter un achat » : shiny, type, nom exact, prix, lien de l'enchère ; « Noter une vente » : la carte et
le prix). Le bouton « ✅ Noté » efface le message : le salon ne garde que ce qu'il reste à noter.
Rien sans DISCORD_WEBHOOK_WIKINSIDER, et rien en simulation.
"""

from __future__ import annotations

from typing import Any

from src.core.notifier import Level, Topic, auction_url


def _common(title: str, price: int, shiny: bool, rarity: str | None, link: str | None) -> list[tuple[str, str]]:
    columns = [("Nom exact", f"`{title}`"), ("Prix", f"`{price}`"), ("Shiny", "oui" if shiny else "non"),
               ("Type", "Marché"), ("Rareté", rarity or "?")]
    if link:
        columns.append(("Lien de l'enchère", link))
    return columns


def note_buy(notifier: Any, base_url: str, *, bid_id: int, title: str, price: int, shiny: bool,
             rarity: str | None, auction_id: str | None, kept: str = "") -> None:
    """Achat gagné par le bot ; `kept` : pourquoi la carte est gardée (collection…), vide pour une revente."""
    link = auction_url(base_url, auction_id) if auction_id else None
    notifier.send(Level.INFO, f"Achat à noter : {title}",
                  f"Wikinsider → **Noter un achat**{f' · carte gardée ({kept})' if kept else ''}",
                  topic=Topic.WIKINSIDER, live=True, columns=_common(title, price, shiny, rarity, link),
                  actions={"kind": "wikinsider", "bid": bid_id})


def note_sale(notifier: Any, base_url: str, *, listing_id: int, title: str, price: int, shiny: bool,
              rarity: str | None, auction_id: str | None, bought: int | None = None) -> None:
    """Vente conclue ; `bought` : prix d'achat noté (position du bot), None pour une carte mise en vente à la main."""
    link = auction_url(base_url, auction_id) if auction_id else None
    columns = _common(title, price, shiny, rarity, link)
    if bought is not None:
        columns.insert(2, ("Achetée", f"{bought} WB ({price - bought:+d})"))
    notifier.send(Level.INFO, f"Vente à noter : {title}", "Wikinsider → **Noter une vente**",
                  topic=Topic.WIKINSIDER, live=True, columns=columns,
                  actions={"kind": "wikinsider", "listing": listing_id})
