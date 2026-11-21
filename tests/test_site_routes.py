"""Routes relevées dans le code du site le 21/11 : historique des prix (cote de secours), baisse d'une revente sans mise
à mi-parcours (reprice), finalisation d'une enchère que le site tarde à régler (settle), paramètres répétés."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.core.endpoints import Endpoints
from src.core.models import Auction
from src.core.pricing import history_quotes
from src.workers import settle as settle_module
from src.workers.inventory_manager import InventoryManager, position_floor
from tests.test_integration import make_client, make_context
from tests.test_session import cookie_for

HISTORY = {"computed_at": "2026-11-21T05:00:00Z", "by_rarity": {
    "UR": {"weekly": [{"start": "2026-10-13", "avg": 900, "min": 800, "max": 1000, "count": 2},
                      {"start": "2026-10-20", "avg": 600, "min": 600, "max": 600, "count": 1},
                      {"start": "2026-10-27", "avg": 0, "min": None, "max": None, "count": 0},
                      {"start": "2026-11-03", "avg": 700, "min": 650, "max": 750, "count": 2},
                      {"start": "2026-11-10", "avg": 640, "min": 640, "max": 640, "count": 1},
                      {"start": "2026-11-17", "avg": 660, "min": 600, "max": 720, "count": 4}]},
    "SR": {"weekly": []}, "XX": {"weekly": [{"start": "2026-11-17", "avg": 5, "count": 1}]}}}


def test_history_quote_is_the_weighted_mean_of_the_last_weeks_with_sales():
    """Moyenne des 4 dernières semaines avec des ventes, pondérée : (600 + 1400 + 640 + 2640) / 8 = 660."""
    quotes = history_quotes(HISTORY)
    assert set(quotes) == {"UR"} and quotes["UR"].count == 8 and quotes["UR"].average == pytest.approx(660)
    assert history_quotes({"by_rarity": "?"}) == {} and history_quotes({}) == {}


@pytest.mark.asyncio
async def test_history_quote_is_fetched_once_never_for_a_shiny_and_paused_when_refused(tmp_path):
    calls = {"n": 0, "status": 200}

    def handler(request):
        if not request.url.path.endswith("/price-history"):
            return httpx.Response(200, json={"balance": 0})  # vérification de session après un 403
        calls["n"] += 1
        return httpx.Response(calls["status"], json=HISTORY if calls["status"] == 200 else {"error": "PRO"})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    prices = ctx.prices
    assert await prices.history_quote("c1", "UR", fetch=False) is None and calls["n"] == 0
    assert (await prices.history_quote("c1", "UR")).average == pytest.approx(660)
    assert (await prices.history_quote("c1", "UR", fetch=False)).count == 8 and calls["n"] == 1  # gardée
    assert await prices.history_quote("c1", "SR") is None and calls["n"] == 1
    assert await prices.history_quote("c2", "UR", shiny=True) is None and calls["n"] == 1
    calls["status"] = 403
    assert await prices.history_quote("c3", "UR") is None and calls["n"] == 2
    assert await prices.history_quote("c4", "UR") is None and calls["n"] == 2  # coupée une heure
    await ctx.db.close()


def test_list_params_are_repeated():
    eps = Endpoints.load(Path("config/endpoints.yaml"))
    req = eps.build("market_page", sort="recent", rarity=["L", "UR"])
    assert req.params["rarity"] == ["L", "UR"]
    assert "rarity" not in eps.build("market_page", rarity=[]).params
    assert eps.build("collection_page", page=0, rarity="L").params["rarity"] == "L"
    assert "rarity" not in eps.build("collection_page", page=0).params


def _selling(base: int, *, bid: int | None = None, hours_left: float = 2.0, status: str = "active") -> dict:
    return {"id": "auc1", "card": {"id": "c-eve", "wikipedia_title": "Eve", "rarity": "L"}, "seller_id": "a1",
            "status": status, "end_at": (datetime.now(UTC) + timedelta(hours=hours_left)).isoformat(),
            "base_amount": base, "current_bid": bid, "current_bidder_id": "x" if bid else None}


async def _posted_position(ctx, base: int, *, posted_hours_ago: float, hours_left: float, copy: str = "o1") -> int:
    db = ctx.db
    bid_id = await db.record_bid(auction_id="won1", card_id="c-eve", title="Eve", rarity="L", is_shiny=False,
                                 amount=240, reference_price=400.0, reason="revente ~400 WB, bénéfice ~160",
                                 night=False, dry_run=False, status="won", end_at=datetime.now(UTC) - timedelta(hours=7))
    now = datetime.now(UTC)
    listing = await db.schedule_listing(owned_card_id=copy, card_id="c-eve", title="Eve", rarity="L", base_amount=base,
                                        duration_minutes=360, post_at=now, target_end=now, reason="position",
                                        bid_id=bid_id)
    await db.update_listing(listing, status="posted", auction_id="auc1",
                            posted_at=now - timedelta(hours=posted_hours_ago),
                            target_end=now + timedelta(hours=hours_left))
    return listing


@pytest.mark.asyncio
async def test_position_without_bid_at_half_time_is_repriced_once(tmp_path):
    """Vente à 375, sans mise au bout de 4 h sur 6 : départ baissé à 356 (× 0,95) sur place, une seule fois."""
    sent, auction = [], {"a": _selling(375)}

    def handler(request):
        if request.method == "POST":
            sent.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"auction": auction["a"]})

    client, notifier = make_client(tmp_path, handler, dry_run=False, position_reprice=True,
                                   position_relist_discount=0.95, wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    listing = await _posted_position(ctx, 375, posted_hours_ago=4, hours_left=2)
    inv = InventoryManager(ctx)
    assert await inv.reprice_listings() == 1
    assert sent == [("/api/marketplace/auc1/reprice", {"new_base_amount": 356})]
    assert (await ctx.db._one("SELECT base_amount FROM listings WHERE id = ?", (listing,)))["base_amount"] == 356
    assert await inv.reprice_listings() == 0 and len(sent) == 1  # une seule fois
    await ctx.db.close()


@pytest.mark.asyncio
async def test_reprice_waits_half_time_skips_a_bid_and_never_goes_below_the_floor(tmp_path):
    sent, auction = [], {"a": _selling(250, bid=250)}

    def handler(request):
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"auction": auction["a"]})

    client, notifier = make_client(tmp_path, handler, dry_run=False, position_reprice=True,
                                   position_relist_discount=0.95, wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    inv = InventoryManager(ctx)
    listing = await _posted_position(ctx, 250, posted_hours_ago=1, hours_left=5)
    assert await inv.reprice_listings() == 0  # pas encore à mi-parcours
    await ctx.db.update_listing(listing, posted_at=datetime.now(UTC) - timedelta(hours=4),
                                target_end=datetime.now(UTC) + timedelta(hours=2))
    assert await inv.reprice_listings() == 0 and sent == []  # une mise : rien
    other = await _posted_position(ctx, 250, posted_hours_ago=4, hours_left=2, copy="o2")
    auction["a"] = _selling(250)
    floor = position_floor(240, 0, ctx.settings)
    assert await inv.reprice_listings() == 1  # 250 × 0,95 = 238 : arrêtée au plancher (achetée 240)
    assert sent == [{"new_base_amount": floor}] and 238 < floor < 250
    assert (await ctx.db._one("SELECT repriced_at FROM listings WHERE id = ?", (other,)))["repriced_at"]
    await ctx.db.close()


class _Worker:
    def __init__(self, live: bool = True):
        import logging
        self.log = logging.getLogger("test")
        self.calls: list[str] = []
        self.settings = type("S", (), {"settle_stuck_auctions": live})()
        worker = self

        class _Api:
            async def settle(self, aid):
                worker.calls.append(aid)
                from src.core.client import MutationResult
                return MutationResult(200, {})
        self.api = _Api()


@pytest.mark.asyncio
async def test_stuck_auction_is_settled_once_after_a_minute():
    settle_module._asked.clear()
    worker = _Worker()
    fresh = Auction.model_validate(_selling(100, hours_left=-0.5 / 60))
    stuck = Auction.model_validate({**_selling(100, hours_left=-3 / 60), "id": "stuck"})
    assert not await settle_module.nudge_settle(worker, fresh)  # finie depuis 30 s
    assert await settle_module.nudge_settle(worker, stuck)
    assert not await settle_module.nudge_settle(worker, stuck) and worker.calls == ["stuck"]
    off = _Worker(live=False)
    stuck2 = Auction.model_validate({**_selling(100, hours_left=-3 / 60), "id": "stuck2"})
    assert not await settle_module.nudge_settle(off, stuck2) and off.calls == []


@pytest.mark.asyncio
async def test_owned_copies_are_searched_in_the_bought_rarity_first(tmp_path):
    """La copie d'une position est cherchée dans sa seule rareté (rarity=L) ; rien trouvé : toutes les raretés."""
    reads, found = [], {"L": False}

    def handler(request):
        rarity = request.url.params.get("rarity")
        reads.append(rarity)
        row = {"id": "o1", "card_id": "c-eve", "obtained_at": datetime.now(UTC).isoformat(),
               "card": {"id": "c-eve", "wikipedia_title": "Eve", "rarity": "L"}}
        return httpx.Response(200, json={"collection": [row] if rarity != "L" or found["L"] else []})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    inv = InventoryManager(ctx)
    found["L"] = True
    assert [c.id for c in await inv._owned_copies("c-eve", "Eve", "L")] == ["o1"] and reads == ["L"]
    reads.clear()
    found["L"] = False
    assert [c.id for c in await inv._owned_copies("c-eve", "Eve", "L")] == ["o1"] and reads == ["L", None]
    await ctx.db.close()
