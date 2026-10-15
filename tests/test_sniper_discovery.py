"""Sniper : repérage des ventes à l'avance, relecture dans la fenêtre de mise, prolongation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.config import Settings
from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.models import Auction
from src.core.pricing import PriceService, Quote
from src.workers.auction_sniper import AuctionSniper, Target, decide, snipe_params
from src.workers.base import Context
from src.workers.collection import collection_key
from tests.test_integration import make_client, seed_observed

SELLER = "22222222-2222-2222-2222-222222222222"


def sale(aid: str, title: str, rarity: str, seconds: float, base: int = 20, bid: int | None = None) -> dict:
    return {"id": aid, "card": {"id": f"card-{aid}", "wikipedia_title": title, "rarity": rarity},
            "seller_id": SELLER, "status": "active",
            "end_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
            "base_amount": base, "current_bid": bid, "current_bidder_id": None}


@pytest.mark.asyncio
async def test_sniper_spots_sales_early_then_rereads_them_to_bid(tmp_path):
    market = {"u1": sale("u1", "Jean Racine", "UR", 300), "c1": sale("c1", "Ada", "C", 300),
              "d1": sale("d1", "14 octobre", "SR", 600, base=10)}
    ended = [sale("e1", "Nightcall", "UR", -20, bid=150)]
    queries = []

    def handler(request):
        path, params = request.url.path, request.url.params
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(404, json={})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        if path == "/api/marketplace":
            queries.append((params["sort"], params.get("rarity"), params.get("q")))
            if params.get("q"):
                return httpx.Response(200, json={"auctions": [market["d1"]], "hasMore": False})
            if params["sort"] == "ending_soon":
                return httpx.Response(200, json={"auctions": ended, "hasMore": False})
            rows = [market["u1"], market["c1"]] if params.get("rarity") == "UR" else []
            return httpx.Response(200, json={"auctions": rows, "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_resale_rarities=["UR", "L"],
                                   collection_sets=["jours"], collection_max_price=50)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.kv_set("collection", {"owned": [], "missing": ["14 octobre"], "total": 366})
    await seed_observed(db, "card-u1", [400, 420, 390], rarity="UR")
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    for _ in range(8):
        await sniper.step()
    # Nouvelles ventes UR et L, ventes terminées, puis le jour manquant cherché par son titre.
    assert ("recent", "UR", None) in queries and ("recent", "L", None) in queries
    assert ("ending_soon", "UR", None) in queries and ("ending_soon", None, "14 octobre") in queries
    assert set(sniper._targets) == {"u1", "d1"}  # la C n'est pas revendue, rien n'est encore misé
    assert not await db._all("SELECT * FROM bids")

    # Les deux ventes entrent dans la fenêtre de mise : chacune est relue sur sa page, puis misée.
    now = datetime.now(timezone.utc)
    for aid in ("u1", "d1"):
        market[aid]["end_at"] = (now + timedelta(seconds=16)).isoformat()
        target = sniper._targets[aid]
        target.auction = Auction.model_validate(market[aid])
        target.seen_at = now - timedelta(seconds=30)
    await sniper.step()
    await sniper.step()
    rows = {r["auction_id"]: r["reason"] for r in await db._all("SELECT auction_id, reason FROM bids")}
    assert set(rows) == {"u1", "d1"}
    assert rows["u1"].startswith("revente") and rows["d1"].startswith("collection")
    assert all(sniper._targets[aid].recheck for aid in rows)

    # À sa fin, une vente misée est relue : prolongée (mise dans les 10 dernières secondes), elle est rejouée.
    sniper._targets["d1"].auction = Auction.model_validate({**market["d1"], "end_at": (
        now - timedelta(seconds=10)).isoformat()})
    sniper._targets.pop("u1")
    market["d1"]["end_at"] = (now + timedelta(seconds=60)).isoformat()
    await sniper.step()
    assert not sniper._targets["d1"].recheck
    assert "Repérage : 1 vente(s) suivie(s) (UR/L et jours manquants)" in sniper.recap()[0]
    await db.close()


def test_resale_only_for_watched_rarities():
    params = snipe_params(Settings(_env_file=None), night=False)
    auction = Auction.model_validate(sale("x", "Ada", "C", 15, base=5))
    quote = Quote(400, 5)
    assert decide(auction, params, quote, 2) is not None
    assert decide(auction, params, quote, 2, resale=False) is None


@pytest.mark.asyncio
async def test_missed_new_sales_are_reported(tmp_path, caplog):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"bidding": [], "balance": 0}))
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    now = datetime.now(timezone.utc)
    page = [Auction.model_validate(sale(f"a{i}", "T", "UR", 3600)) for i in range(50)]
    sniper._check_gap("UR", page, True, now)
    sniper._check_gap("UR", page[10:] + page[:10], True, now)  # recoupement : rien de manqué
    assert sniper._gaps == 0
    later = [Auction.model_validate(sale(f"b{i}", "T", "UR", 3600)) for i in range(50)]
    sniper._check_gap("UR", later, True, now)
    sniper._check_gap("UR", page, True, now, 95.0)
    assert sniper._gaps == 2
    warned = [r.message for r in caplog.records if "certaines ont été manquées" in r.message]
    assert len(warned) == 1  # une fois par heure
    assert "en ? entre deux lectures" in warned[0]
    await db.close()


@pytest.mark.asyncio
async def test_bid_follows_the_reread_without_other_requests(tmp_path):
    """Solde, places de vente et cote sont lus avant de relire l'enchère : la mise part juste après la relecture.
    Une carte de collection manquante passe avant une revente dans la fenêtre de mise."""
    now = datetime.now(timezone.utc)
    market = {"u1": sale("u1", "Jean Racine", "UR", 14), "d1": sale("d1", "11 décembre", "SR", 16, base=10)}
    calls = []

    def handler(request):
        path, params = request.url.path, request.url.params
        calls.append(f"{request.method} {path}")
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(404, json={})
        if path.endswith("/bid"):
            return httpx.Response(200, json={"current_bid": 10, "bidder_balance": 2990})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_resale_rarities=["UR", "L"],
                                   collection_sets=["jours"], collection_max_price=50,
                                   dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.kv_set("collection", {"owned": [], "missing": [collection_key("11 décembre")], "total": 366})
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    params = snipe_params(client.settings, night=False)
    for aid, data in market.items():
        sniper._targets[aid] = Target(Auction.model_validate(data), now - timedelta(seconds=30))
    sniper._balance = None  # solde à relire
    assert sniper._next_due(params, now).auction.id == "d1"  # la collection d'abord, même si u1 finit avant

    calls.clear()
    await sniper.step()
    read = calls.index("GET /api/marketplace/d1")
    assert "GET /api/wikibidous" in calls[:read]  # solde lu avant la relecture
    assert calls[read + 1] == "POST /api/marketplace/d1/bid"  # puis la mise, sans autre requête entre les deux
    await db.close()


@pytest.mark.asyncio
async def test_unread_collection_target_is_logged(tmp_path, caplog):
    caplog.set_level("INFO")
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"bidding": [], "balance": 0}),
                                   collection_sets=["jours"])
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.kv_set("collection", {"owned": [], "missing": ["11 septembre"], "total": 366})
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    now = datetime.now(timezone.utc)
    sniper._targets["s1"] = Target(Auction.model_validate(sale("s1", "11 septembre", "UR", 5)), now)
    sniper._prune(now)
    assert any("11 septembre" in r.message and "pas relue à temps" in r.message for r in caplog.records)
    await db.close()


def _bare_handler(calls, market):
    def handler(request):
        path, params = request.url.path, request.url.params
        calls.append(f"{request.method} {path}")
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(404, json={})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [], "hasMore": False})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        raise AssertionError(f"route inattendue {request.url}")
    return handler


@pytest.mark.asyncio
async def test_collection_guard_holds_slow_work(tmp_path):
    """Une carte de collection entre dans sa fenêtre de mise dans 5 s : la revente déjà dans la sienne attend,
    le sniper se réveille à l'entrée de la carte (15/10 : « 7 octobre à 10 WB … pas relue à temps »)."""
    now = datetime.now(timezone.utc)
    market = {"u1": sale("u1", "Jean Racine", "UR", 15), "d1": sale("d1", "7 octobre", "UR", 25, base=10)}
    calls = []
    client, notifier = make_client(tmp_path, _bare_handler(calls, market), sniper_resale_rarities=["UR", "L"],
                                   collection_sets=["jours"], collection_max_price=50)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.kv_set("collection", {"owned": [], "missing": ["7 octobre"], "total": 366})
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    for aid, data in market.items():
        sniper._targets[aid] = Target(Auction.model_validate(data), now - timedelta(seconds=30))
    calls.clear()
    delay = await sniper.step()
    # Seulement solde et places de vente mis en cache : ni relecture de u1, ni repérage.
    assert sorted(calls) == ["GET /api/marketplace/mine", "GET /api/wikibidous"]
    assert 4 < delay < 6
    await db.close()


@pytest.mark.asyncio
async def test_late_discovery_goes_before_resale_rereads(tmp_path):
    now = datetime.now(timezone.utc)
    market = {"u1": sale("u1", "Jean Racine", "UR", 15)}
    calls = []
    client, notifier = make_client(tmp_path, _bare_handler(calls, market), sniper_resale_rarities=["UR"])
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    sniper._targets["u1"] = Target(Auction.model_validate(market["u1"]), now - timedelta(seconds=30))
    sniper._last_read = {("recent", "UR"): now - timedelta(seconds=5), ("ended", "UR"): now}
    calls.clear()
    await sniper.step()  # repérage à jour : la revente est relue
    assert "GET /api/marketplace/u1" in calls
    sniper._targets["u1"] = Target(Auction.model_validate(market["u1"]), now - timedelta(seconds=30))
    sniper._last_read[("recent", "UR")] = now - timedelta(seconds=25)  # plus de deux fois la période (10 s)
    calls.clear()
    await sniper.step()
    assert calls.count("GET /api/marketplace") == 1 and "GET /api/marketplace/u1" not in calls
    await db.close()
