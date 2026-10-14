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
        if path == "/api/marketplace":
            assert request.url.params["sort"] == "ending_soon"
            return httpx.Response(200, json={"auctions": [auction, {"broken": True}], "hasMore": False})
        if path.endswith("/sales"):
            return httpx.Response(200, json={"summary": {"C": {"average": 40, "count": 8}}})
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
    assert "deal" in bid["reason"]
    await sniper.step()  # pas de seconde simulation sur la même enchère
    assert sum(1 for level, title in notifier.sent if title == "Enchère simulée") == 1
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
