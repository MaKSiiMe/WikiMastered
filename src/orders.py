"""Ordres donnés sur Discord pour la prochaine remise en vente d'une position (boutons « Brader » / « Garder » de
l'alerte « Position qui traîne ») : la vente est déjà publiée sur le site, le bot ne peut pas la changer, l'ordre
s'applique à sa fin si elle reste invendue. Gardés en base (kv « position_orders » : position -> ordre)."""

from __future__ import annotations

from src.core.database import Database

KV_KEY = "position_orders"
FIRE = "fire"  # revente à 1 WB tout de suite, sans repasser par les remises à -10 %
KEEP = "keep"  # la carte reste dans la collection, plus revendue


async def give(db: Database, bid_id: int, order: str) -> None:
    orders = await db.kv_get(KV_KEY, {})
    orders[str(bid_id)] = order
    await db.kv_set(KV_KEY, orders)


async def take(db: Database, bid_id: int) -> str | None:
    """L'ordre en attente pour cette position, retiré."""
    orders = await db.kv_get(KV_KEY, {})
    order = orders.pop(str(bid_id), None)
    if order is not None:
        await db.kv_set(KV_KEY, orders)
    return order
