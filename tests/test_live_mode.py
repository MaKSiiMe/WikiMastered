"""Mode mixte : tout est réel sauf ce qui est listé dans SIMULATE (ex. le sniper)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.config import Settings
from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context
from tests.test_integration import ME, make_client, seed_observed


def test_simulate_and_live_writes_groups():
    s = Settings(_env_file=None, dry_run=False, simulate=["sniper"])
    assert not s.is_live("place_bid") and s.is_live("collection_bid") and s.is_live("create_listing")
    assert s.is_live("open_pack") and s.mode_label == "RÉEL (simulé : sniper)"
    d = Settings(_env_file=None, dry_run=True, live_writes=["packs", "ventes"])
    assert d.is_live("open_pack") and d.is_live("create_listing") and not d.is_live("place_bid")
    assert Settings(_env_file=None, collection_sets='"jours"').collection_sets == ["jours"]  # guillemets retirés


@pytest.mark.asyncio
async def test_sniper_simulated_while_collection_bids_are_real(tmp_path):
    now = datetime.now(timezone.utc)

    def sale(aid, title, base):
        return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": (now + timedelta(seconds=15)).isoformat(), "base_amount": base,
                "current_bid": None, "current_bidder_id": None}

    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(request.url.path)
            return httpx.Response(200, json={"current_bid": 10, "bidder_balance": 2990})
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [sale("pos", "Mème", 100), sale("col", "14 octobre", 10)],
                                             "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, simulate=["sniper"],
                                   collection_sets=["jours"], collection_max_price=20)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await seed_observed(db, "card-Mème", [400, 410, 390])
    await db.kv_set("collection", {"owned": [], "missing": ["14 octobre"], "total": 366})
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    await sniper.step()
    status = {r["auction_id"]: (r["status"], r["dry_run"]) for r in await db._all("SELECT * FROM bids")}
    assert status == {"pos": ("simulated", 1), "col": ("placed", 0)}
    assert posted == ["/api/marketplace/col/bid"]  # seule la mise de collection est partie
    assert client.user_id == ME
    await db.close()
