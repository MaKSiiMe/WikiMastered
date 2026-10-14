"""Autonomie : le bot vérifie lui-même les écritures au résultat incertain, sans jamais les rejouer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context
from src.workers.inventory_manager import POSITION_MAX_LIST_FAILURES, InventoryManager
from tests.test_integration import ME, make_client

SELLER = "22222222-2222-2222-2222-222222222222"


def auction(aid: str, card: str, **kw) -> dict:
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    return {"id": aid, "card": {"id": card, "wikipedia_title": card, "rarity": "C"}, "seller_id": SELLER,
            "status": "active", "end_at": end.isoformat(), "base_amount": 100, "current_bid": None,
            "current_bidder_id": None, **kw}


async def setup_ctx(tmp_path, handler, **overrides):
    client, notifier = make_client(tmp_path, handler, **overrides)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    return Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)), db, notifier


async def uncertain_bid(db: Database, aid: str, card: str, amount: int) -> None:
    await db.record_bid(auction_id=aid, card_id=card, title=card, rarity="C", is_shiny=False, amount=amount,
                        reference_price=400, reason="revente ~400 WB", night=False, dry_run=False,
                        status="uncertain", end_at=datetime.now(timezone.utc) + timedelta(hours=1))
    await db._exec("UPDATE bids SET placed_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),))


@pytest.mark.asyncio
async def test_uncertain_bids_are_checked_on_the_site(tmp_path):
    server = {
        "a-lead": auction("a-lead", "c1", current_bid=120, current_bidder_id=ME),
        "a-lost": auction("a-lost", "c2", current_bid=150, current_bidder_id=SELLER),
        "a-won": auction("a-won", "c3", status="settled_sold", current_bid=90, winner_id=ME),
    }

    def handler(request):
        if request.method != "GET":
            raise AssertionError("une mise incertaine ne doit jamais être rejouée")
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        aid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"auction": server[aid]})

    ctx, db, notifier = await setup_ctx(tmp_path, handler, dry_run=False)
    for aid, amount in (("a-lead", 120), ("a-lost", 110), ("a-won", 90)):
        await uncertain_bid(db, aid, server[aid]["card"]["id"], amount)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper._reconcile()
    await sniper._reconcile()  # 2 par passage
    status = {r["auction_id"]: r["status"] for r in await db._all("SELECT auction_id, status FROM bids")}
    assert status == {"a-lead": "placed", "a-lost": "outbid", "a-won": "won"}
    assert sniper._leading["a-lead"][1] == 120
    assert (await db.positions_summary())["count"] == 1  # la carte gagnée devient une position
    assert [t for _, t in notifier.sent].count("Mise incertaine vérifiée") == 3
    await db.close()


@pytest.mark.asyncio
async def test_uncertain_listing_found_or_reprogrammed(tmp_path):
    selling = [auction("sale-1", "c1", base_amount=400)]

    def handler(request):
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": selling})
        raise AssertionError(f"route inattendue {request.url}")

    ctx, db, notifier = await setup_ctx(tmp_path, handler, dry_run=False)
    now = datetime.now(timezone.utc)
    for owned, card in (("o1", "c1"), ("o2", "c2")):
        lid = await db.schedule_listing(owned_card_id=owned, card_id=card, title=card, rarity="C", base_amount=400,
                                        duration_minutes=360, post_at=now, target_end=now + timedelta(hours=6),
                                        reason="test")
        await db.update_listing(lid, status="uncertain", posted_at=now - timedelta(minutes=2))
    assert await db.scheduled_owned_ids() == {"o1", "o2"}  # pas de nouvelle vente tant que ce n'est pas vérifié
    await InventoryManager(ctx).reconcile_listings()
    rows = {r["owned_card_id"]: r for r in await db._all("SELECT * FROM listings")}
    assert rows["o1"]["status"] == "posted" and rows["o1"]["auction_id"] == "sale-1"
    assert rows["o2"]["status"] == "failed"
    await db.close()


@pytest.mark.asyncio
async def test_position_kept_after_repeated_listing_failures(tmp_path):
    def handler(request):
        raise AssertionError(f"route inattendue {request.url}")

    ctx, db, notifier = await setup_ctx(tmp_path, handler)
    bid = await db.record_bid(auction_id="a1", card_id="c1", title="Mème", rarity="C", is_shiny=False, amount=100,
                              reference_price=400, reason="revente", night=False, dry_run=True, status="sim_won",
                              end_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    for _ in range(POSITION_MAX_LIST_FAILURES):
        lid = await db.schedule_listing(owned_card_id=f"sim-{bid}", card_id="c1", title="Mème", rarity="C",
                                        base_amount=400, duration_minutes=360, post_at=now, target_end=now,
                                        reason="test", bid_id=bid)
        await db.update_listing(lid, status="failed", error="refusée")
    await InventoryManager(ctx).manage_positions()
    assert (await db.positions_summary())["count"] == 0
    assert ("WARNING", "Revente abandonnée") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_listing_auction_id_read_from_real_response(tmp_path):
    """Réponse réelle de POST /api/marketplace : HTTP 201 {"auction_id": ...}, sans appel supplémentaire."""
    def handler(request):
        raise AssertionError(f"aucune requête attendue : {request.url}")

    ctx, db, _ = await setup_ctx(tmp_path, handler)
    found = await InventoryManager(ctx)._listing_auction_id({"auction_id": "63880418-489d"}, "c1")
    assert found == "63880418-489d"
    await db.close()
