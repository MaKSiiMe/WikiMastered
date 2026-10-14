"""Client et sniper face à un faux serveur (httpx.MockTransport) : aucun appel réseau réel."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.config import Settings
from src.core.api import WikiMastersAPI
from src.core.client import ApiError, WikiMastersClient
from src.core.database import Database
from src.core.endpoints import Endpoints
from src.core.notifier import DiscordNotifier
from src.core.pricing import PriceService
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context

ROOT = Path(__file__).resolve().parents[1]
ME = "11111111-1111-1111-1111-111111111111"


class Recorder(DiscordNotifier):
    def __init__(self):
        super().__init__("", dry_run=True)
        self.sent: list[tuple[str, str]] = []

    def send(self, level, title, description="", **fields):
        self.sent.append((level.name, title))


def make_client(tmp_path: Path, handler, **overrides) -> tuple[WikiMastersClient, Recorder]:
    s = Settings(_env_file=None, data_dir=tmp_path, wm_cookie="a=b", wm_user_id=ME,
                 http_min_spacing_seconds=0, http_jitter_seconds=0, backoff_base_seconds=0.01,
                 backoff_rate_limit_base_seconds=0.01, **overrides)
    notifier = Recorder()
    client = WikiMastersClient(s, Endpoints.load(ROOT / "config" / "endpoints.yaml"), notifier)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies=client._http.cookies)
    return client, notifier


@pytest.mark.asyncio
async def test_rate_limit_backs_off_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"balance": 42})

    client, notifier = make_client(tmp_path, handler)
    assert await WikiMastersAPI(client).balance() == 42
    assert ("CRITICAL", "Limitation de requêtes (429)") in notifier.sent


@pytest.mark.asyncio
async def test_401_pauses_until_cookie_dropped(tmp_path, monkeypatch):
    def handler(request):
        if "good=1" in request.headers.get("cookie", ""):
            return httpx.Response(200, json={"balance": 7})
        return httpx.Response(401, json={"error": "unauthorized"})

    client, notifier = make_client(tmp_path, handler)
    real_sleep = asyncio.sleep
    monkeypatch.setattr("src.core.client.asyncio.sleep", lambda s: real_sleep(0.01))
    task = asyncio.create_task(WikiMastersAPI(client).balance())
    await real_sleep(0.05)
    assert not client.session_ok and not task.done()
    assert notifier.sent[0][0] == "CRITICAL"
    (tmp_path / "session_cookie.txt").write_text("good=1", encoding="utf-8")
    assert await asyncio.wait_for(task, timeout=2) == 7
    assert client.session_ok and ("SUCCESS", "Session rétablie") in notifier.sent
    assert "good=1" in (tmp_path / "session.json").read_text()


@pytest.mark.asyncio
async def test_403_with_valid_session_is_a_route_error(tmp_path):
    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 1})
        return httpx.Response(403, json={"error": "Réservé aux membres PRO"})

    client, _ = make_client(tmp_path, handler)
    with pytest.raises(ApiError):
        await client.get("sales_summary", card_id="x")
    assert client.session_ok


@pytest.mark.asyncio
async def test_dry_run_never_sends_writes(tmp_path):
    def handler(request):
        raise AssertionError(f"requête envoyée : {request.method} {request.url}")

    client, _ = make_client(tmp_path, handler)
    result = await WikiMastersAPI(client).place_bid("abc", 5)
    assert result.dry_run and not result.ok


@pytest.mark.asyncio
async def test_sniper_step_simulates_deal_bid(tmp_path):
    end = (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()
    auction = {
        "id": "33333333-3333-3333-3333-333333333333",
        "card": {"id": "44444444-4444-4444-4444-444444444444", "wikipedia_title": "Ada Lovelace", "rarity": "C"},
        "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
        "base_amount": 5, "current_bid": 6, "current_bidder_id": "55555555-5555-5555-5555-555555555555",
    }

    def handler(request):
        path = request.url.path
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if path == "/api/marketplace" and "mine" in request.url.params:
            return httpx.Response(200, json={"auctions": [], "selling": []})
        if path == "/api/marketplace":
            assert request.url.params["sort"] == "ending_soon"
            return httpx.Response(200, json={"auctions": [auction, {"broken": True}], "hasMore": False})
        if path.endswith("/sales") and "scope" not in request.url.params:
            return httpx.Response(200, json={"recent": [{"final_price": p} for p in (200, 190, 210, 50, 205)]})
        raise AssertionError(f"route inattendue {path}")

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    bid = await db.last_bid(auction["id"])
    assert bid["status"] == "simulated" and bid["amount"] == 7  # max(ceil(6,6), 7)
    assert bid["reference_price"] == 200  # médiane des ventes récentes
    assert "revente" in bid["reason"]
    await sniper.step()  # pas de seconde simulation sur la même enchère
    assert not any("simulée" in title for _, title in notifier.sent)  # pas de message par enchère
    sniper._next_digest = datetime.now(timezone.utc)
    sniper._flush_digest(datetime.now(timezone.utc))
    assert ("INFO", "Sniper : enchères simulées (1)") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_sniper_respects_concurrent_bid_limit(tmp_path):
    end = (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()

    def auction(i: int, base: int) -> dict:
        return {
            "id": f"33333333-3333-3333-3333-{i:012d}",
            "card": {"id": f"44444444-4444-4444-4444-{i:012d}", "wikipedia_title": f"Carte {i}", "rarity": "SR"},
            "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
            "base_amount": base, "current_bid": None, "current_bidder_id": None,
        }

    market = [auction(i, base) for i, base in enumerate([14, 3, 9, 5, 12, 2, 7])]
    manual = {**auction(99, 1), "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"auctions": [manual], "selling": []})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={"error": "aucune vente"})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_rarity_caps={"SR": 15}, portfolio_max_positions=10)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    await sniper.step()
    rows = await db._all("SELECT amount FROM bids WHERE status = 'simulated' ORDER BY amount")
    # 1 enchère manuelle déjà en cours -> 4 places ; les plus grosses marges (15 - mise) d'abord.
    assert [r["amount"] for r in rows] == [2, 3, 5, 7]
    assert sniper.free_slots(datetime.now(timezone.utc)) == 0
    await db.close()


@pytest.mark.asyncio
async def test_pack_opener_learns_stock_and_backs_off_when_empty(tmp_path):
    from src.workers.pack_opener import PackOpener, seconds_until_stock

    stock = {"n": 2}

    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/packs/open"
        if stock["n"] == 0:
            return httpx.Response(400, json={"error": "Plus de paquets", "packs_remaining": 0})
        stock["n"] -= 1
        return httpx.Response(200, json={
            "cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "L"}],
            "owned_copies": [{"id": "o1", "card_id": "c1", "is_shiny": True}],
            "packs_remaining": stock["n"],
        })

    client, notifier = make_client(tmp_path, handler, dry_run=False,
                                   pack_between_opens_min_seconds=0, pack_between_opens_max_seconds=0)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    opener = PackOpener(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await opener.setup()
    await opener._open_batch()
    assert stock["n"] == 0 and opener.state.remaining == 0
    assert sum(1 for _, title in notifier.sent if title == "Tirage remarquable !") == 2
    # Stock vide sans date fraîche : la prochaine tentative attend une période complète.
    await opener._open_batch()
    wait = seconds_until_stock(opener.state, 1, opener.period, datetime.now(timezone.utc))
    assert wait > 500
    await db.close()


@pytest.mark.asyncio
async def test_route_rate_limit_does_not_freeze_other_routes(tmp_path):
    from src.core.client import RateLimited

    def handler(request):
        if request.url.path.endswith("/sales"):
            return httpx.Response(403, headers={"Retry-After": "120"}, json={"error": "Too many requests"})
        return httpx.Response(200, json={"balance": 3})

    client, notifier = make_client(tmp_path, handler)
    api = WikiMastersAPI(client)
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)  # 1er appel : limite constatée
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)  # 2e : refusé sans requête
    assert client.route_paused_for("sales_summary") > 100
    assert await asyncio.wait_for(api.balance(), timeout=1) == 3  # les autres routes continuent
    assert client.session_ok
    assert [lvl for lvl, title in notifier.sent if "Route limitée" in title] == ["WARNING"]


@pytest.mark.asyncio
async def test_listings_respect_concurrent_limit(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    posted = []

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 4 + len(posted), "maxConcurrentAuctions": 5})
        if request.method == "POST" and request.url.path == "/api/marketplace":
            posted.append(request.content)
            return httpx.Response(200, json={"id": "new"})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    now = datetime.now(timezone.utc)
    for i in range(2):
        await db.schedule_listing(owned_card_id=f"o{i}", card_id="c", title=f"T{i}", rarity="C", base_amount=20,
                                  duration_minutes=120, post_at=now - timedelta(minutes=1),
                                  target_end=now + timedelta(hours=2), reason="test")
    await inv.post_due_listings()
    assert len(posted) == 1  # une seule place libre (4/5)
    rows = await db._all("SELECT status, post_at FROM listings ORDER BY id")
    assert rows[0]["status"] == "posted" and rows[1]["status"] == "scheduled"
    assert datetime.fromisoformat(rows[1]["post_at"]) > now + timedelta(hours=12)  # reportée
    await db.close()


@pytest.mark.asyncio
async def test_portfolio_simulated_buy_then_resale(tmp_path):
    """Scénario complet en DRY_RUN : mise, gain simulé, mise en vente, revente simulée, bénéfice."""
    from src.workers.inventory_manager import InventoryManager

    auction_id = "33333333-3333-3333-3333-333333333333"
    state = {"ended": False}

    def auction(**kw) -> dict:
        end = datetime.now(timezone.utc) + (timedelta(seconds=-120) if state["ended"] else timedelta(seconds=8))
        return {"id": auction_id, "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": end.isoformat(), "base_amount": 100, "current_bid": None, "current_bidder_id": None, **kw}

    def handler(request):
        path = request.url.path
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if path == "/api/marketplace" and "mine" in request.url.params:
            return httpx.Response(200, json={"auctions": [], "selling": []})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [] if state["ended"] else [auction()], "hasMore": False})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(200, json={"recent": [{"final_price": 400}] * 5})
        if path == f"/api/marketplace/{auction_id}":
            return httpx.Response(200, json={"auction": auction(status="settled_unsold")})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, position_list_at_peak=False, listing_min_duration_minutes=60)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))
    sniper, inv = AuctionSniper(ctx), InventoryManager(ctx)
    await sniper.setup()
    await inv.setup()

    await sniper.step()                       # budget ~540, estimation 400 : mise 100 acceptée
    assert (await db.last_bid(auction_id))["status"] == "simulated"
    state["ended"] = True
    await db._exec("UPDATE bids SET end_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await sniper._settle()                    # personne n'a misé : achat simulé remporté
    assert (await db.last_bid(auction_id))["status"] == "sim_won"
    assert (await db.positions_summary())["count"] == 1

    await inv.manage_positions()              # revente programmée au prix estimé
    await db._exec("UPDATE listings SET post_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    await inv.post_due_listings()
    listing = (await db._all("SELECT * FROM listings"))[0]
    assert listing["status"] == "simulated" and listing["base_amount"] == 400 and listing["bid_id"]
    await db._exec("UPDATE listings SET target_end = ?", ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()               # revente simulée : position close, bénéfice +300
    report = await db.report(datetime.now(timezone.utc) - timedelta(days=1))
    assert report["profit_simulated"] == 300 and report["positions_held"] == 0
    assert ("INFO", "Revente simulée") in notifier.sent
    await db.close()
