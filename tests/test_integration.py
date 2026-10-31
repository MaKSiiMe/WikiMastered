"""Client et sniper face à un faux serveur (httpx.MockTransport) : aucun appel réseau réel."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
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
    now = datetime.now(UTC)
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
        "http_jitter_seconds": 0, "http_write_spacing_seconds": 0,
        "backoff_base_seconds": 0.01, "backoff_rate_limit_base_seconds": 0.01,
        # Les tests du sniper revendent toutes les raretés (par défaut : UR et L seulement).
        "sniper_resale_rarities": list(RARITIES),
        # Fenêtre de mise des tests écrits avant le 16/10 (enchères à ~15 s de la fin) : 20 s → 11 s.
        "sniper_window_seconds": 20, "sniper_min_seconds_left": 11, "night_window_seconds": 20,
        # Tests écrits avant le 17/10 : mise au minimum (SNIPER_BID_PUSH a ses propres tests).
        "sniper_bid_push": 0.0,
        # Avant le 21/10 : mises basses face à l'estimation (SNIPER_MIN_BID_SHARE a ses propres tests).
        "sniper_min_bid_share": 0.0, **overrides})
    notifier = Recorder()
    client = WikiMastersClient(s, Endpoints.load(ROOT / "config" / "endpoints.yaml"), notifier)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies=client._http.cookies)
    return client, notifier


async def make_context(tmp_path: Path, client: WikiMastersClient, notifier: Recorder) -> Context:
    """Contexte complet des workers, sur une base neuve du dossier du test."""
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    return Context(client.settings, client, api, db, notifier, PriceService(api, db, 30))


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
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
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
    sniper._next_digest = datetime.now(UTC)
    sniper._flush_digest(datetime.now(UTC))
    assert ("INFO", "Sniper : enchères simulées (1)") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_sniper_respects_concurrent_bid_limit(tmp_path):
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()

    def auction(i: int, base: int) -> dict:
        return {
            "id": f"33333333-3333-3333-3333-{i:012d}",
            "card": {"id": f"44444444-4444-4444-4444-{i:012d}", "wikipedia_title": f"{i + 1} mars", "rarity": "SR"},
            "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
            "base_amount": base, "current_bid": None, "current_bidder_id": None,
        }

    market = [auction(i, base) for i, base in enumerate([14, 3, 9, 5, 12, 2, 7])]
    manual = {**auction(99, 1), "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}

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

    client, notifier = make_client(tmp_path, handler, collection_sets=["jours"], collection_max_price=15,
                                   portfolio_max_positions=10,
                                   sniper_max_concurrent_bids=5)  # limite volontaire (le site n'en a pas)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set("collection", {"owned": [], "missing": [f"{i + 1} mars" for i in range(7)], "total": 366})
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    rows = await db._all("SELECT amount FROM bids WHERE status = 'simulated' ORDER BY amount")
    # 1 enchère manuelle déjà en cours -> 4 places ; les plus grosses marges (15 - mise) d'abord.
    assert [r["amount"] for r in rows] == [2, 3, 5, 7]
    assert sniper.free_slots(datetime.now(UTC)) == 0
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
    assert not [title for _, title in notifier.sent if "Route limitée" in title]  # refus isolé : logs seulement


@pytest.mark.asyncio
async def test_route_limit_alert_only_after_repeated_refusals(tmp_path):
    """18/10 : sales_summary refusée une fois toutes les ~80 min, reprise 1 min plus tard. Message à partir du
    3e refus de suite (sans succès entre les deux), une fois par heure au plus."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    client._route_backoff("sales_summary", 0)
    client._on_route_success("sales_summary")  # repartie : le compte repart de zéro
    client._route_backoff("sales_summary", 0)
    client._route_backoff("sales_summary", 0)
    assert not notifier.sent
    client._route_backoff("sales_summary", 0)
    client._route_backoff("sales_summary", 0)
    assert notifier.sent == [("WARNING", "Route limitée : sales_summary")]


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
async def test_second_copy_waits_for_the_first_sale_and_abandoned_one_is_reopened(tmp_path):
    """29/10 : 2 Zidane ; le 2e, refusé 3 fois (« déjà engagée dans un échange en attente ») pendant que le 1er était
    en vente, a été abandonné. Il attend désormais la fin de l'autre vente, et l'abandonné est repris au démarrage."""
    from src.workers.inventory_manager import InventoryManager

    posted = []

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.method == "POST" and request.url.path == "/api/marketplace":
            posted.append(request.content)
            return httpx.Response(201, json={"auction_id": f"a{len(posted)}"})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    now = datetime.now(UTC)
    first = await db.schedule_listing(owned_card_id="z1", card_id="zz", title="Zinédine Zidane", rarity="L",
                                      base_amount=13427, duration_minutes=360, post_at=now - timedelta(minutes=1),
                                      target_end=now + timedelta(hours=6), reason="cote 13427")
    second = await db.schedule_listing(owned_card_id="z2", card_id="zz", title="Zinédine Zidane", rarity="L",
                                       base_amount=13310, duration_minutes=720, post_at=now - timedelta(minutes=1),
                                       target_end=now + timedelta(hours=12), reason="doublon")
    await inv.post_due_listings()
    assert len(posted) == 1
    rows = {r["id"]: r for r in await db._all("SELECT id, status, post_at FROM listings")}
    assert rows[first]["status"] == "posted" and rows[second]["status"] == "scheduled"
    assert datetime.fromisoformat(rows[second]["post_at"]) > now + timedelta(hours=6)

    # Position abandonnée avant le correctif : rouverte, ses refus ne comptent plus ; trop ancienne : laissée.
    async def abandoned(card: str, at: datetime) -> int:
        failed_at = (at + timedelta(minutes=10)).isoformat()
        bid = await db.record_bid(auction_id=f"b{card}", card_id=card, title=card, rarity="L", is_shiny=False,
                                  amount=12750, reference_price=13427, reason="achat manuel", night=False,
                                  dry_run=False, status="won", end_at=at)
        await db._exec("INSERT INTO listings (owned_card_id, card_id, title, rarity, base_amount, duration_minutes, post_at, "
                       "status, created_at, posted_at, settled_at, dry_run) "
                       "VALUES (?, ?, ?, 'L', 13427, 360, ?, 'sold', ?, ?, ?, 0)",
                       (f"{card}-1", card, card, at.isoformat(), at.isoformat(), at.isoformat(),
                        (at + timedelta(hours=6)).isoformat()))
        await db._exec("INSERT INTO listings (owned_card_id, card_id, title, rarity, base_amount, duration_minutes, post_at, "
                       "status, created_at, error, bid_id, dry_run) VALUES (?, ?, ?, 'L', 13310, 720, ?, 'failed', ?, "
                       "'Cette carte est déjà engagée dans un échange en attente', ?, 0)",
                       (f"{card}-2", card, card, failed_at, failed_at, bid))
        await db.close_position(bid, None)
        return bid

    recent, old = await abandoned("recent", now - timedelta(days=1)), await abandoned("old", now - timedelta(days=13))
    assert await db.reopen_same_card_positions(now - timedelta(days=7)) == 1
    assert (await db.get_bid(recent))["closed"] == 0 and (await db.get_bid(old))["closed"] == 1
    assert await db.failed_listings_count(recent) == 0
    await db.close()

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

    # SELL_ANYTIME=false : un doublon vise le soir ; sans place, il est reporté au lendemain.
    client, notifier = make_client(tmp_path, handler, dry_run=False, sell_anytime=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    now = datetime.now(UTC)
    for i in range(2):
        await db.schedule_listing(owned_card_id=f"o{i}", card_id=f"c{i}", title=f"T{i}", rarity="C", base_amount=20,
                                  duration_minutes=120, post_at=now - timedelta(minutes=1),
                                  target_end=now + timedelta(hours=2), reason="test")
    await inv.post_due_listings()
    assert len(posted) == 1  # une seule place libre (4/5)
    rows = await db._all("SELECT status, post_at FROM listings ORDER BY id")
    assert rows[0]["status"] == "posted" and rows[1]["status"] == "scheduled"
    assert datetime.fromisoformat(rows[1]["post_at"]) > now + timedelta(hours=12)  # doublon : reporté au lendemain

    # Revente d'une position (16/10, Place publique reportée de 24 h) : nouvel essai dès SELL_CHECK_MINUTES.
    await db.schedule_listing(owned_card_id="p1", card_id="pp", title="Place publique", rarity="L", base_amount=356,
                              duration_minutes=720, post_at=now - timedelta(minutes=1),
                              target_end=now + timedelta(hours=12), reason="position", bid_id=7)
    await inv.post_due_listings()
    row = await db._one("SELECT status, post_at FROM listings WHERE title = 'Place publique'")
    retry = datetime.fromisoformat(row["post_at"])
    assert row["status"] == "scheduled" and now < retry <= now + timedelta(minutes=11)
    await db.close()


@pytest.mark.asyncio
async def test_null_selling_count_falls_back_to_posted_listings(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": None, "maxConcurrentAuctions": 5})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    now = datetime.now(UTC)
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
        end = datetime.now(UTC) + (timedelta(seconds=-120) if state["ended"] else timedelta(seconds=15))
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "c1", [400, 420, 390])
    sniper, inv = AuctionSniper(ctx), InventoryManager(ctx)
    await sniper.setup()
    await inv.setup()

    await sniper.step()                       # budget ~540, estimation 400 : mise 100 acceptée
    assert (await db.last_bid(auction_id))["status"] == "simulated"
    state["ended"] = True
    await db._exec("UPDATE bids SET end_at = ?", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await sniper._settle()                    # personne n'a misé : achat simulé remporté
    assert (await db.last_bid(auction_id))["status"] == "sim_won"
    assert (await db.positions_summary())["count"] == 1

    await inv.manage_positions()              # départ : achat 100 + moitié du bénéfice attendu (400) = 250
    await db._exec("UPDATE listings SET post_at = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
    await inv.post_due_listings()
    listing = (await db._all("SELECT * FROM listings"))[0]
    assert listing["status"] == "simulated" and listing["base_amount"] == 250 and listing["bid_id"]
    await db._exec("UPDATE listings SET target_end = ?", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()               # enchère montée au prix du marché (400) : bénéfice +300
    report = await db.report(datetime.now(UTC) - timedelta(days=1))
    assert report["profit_simulated"] == 300 and report["positions_held"] == 0
    assert ("INFO", "Revente simulée") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_ended_auctions_become_observed_sales(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)
    end = now - timedelta(minutes=1)

    def row(aid, bid, seen_before_end):
        return {"auction_id": aid, "card_id": "gmmtv", "title": "GMMTV", "rarity": "R",
                "is_shiny": 0, "current_bid": bid, "base_amount": 34, "min_bid": 1, "end_at": end.isoformat(),
                "seen_at": (end - timedelta(seconds=seen_before_end)).isoformat()}

    await db.upsert_auctions([row("a1", 157, 3), row("a2", None, 3), row("a3", 90, 3600)])
    assert await db.harvest_ended_auctions(now - timedelta(seconds=30)) == 1  # sans mise / vue trop tôt : ignorées
    assert await db.observed_prices("gmmtv", 5) == ([157], 1)
    assert (await db._one("SELECT seen_gap FROM observed_sales"))["seen_gap"] == 3
    assert await db._all("SELECT * FROM auctions_seen") == []
    # Réglée plus haut (mise de dernière seconde, enchère prolongée) : le prix relu au règlement remplace la lecture.
    await db.correct_observed_sale("a1", 290, "rival")
    assert await db.observed_prices("gmmtv", 5) == ([290], 1)
    assert (await db._one("SELECT buyer_id FROM observed_sales"))["buyer_id"] == "rival"
    await db.close()


@pytest.mark.asyncio
async def test_older_market_reads_do_not_overwrite_newer_ones(tmp_path):
    """25/10 : Tom Holland, relue surenchérie et prolongée à 15:31:29, remontrée deux fois par la liste du marché en
    tête à l'ancienne fin."""
    from src.core.models import Auction
    from src.workers.auction_sniper import is_older

    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    def row(end, bid, bidder):
        return {"auction_id": "th", "card_id": "c", "title": "Tom Holland", "rarity": "L", "is_shiny": 0,
                "current_bid": bid, "base_amount": 100, "min_bid": bid + 1, "end_at": end.isoformat(),
                "seen_at": now.isoformat(), "bidder_id": bidder}

    fresh = row(now + timedelta(seconds=53), 2985, "rival")
    await db.upsert_auctions([fresh])
    await db.upsert_auctions([row(now - timedelta(seconds=7), 2714, "moi")])  # lecture en retard
    kept = await db._one("SELECT current_bid, bidder_id, end_at FROM auctions_seen")
    assert (kept["current_bid"], kept["bidder_id"], kept["end_at"]) == (2985, "rival", fresh["end_at"])
    await db.upsert_auctions([row(now + timedelta(seconds=90), 3300, "autre")])  # plus récente : prise
    assert (await db._one("SELECT current_bid FROM auctions_seen"))["current_bid"] == 3300
    await db.close()

    def auction(end, bid):
        return Auction.model_validate({"id": "th", "card_id": "c", "status": "active", "base_amount": 100,
                                       "current_bid": bid, "end_at": end.isoformat(),
                                       "card": {"id": "c", "wikipedia_title": "Tom Holland", "rarity": "L"}})
    assert is_older(auction(now, 2714), auction(now + timedelta(seconds=53), 2985))
    assert not is_older(auction(now + timedelta(seconds=53), 2985), auction(now, 2714))


def test_position_copy_has_the_bought_rarity_and_version():
    """Une carte existe en plusieurs raretés : un exemplaire plus récent d'une autre rareté ou shiny (tiré d'un paquet)
    n'est jamais vendu à la place de la position, ni un exemplaire protégé, ni un exemplaire qu'on avait déjà."""
    from src.core.models import OwnedCard
    from src.workers.inventory_manager import newest_sellable, same_copies

    placed = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def copy(oid, rarity, minutes, shiny=False, tags=()):
        return OwnedCard.model_validate({"id": oid, "card_id": "c", "is_shiny": shiny,
                                         "obtained_at": (placed + timedelta(minutes=minutes)).isoformat(),
                                         "tags": [{"name": t} for t in tags],
                                         "card": {"id": "c", "wikipedia_title": "Fellation", "rarity": rarity}})
    copies = [copy("c-paquet", "C", 30), copy("l-shiny", "L", 20, shiny=True), copy("l-ancienne", "L", -14400),
              copy("l-achetee", "L", 2), copy("l-date", "L", 40, tags=["date"])]
    normal_l = same_copies(copies, "L", False)
    assert [c.id for c in normal_l] == ["l-ancienne", "l-achetee", "l-date"]
    assert newest_sellable(normal_l, {"date"}, placed) == "l-achetee"
    assert newest_sellable(normal_l, {"date"}) == "l-achetee"
    assert newest_sellable(same_copies(copies, "L", True), {"date"}, placed) == "l-shiny"
    assert newest_sellable([copies[2]], {"date"}, placed) == "l-ancienne"  # seule de sa rareté : gardée en repli


def test_position_floor_allows_a_small_loss_after_unsold_listings():
    from src.workers.inventory_manager import position_floor
    s = Settings(_env_file=None)
    assert [position_floor(6119, n, s) for n in (0, 1, 2, 3)] == [6120, 6120, 5508, 5508]
    assert position_floor(6119, 5, Settings(_env_file=None, position_max_loss=0)) == 6120


@pytest.mark.asyncio
async def test_bids_unlimited_by_default(tmp_path):
    """Le site n'impose pas de limite aux mises : 94 enchères manuelles en cours ne bloquent rien."""
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "c1", [400, 410, 390])
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    assert (await db.last_bid("33333333-3333-3333-3333-333333333333"))["status"] == "simulated"
    assert len(sniper._manual_auctions) == 94  # repérées comme tes mises, sans bloquer le bot
    await db.close()


@pytest.mark.asyncio
async def test_sniper_never_takes_cards_you_bid_on(tmp_path):
    """Tes mises manuelles (collections) : le bot ignore ces enchères et les autres ventes de la même carte."""
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()

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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    for card in ("collection", "meme"):
        await seed_observed(db, card, [400, 410, 390])
    sniper = AuctionSniper(ctx)
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
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
    for _ in range(3):
        client._route_backoff("sales_summary", 0)
    assert ("CRITICAL", "Limitation de requêtes (429)") in notifier.sent
    assert ("WARNING", "Route limitée : sales_summary") in notifier.sent


@pytest.mark.asyncio
async def test_daily_budget_applies_in_dry_run(tmp_path):
    """La simulation respecte SNIPER_DAILY_BUDGET et prévient s'il est sous le budget d'une position."""
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    for i in range(3):
        await seed_observed(db, f"c{i}", [400, 410, 390])
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    assert ("WARNING", "Sniper : budget du jour trop petit") in notifier.sent
    await sniper.step()
    rows = await db._all("SELECT amount FROM bids WHERE status = 'simulated'")
    assert len(rows) == 2  # 100 + 100 ; la troisième dépasserait 250
    assert ("INFO", "Sniper : budget du jour atteint") in notifier.sent
    await db.close()


GOOGLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>squid game saison 3</title><ht:approx_traffic>1000+</ht:approx_traffic></item>
<item><title>décharge</title><ht:approx_traffic>200+</ht:approx_traffic></item>
</channel></rss>"""


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
