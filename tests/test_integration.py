"""Client et sniper face à un faux serveur (httpx.MockTransport) : aucun appel réseau réel."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.config import RARITIES, Settings
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


async def seed_observed(db: Database, card_id: str, prices: list[int], rarity: str = "C") -> None:
    """Ventes déjà observées par le sniper pour cette carte, dans cette rareté."""
    now = datetime.now(timezone.utc)
    await db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at) VALUES (?, ?, 't', ?, ?, ?)",
        [(f"seed-{card_id}-{i}", card_id, rarity, p, (now - timedelta(hours=i + 1)).isoformat())
         for i, p in enumerate(prices)])
    await db.db.commit()


class Recorder(DiscordNotifier):
    def __init__(self):
        super().__init__("", dry_run=True)
        self.sent: list[tuple[str, str]] = []

    def send(self, level, title, description="", **fields):
        self.sent.append((level.name, title))


def make_client(tmp_path: Path, handler, **overrides) -> tuple[WikiMastersClient, Recorder]:
    s = Settings(_env_file=None, **{
        "data_dir": tmp_path, "wm_cookie": "a=b", "wm_user_id": ME, "http_min_spacing_seconds": 0,
        "http_jitter_seconds": 0, "backoff_base_seconds": 0.01, "backoff_rate_limit_base_seconds": 0.01,
        # Les tests du sniper revendent toutes les raretés (par défaut : UR et L seulement).
        "sniper_resale_rarities": list(RARITIES),
        # Fenêtre de mise des tests écrits avant le 16/10 (enchères à ~15 s de la fin) : 20 s → 11 s.
        "sniper_window_seconds": 20, "sniper_min_seconds_left": 11, "night_window_seconds": 20, **overrides})
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
    end = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
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
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path == "/api/marketplace":
            assert request.url.params["sort"] in ("recent", "ending_soon")
            return httpx.Response(200, json={"auctions": [auction, {"broken": True}], "hasMore": False})
        if path.endswith("/sales") and "scope" not in request.url.params:
            return httpx.Response(200, json={"recent": [{"final_price": p} for p in (200, 190, 210, 50, 205)]})
        raise AssertionError(f"route inattendue {path}")

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))
    await seed_observed(db, auction["card"]["id"], [200, 210, 195])
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
    end = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()

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
            # `auctions` = marché général (leurre) ; seules les enchères de `bidding` sont les miennes.
            return httpx.Response(200, json={"bidding": [manual], "selling": [], "auctions": market})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={"error": "aucune vente"})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})

        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_rarity_caps={"SR": 15}, portfolio_max_positions=10,
                                   sniper_max_concurrent_bids=5)  # limite volontaire (le site n'en a pas)
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
    # Un seul message pour la session : 2 boosters, avec les 2 tirages remarquables (L shiny).
    titles = [t for _, t in notifier.sent if t != "Première réponse réelle : open_pack"]
    assert titles == ["Boosters ouverts : tirage remarquable !"]
    assert (await db.report(datetime.now(timezone.utc) - timedelta(hours=1)))["boosters_ouverts"] == 2
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
async def test_lone_server_error_pauses_only_its_route(tmp_path):
    from src.core.client import RateLimited

    def handler(request):
        if request.url.path.endswith("/sales"):
            return httpx.Response(500, json={"error": "boom"})
        if request.url.path == "/api/marketplace":
            return httpx.Response(502, text="<html>Bad gateway</html>")
        return httpx.Response(200, json={"balance": 3})

    client, _ = make_client(tmp_path, handler, backoff_base_seconds=60)
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)
    assert client.route_paused_for("sales_summary") > 50
    assert client._failures == 0  # pas de pause générale : le sniper continue
    assert await asyncio.wait_for(WikiMastersAPI(client).balance(), timeout=1) == 3
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)  # toujours en pause, sans requête

    # Des 500 sur deux routes sans succès entre les deux : panne générale, tout le bot attend.
    client, _ = make_client(tmp_path, handler, backoff_base_seconds=60)
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)
    task = asyncio.create_task(client.get("market_page"))
    await asyncio.sleep(0.05)
    assert client._failures == 1 and not task.done()
    task.cancel()


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
async def test_null_selling_count_falls_back_to_posted_listings(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": None, "maxConcurrentAuctions": 5})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    now = datetime.now(timezone.utc)
    await db.schedule_listing(owned_card_id="o1", card_id="c", title="T", rarity="C", base_amount=20,
                              duration_minutes=120, post_at=now, target_end=now + timedelta(hours=2), reason="test")
    await db._exec("UPDATE listings SET status = 'posted'")
    assert await inv.slot_usage() == (1, 5)
    await db.close()


@pytest.mark.asyncio
async def test_portfolio_simulated_buy_then_resale(tmp_path):
    """Scénario complet en DRY_RUN : mise, gain simulé, mise en vente, revente simulée, bénéfice."""
    from src.workers.inventory_manager import InventoryManager

    auction_id = "33333333-3333-3333-3333-333333333333"
    state = {"ended": False}

    def auction(**kw) -> dict:
        end = datetime.now(timezone.utc) + (timedelta(seconds=-120) if state["ended"] else timedelta(seconds=15))
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

    client, notifier = make_client(tmp_path, handler, listing_min_duration_minutes=60)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))
    await seed_observed(db, "c1", [400, 420, 390])
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

    await inv.manage_positions()              # départ : achat 100 + moitié du bénéfice attendu (400) = 250
    await db._exec("UPDATE listings SET post_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    await inv.post_due_listings()
    listing = (await db._all("SELECT * FROM listings"))[0]
    assert listing["status"] == "simulated" and listing["base_amount"] == 250 and listing["bid_id"]
    await db._exec("UPDATE listings SET target_end = ?", ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()               # enchère montée au prix du marché (400) : bénéfice +300
    report = await db.report(datetime.now(timezone.utc) - timedelta(days=1))
    assert report["profit_simulated"] == 300 and report["positions_held"] == 0
    assert ("INFO", "Revente simulée") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_ended_auctions_become_observed_sales(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=1)

    def row(aid, bid, seen_before_end):
        return {"auction_id": aid, "card_id": "gmmtv", "title": "GMMTV", "norm_title": "gmmtv", "rarity": "R",
                "is_shiny": 0, "current_bid": bid, "base_amount": 34, "min_bid": 1, "end_at": end.isoformat(),
                "seen_at": (end - timedelta(seconds=seen_before_end)).isoformat()}

    await db.upsert_auctions([row("a1", 157, 3), row("a2", None, 3), row("a3", 90, 3600)])
    assert await db.harvest_ended_auctions(now - timedelta(seconds=30)) == 1  # sans mise / vue trop tôt : ignorées
    assert await db.observed_prices("gmmtv", 5) == ([157], 1)
    assert await db._all("SELECT * FROM auctions_seen") == []
    await db.close()


@pytest.mark.asyncio
async def test_bids_unlimited_by_default(tmp_path):
    """Le site n'impose pas de limite aux mises : 94 enchères manuelles en cours ne bloquent rien."""
    end = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
    calls = []

    def handler(request):
        calls.append(request.url.path + "?" + str(request.url.query))
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            others = [{"id": f"55555555-5555-5555-5555-{i:012d}", "status": "active", "end_at": end,
                       "card": {"id": f"autre-{i}", "wikipedia_title": f"Autre {i}", "rarity": "C"},
                       "base_amount": 5} for i in range(94)]
            return httpx.Response(200, json={"bidding": others, "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [{
                "id": "33333333-3333-3333-3333-333333333333",
                "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
                "base_amount": 100, "current_bid": None, "current_bidder_id": None}], "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await seed_observed(db, "c1", [400, 410, 390])
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    await sniper.step()
    assert (await db.last_bid("33333333-3333-3333-3333-333333333333"))["status"] == "simulated"
    assert len(sniper._manual_auctions) == 94  # repérées comme tes mises, sans bloquer le bot
    await db.close()


@pytest.mark.asyncio
async def test_sniper_never_takes_cards_you_bid_on(tmp_path):
    """Tes mises manuelles (collections) : le bot ignore ces enchères et les autres ventes de la même carte."""
    end = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()

    def auction(aid: str, card: str) -> dict:
        return {"id": aid, "card": {"id": card, "wikipedia_title": card, "rarity": "C"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
                "base_amount": 100, "current_bid": None, "current_bidder_id": None}

    mine = auction("aaaaaaaa-0000-0000-0000-000000000001", "collection")   # tu mises dessus
    same = auction("aaaaaaaa-0000-0000-0000-000000000002", "collection")   # même carte, autre vente
    other = auction("aaaaaaaa-0000-0000-0000-000000000003", "meme")

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [mine], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [mine, same, other], "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    for card in ("collection", "meme"):
        await seed_observed(db, card, [400, 410, 390])
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    await sniper.step()
    rows = await db._all("SELECT auction_id FROM bids")
    assert [r["auction_id"] for r in rows] == [other["id"]]
    await db.close()


@pytest.mark.asyncio
async def test_protected_tags_are_never_sold_or_discarded(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    def row(oid, tags):
        return {"id": oid, "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Date", "rarity": "C"},
                "tags": [{"id": "t", "name": t} for t in tags]}

    def handler(request):
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": [row("o1", []), row("o2", ["Date"]), row("o3", [])]})
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if request.url.path.endswith("/sales"):
            return httpx.Response(200, json={"summary": {"C": {"average": 50}}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, protected_tags=["date"])
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    _, excluded, _, _ = await inv.snapshot()
    assert excluded == {"o2"}  # étiquette « date » : jamais vendue ni défaussée (casse ignorée)
    await db.close()


@pytest.mark.asyncio
async def test_first_rate_limit_alert_even_right_after_boot(tmp_path, monkeypatch):
    """time.monotonic() part du démarrage de la machine : l'alerte part même 5 s après le boot."""
    import src.core.client as client_module

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 5.0)
    client._backoff("balance: HTTP 429", limited=True)
    client._route_backoff("sales_summary", 0)
    assert ("CRITICAL", "Limitation de requêtes (429)") in notifier.sent
    assert ("WARNING", "Route limitée : sales_summary") in notifier.sent


@pytest.mark.asyncio
async def test_daily_budget_applies_in_dry_run(tmp_path):
    """La simulation respecte SNIPER_DAILY_BUDGET et prévient s'il est sous le budget d'une position."""
    end = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
    market = [{"id": f"33333333-3333-3333-3333-{i:012d}", "card": {"id": f"c{i}", "wikipedia_title": f"C{i}",
               "rarity": "C"}, "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
               "end_at": end, "base_amount": 100, "current_bid": None, "current_bidder_id": None} for i in range(3)]

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_daily_budget=250)
    db = Database(tmp_path / "t.db")
    await db.connect()
    for i in range(3):
        await seed_observed(db, f"c{i}", [400, 410, 390])
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    assert ("WARNING", "Sniper : budget du jour trop petit") in notifier.sent
    await sniper.step()
    rows = await db._all("SELECT amount FROM bids WHERE status = 'simulated'")
    assert len(rows) == 2  # 100 + 100 ; la troisième dépasserait 250
    assert ("INFO", "Sniper : budget du jour atteint") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_pack_sessions_every_15_to_90_minutes(tmp_path):
    from src.workers.pack_opener import PackOpener

    opened = []

    def handler(request):
        opened.append(request.url.path)
        return httpx.Response(200, json={"cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "C"}],
                                         "packs_remaining": 0})

    client, notifier = make_client(tmp_path, handler, dry_run=False, pack_between_opens_min_seconds=0,
                                   pack_between_opens_max_seconds=0)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    opener = PackOpener(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await opener.setup()
    assert 60 <= await opener.step() <= 600 and not opened  # pas d'ouverture dès le démarrage
    opener._next_open = datetime.now(timezone.utc)
    await opener.step()
    assert len(opened) == 1
    gap = (opener._next_open - datetime.now(timezone.utc)).total_seconds() / 60
    assert 14.9 <= gap <= 90
    assert await opener.step() > 0 and len(opened) == 1  # on attend la prochaine session
    await db.close()


@pytest.mark.asyncio
async def test_trend_alerts_cards_under_position_budget(tmp_path):
    from src.workers.wiki_trends import Trend, WikiTrends

    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    def sale(aid: str, title: str, current: int) -> dict:
        return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "C"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
                "base_amount": 10, "current_bid": current, "current_bidder_id": None}

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [sale("a1", "Squid Game", 120), sale("a2", "Squid Game", 900),
                                                          sale("a3", "Autre", 50)], "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await seed_observed(db, "card-Squid Game", [400, 380, 420])
    api = WikiMastersAPI(client)
    trends = WikiTrends(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    budget = await trends.max_price()
    assert budget == 594  # 3300 × 0,9 / 5, et non plus 30 WB fixes
    trend = Trend("Squid_Game", 50000, 2000, 25.0, 90000)
    lines = await trends.check_market(trend, budget)
    assert len(lines) == 1 and "mise min 132 WB" in lines[0] and "estimation ~400 WB" in lines[0]
    assert await trends.check_market(trend, budget) == []  # chaque vente n'est signalée qu'une fois
    signals = await db._all("SELECT auction_id, source FROM trend_signals ORDER BY auction_id")
    assert [(r["auction_id"], r["source"]) for r in signals] == [("a1", "wikipedia"), ("a2", "wikipedia")]
    await db.close()


GOOGLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>squid game saison 3</title><ht:approx_traffic>1000+</ht:approx_traffic></item>
<item><title>décharge</title><ht:approx_traffic>200+</ht:approx_traffic></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_google_trends_feed_to_market_signals(tmp_path):
    from src.workers.google_trends import GoogleTrends, parse_feed

    assert [(t.term, t.traffic) for t in parse_feed(GOOGLE_FEED)] == [("squid game saison 3", 1000), ("décharge", 200)]
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    searches = []

    def handler(request):
        if request.url.host == "trends.google.com":
            return httpx.Response(200, text=GOOGLE_FEED)
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace":
            searches.append(request.url.params["q"])
            title = "Squid Game" if "squid" in request.url.params["q"] else "Décharge publique"
            return httpx.Response(200, json={"auctions": [{
                "id": f"a-{title}", "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
                "base_amount": 100, "current_bid": None, "current_bidder_id": None}], "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, trends_notify=True)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    gt = GoogleTrends(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await gt.setup()
    gt.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await gt.step() > 600
    assert ("INFO", "Tendances Google : 1 carte(s) en vente") in notifier.sent  # pas « Décharge publique »
    await gt.step()
    assert searches == ["squid game saison 3", "décharge"]  # termes déjà vus : pas recherchés deux fois
    assert await db.terms_stats("google", datetime.now(timezone.utc) - timedelta(hours=1)) == (2, 1)
    await db.close()


@pytest.mark.asyncio
async def test_trend_alert_one_line_per_card(tmp_path):
    from src.workers.trend_common import scan_market

    end = datetime.now(timezone.utc) + timedelta(hours=2)

    def sale(aid, current, hours):
        return {"id": aid, "card": {"id": "greta", "wikipedia_title": "Greta Thunberg", "rarity": "UR"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": (end + timedelta(hours=hours)).isoformat(), "base_amount": 10, "current_bid": current,
                "current_bidder_id": None}

    def handler(request):
        return httpx.Response(200, json={"auctions": [sale("g1", 400, 0), sale("g2", 150, 3), sale("g3", 275, 1)],
                                         "hasMore": False})

    client, notifier = make_client(tmp_path, handler)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))
    lines = await scan_market(ctx, source="wikipedia", term="Greta Thunberg", detail="vues x123", max_price=590)
    assert len(lines) == 1 and "3 ventes, mise min 165 à 441 WB" in lines[0] and "première fin" in lines[0]
    assert len(await db._all("SELECT * FROM trend_signals")) == 3  # chaque vente reste un signal mesuré
    await db.close()


@pytest.mark.asyncio
async def test_pack_retries_in_5_to_10_minutes_after_verification(tmp_path, caplog):
    from src.workers.pack_opener import PackOpener

    state = {"verify": True}

    def handler(request):
        if state["verify"]:
            return httpx.Response(200, json={"human_verification_required": True, "packs_remaining": 8})
        return httpx.Response(200, json={"cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "C"}],
                                         "packs_remaining": 0})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    opener = PackOpener(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await opener.setup()
    caplog.set_level(logging.INFO)
    for _ in range(3):  # trois échecs de suite : une seule alerte
        opener._next_open = datetime.now(timezone.utc)
        await opener.step()
        gap = (opener._next_open - datetime.now(timezone.utc)).total_seconds() / 60
        assert 4.9 <= gap <= 10
    assert [t for _, t in notifier.sent if "vérification" in t] == ["Paquets : vérification humaine demandée"]
    # Chaque nouvel essai dit pourquoi (le 15/10 au soir : une heure de « ouverture échouée » sans raison).
    retries = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ouverture échouée")]
    assert len(retries) == 3 and all("(vérification humaine en attente)" in m for m in retries)
    assert opener.blocked and opener.blocked.startswith("vérification humaine en attente depuis ")
    state["verify"] = False
    opener._next_open = datetime.now(timezone.utc)
    await opener.step()
    assert ("SUCCESS", "Paquets : vérification levée") in notifier.sent
    assert opener.blocked is None
    assert (opener._next_open - datetime.now(timezone.utc)).total_seconds() / 60 >= 14.9  # retour au rythme normal
    await db.close()


@pytest.mark.asyncio
async def test_human_checks_are_recorded_with_their_context(tmp_path):
    from src.workers.pack_opener import PackOpener, checks_report

    state = {"verify": False}

    def handler(request):
        if state["verify"]:
            return httpx.Response(200, json={"human_verification_required": True, "packs_remaining": 8})
        return httpx.Response(200, json={"cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "C"}],
                                         "packs_remaining": 0})

    client, notifier = make_client(tmp_path, handler, dry_run=False, pack_between_opens_min_seconds=0,
                                   pack_between_opens_max_seconds=0)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    opener = PackOpener(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await opener.setup()

    async def attempt():
        opener._next_open = datetime.now(timezone.utc)
        await opener.step()

    state["verify"] = True
    await attempt()  # 1re vérification, juste après le démarrage
    await attempt()  # même vérification, toujours en attente : pas de doublon
    state["verify"] = False
    await attempt()  # levée, 1 booster
    await attempt()  # 1 booster de plus
    state["verify"] = True
    await attempt()  # 2e vérification
    rows = await db.pack_checks()
    assert len(rows) == 2 and rows[0]["cleared_at"] and rows[1]["cleared_at"] is None
    report = await checks_report(db)
    assert report[0] == "2 vérification(s), dont 2 dans les 15 min après un démarrage du bot"
    assert report[1].startswith("Entre deux vérifications : médiane 2 (de 2 à 2) booster(s)")
    assert "2 booster(s) et 0 min depuis la précédente" in report[3] and report[3].endswith("en attente")
    await db.close()


@pytest.mark.asyncio
async def test_empty_stock_refusal_keeps_pending_verification(tmp_path):
    """15/10 : vérification en attente, puis 403 « Plus de paquets disponibles », puis vérification à nouveau.
    Le refus ne lève pas la vérification (pas de 2e alerte) et vaut stock vide (rythme normal, pas d'avertissement)."""
    from src.workers.pack_opener import PackOpener

    state = {"answer": "verify"}

    def handler(request):
        if request.url.path == "/api/wikibidous":  # sonde de session après le 403
            return httpx.Response(200, json={"balance": 1})
        if state["answer"] == "verify":
            return httpx.Response(200, json={"human_verification_required": True})
        if state["answer"] == "empty":
            return httpx.Response(403, json={"error": "Plus de paquets disponibles"})
        return httpx.Response(200, json={"cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "C"}],
                                         "packs_remaining": 0})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    opener = PackOpener(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await opener.setup()

    async def attempt(answer):
        state["answer"] = answer
        opener._next_open = datetime.now(timezone.utc)
        opener.state.last_regen_at = None  # stock supposé plein : la tentative part tout de suite
        await opener.step()
        return (opener._next_open - datetime.now(timezone.utc)).total_seconds() / 60

    await attempt("verify")
    assert await attempt("empty") >= 14.9  # stock vide : rythme normal, pas un nouvel essai dans 5 à 10 min
    assert opener.state.remaining == 0 and opener.blocked  # toujours en attente
    await attempt("verify")
    assert [t for _, t in notifier.sent if "vérification" in t] == ["Paquets : vérification humaine demandée"]
    assert len(await db.pack_checks()) == 1
    await attempt("ok")
    assert ("SUCCESS", "Paquets : vérification levée") in notifier.sent and opener.blocked is None
    await db.close()


@pytest.mark.asyncio
async def test_lone_timeout_pauses_only_its_route(tmp_path):
    """15/10 17:50 : un ReadTimeout sur la cote figeait tout le bot 35 s."""
    from src.core.client import RateLimited

    def handler(request):
        if request.url.path.endswith("/sales"):
            raise httpx.ReadTimeout("lent", request=request)
        return httpx.Response(200, json={"balance": 3})

    client, _ = make_client(tmp_path, handler, backoff_base_seconds=60)
    with pytest.raises(RateLimited):
        await client.get("sales_summary", card_id="x", wait=False)
    assert client.route_paused_for("sales_summary") > 50 and client._failures == 0
    assert await asyncio.wait_for(WikiMastersAPI(client).balance(), timeout=1) == 3
