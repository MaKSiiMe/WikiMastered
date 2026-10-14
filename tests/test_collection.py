"""Collection : lecture tolérante, doublons, mode collection des jours de l'année."""

from __future__ import annotations

import httpx
import pytest

from src.core.api import WikiMastersAPI
from tests.test_integration import make_client


def copy(oid: str, card: str, title: str | None = None) -> dict:
    return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title or card, "rarity": "C"}}


@pytest.mark.asyncio
async def test_collection_read_survives_changes_during_paging(tmp_path):
    """Une carte gagnée pendant la lecture décale les pages : l'exemplaire revu est ignoré."""
    page0 = [copy(f"o{i}", f"c{i}") for i in range(50)]
    page1 = [copy("o49", "c49"), copy("o50", "c50")]  # o49 revu à cause du décalage

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"collection": page0 if page == 0 else page1})

    client, _ = make_client(tmp_path, handler)
    rows = await WikiMastersAPI(client).load_collection()
    assert [r.id for r in rows] == [f"o{i}" for i in range(51)]


@pytest.mark.asyncio
async def test_duplicates_priced_within_quota(tmp_path):
    """Pas plus de SELL_PRICE_LOOKUPS_PER_RUN cotes demandées ; les autres cartes : ventes observées."""
    from src.core.database import Database
    from src.core.pricing import PriceService
    from src.workers.base import Context
    from src.workers.inventory_manager import InventoryManager, select_for_sale
    from tests.test_integration import seed_observed

    rows = [copy("a1", "A"), copy("a2", "A"), copy("a3", "A"), copy("b1", "B"), copy("b2", "B"),
            copy("c1", "C"), copy("c2", "C")]
    lookups = []

    def handler(request):
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": rows})
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if request.url.path.endswith("/sales"):
            lookups.append(request.url.path)
            return httpx.Response(200, json={"summary": {"C": {"average": 80}}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sell_price_lookups_per_run=1)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await seed_observed(db, "B", [60, 70])
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    groups, excluded, quotes, _ = await inv.snapshot()
    assert len(lookups) == 2  # A (3 exemplaires, prioritaire) : historique refusé puis moyenne, 1 seule carte
    assert quotes["C"] == {}  # ni cote ni vente observée : ignorée ce passage
    sales = select_for_sale(groups, quotes, excluded, client.settings)
    assert sorted((c.owned.catalog_id, c.price) for c in sales) == [("A", 80), ("A", 80), ("B", 65)]
    await db.close()
