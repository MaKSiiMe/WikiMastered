"""Validation avant le passage en réel : simulation réaliste, bilan, premières vraies réponses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.main import validation_text
from src.workers.base import Context
from src.workers.inventory_manager import InventoryManager
from tests.test_integration import make_client, seed_observed


async def simulated_position(db: Database, base_amount: int) -> int:
    now = datetime.now(timezone.utc)
    bid = await db.record_bid(auction_id="a1", card_id="c1", title="Mème", rarity="C", is_shiny=False, amount=100,
                              reference_price=400, reason="revente ~400 WB", night=False, dry_run=True,
                              status="sim_won", end_at=now)
    lid = await db.schedule_listing(owned_card_id=f"sim-{bid}", card_id="c1", title="Mème", rarity="C",
                                    base_amount=base_amount, duration_minutes=360, post_at=now,
                                    target_end=now - timedelta(minutes=5), reason="position", bid_id=bid)
    await db.update_listing(lid, status="simulated")
    return bid


@pytest.mark.asyncio
async def test_simulated_resale_follows_the_observed_market(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [300, 310, 290])  # la tendance est retombée sous le prix de départ
    await inv.settle_listings()
    rows = await db._all("SELECT status, base_amount, relist_count FROM listings ORDER BY id")
    assert [r["status"] for r in rows] == ["sim_unsold", "scheduled"]
    assert rows[1]["base_amount"] == 360 and rows[1]["relist_count"] == 1  # remise à -10 %
    assert (await db.get_bid(bid))["closed"] == 0
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 2",
                   ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 3",
                   ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()  # 2 remises déjà faites : carte gardée
    assert (await db.get_bid(bid))["closed"] == 1 and (await db.get_bid(bid))["sale_price"] is None
    stats = await db.validation_stats(datetime.now(timezone.utc) - timedelta(days=1), 2)
    assert stats["sim_kept"] == 1 and stats["sim_kept_cost"] == 100 and stats["sim_sold"] == 0
    assert ("WARNING", "Position invendue") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_simulated_resale_sells_when_the_market_pays(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [420, 400, 450])
    await inv.settle_listings()
    assert (await db.get_bid(bid))["sale_price"] == 400
    assert (await db.validation_stats(datetime.now(timezone.utc), 2))["sim_profit"] == 300
    await db.close()


def test_validation_verdicts():
    base = {"observed_today": 120, "observed_total": 900, "cards_priced": 80, "sim_bought": 14, "sim_held": 2,
            "uncertain": 0}
    assert "assez" in validation_text({**base, "sim_sold": 3, "sim_profit": 400, "sim_kept": 1, "sim_kept_cost": 90})
    assert "envisageable" in validation_text({**base, "sim_sold": 10, "sim_profit": 900, "sim_kept": 2,
                                              "sim_kept_cost": 300})
    assert "rester en simulation" in validation_text({**base, "sim_sold": 8, "sim_profit": 200, "sim_kept": 4,
                                                      "sim_kept_cost": 900})


@pytest.mark.asyncio
async def test_first_real_writes_are_kept_for_checking(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"ok": True, "auction": {"id": "x"}}),
                                   dry_run=False)
    for _ in range(4):
        await WikiMastersAPI(client).create_listing("o1", 400, 360)
    files = sorted(p.name for p in (tmp_path / "inspect" / "writes").iterdir())
    assert files == ["create_listing-1.json", "create_listing-2.json", "create_listing-3.json"]
    assert [t for _, t in notifier.sent] == ["Première réponse réelle : create_listing"]
