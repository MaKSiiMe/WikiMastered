"""Commande prices : ventes observées d'une carte, estimation du bot, achats et ventes en cours."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.main import prices_status
from tests.test_integration import ME, make_client, make_context

TITLE = "Liste des éclipses solaires du XXIe siècle"


def selling(aid: str, base: int, bid: int | None = None, seller: str = "s") -> dict:
    return {"id": aid, "card": {"id": "ecl21", "wikipedia_title": TITLE, "rarity": "L"}, "seller_id": seller,
            "status": "active", "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "base_amount": base, "current_bid": bid}


@pytest.mark.asyncio
async def test_prices_shows_observed_sales_estimate_and_competition(tmp_path):
    """18/10 : l'éclipse XXIe, achetée 882 et estimée ~1197, en vente à 1040 face à trois autres à 600-900."""
    market = [selling("m1", 600), selling("m2", 900), selling("me", 1040, seller=ME),
              {**selling("x", 275, 275), "card": {"id": "ecl20", "wikipedia_title": "Liste des éclipses solaires "
                                                  "du XXe siècle", "rarity": "L"}}]

    def handler(request):
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        if request.url.params.get("scope") == "summary":
            return httpx.Response(200, json={"summary": {"L": {"average": 1300}}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, base_amount, ended_at) "
        "VALUES (?, 'ecl21', ?, 'L', ?, ?, ?)",
        [(f"o{i}", TITLE, p, b, (now - timedelta(hours=i + 1)).isoformat())
         for i, (p, b) in enumerate([(1250, 900), (1197, 800), (1100, 1100), (1300, 700), (1150, 1000)])])
    await ctx.db.db.commit()
    await ctx.db.record_bid(auction_id="b", card_id="ecl21", title=TITLE, rarity="L", is_shiny=False, amount=882,
                            reference_price=1197, reason="revente ~1197 WB", night=False, dry_run=False,
                            status="won", end_at=now)
    text = await prices_status(ctx, TITLE.lower())
    assert text.startswith(TITLE + "\n")  # le vrai titre, pas celui tapé
    assert "L : 5 vente(s) observée(s) · estimation du bot ~1197 WB" in text and "moyenne du site : 1300 WB" in text
    assert "1100 WB  (départ 1100)  au prix de départ, sans surenchère" in text
    assert "réel : 882 WB, estimée ~1197" in text
    assert "En vente maintenant (3)" in text and "XXe" not in text  # l'autre carte n'est pas mêlée
    assert text.index("départ 600, aucune") < text.index("départ 900, aucune") < text.index("départ 1040, aucune")
    assert text.count("(toi)") == 1
    await ctx.db.close()


@pytest.mark.asyncio
async def test_prices_unknown_title(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"auctions": [], "hasMore": False}))
    ctx = await make_context(tmp_path, client, notifier)
    assert "ni vente observée ni vente en cours" in await prices_status(ctx, "Inconnue")
    await ctx.db.close()


async def seen(db, aid: str, min_bid: int, hours: float, *, card: str = "ecl21", seller: str = "s",
               shiny: int = 0) -> None:
    end = datetime.now(UTC) + timedelta(hours=hours)
    await db.upsert_auctions([{"auction_id": aid, "card_id": card, "title": TITLE, "rarity": "L", "is_shiny": shiny,
                               "current_bid": None, "base_amount": min_bid, "min_bid": min_bid,
                               "end_at": end.isoformat(), "seen_at": datetime.now(UTC).isoformat(),
                               "seller_id": seller}])


@pytest.mark.asyncio
async def test_resale_estimate_capped_by_the_cheapest_rival_sale(tmp_path):
    """18/10 : l'éclipse XXIe achetée 882 (estimée 1197) alors que d'autres étaient en vente à 600 et 900."""
    from src.core.models import Auction
    from src.core.pricing import Quote
    from src.workers.auction_sniper import AuctionSniper

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    sniper = AuctionSniper(ctx)
    target = Auction.model_validate({**selling("t", 800), "end_at": (datetime.now(UTC) + timedelta(seconds=15))
                                     .isoformat()})
    quote = Quote(1197.0, 5, 3, 1.5)
    await seen(ctx.db, "t", 800, 0.01)            # l'enchère visée elle-même
    await seen(ctx.db, "soon", 300, 0.5)          # finit avant que la revente soit en concurrence
    await seen(ctx.db, "mine", 400, 5, seller=ME)  # la mienne
    await seen(ctx.db, "shiny", 200, 5, shiny=1)
    await seen(ctx.db, "other", 100, 5, card="ecl20")
    assert await sniper._against_rivals(target, quote) == quote
    await seen(ctx.db, "m2", 900, 10)
    await seen(ctx.db, "m1", 600, 11)
    assert (await sniper._against_rivals(target, quote)).average == 600.0
    await ctx.db.close()


@pytest.mark.asyncio
async def test_resale_listed_just_under_the_cheapest_rival(tmp_path):
    """18/10 : l'éclipse XXIe en vente à 1040 face à d'autres à 600 et 900 restait sans acheteur."""
    from src.core.models import Auction
    from src.workers.inventory_manager import InventoryManager

    market: list[dict] = []
    posted: list[dict] = []

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.method == "GET":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={"auction_id": f"a{len(posted)}"})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    inv = InventoryManager(ctx)

    async def position(price: int, cost: int) -> tuple[int, int]:
        bid = await db.record_bid(auction_id=f"b{price}", card_id="ecl21", title=TITLE, rarity="L", is_shiny=False,
                                  amount=cost, reference_price=1197, reason="revente ~1197 WB", night=False,
                                  dry_run=False, status="won", end_at=now)
        lid = await db.schedule_listing(owned_card_id=f"o{price}", card_id="ecl21", title=TITLE, rarity="L",
                                        base_amount=price, duration_minutes=360, post_at=now - timedelta(seconds=1),
                                        target_end=now, reason="position", bid_id=bid)
        return bid, lid

    async def listing(lid):
        return await db._one("SELECT status, base_amount, post_at FROM listings WHERE id = ?", (lid,))

    market[:] = [selling("m1", 1200), selling("soon", 500) | {"end_at": (now + timedelta(minutes=20)).isoformat()},
                 selling("mine", 300, seller=ME)]
    _, lid = await position(1040, 882)
    await inv.post_due_listings()  # concurrente plus chère (les autres ne comptent pas) : prix gardé
    assert (await listing(lid))["base_amount"] == 1040 and "1040" in json.dumps(posted[-1])

    market[:] = [selling("m2", 900, 850)]  # mise actuelle 850 : on se place sous la prochaine mise permise
    _, lid = await position(1041, 500)
    await inv.post_due_listings()
    row = await listing(lid)
    assert row["status"] == "posted" and row["base_amount"] == Auction.model_validate(market[0]).min_bid - 1

    # Sous le prix d'achat (882) : publiée quand même, au prix d'achat (20/10 : Diana Spencer gardée hors vente toute
    # la nuit face à 605-807, vendue 2000 à la main).
    market[:] = [selling("m3", 600)]
    _, lid = await position(1042, 882)
    await inv.post_due_listings()
    row = await listing(lid)
    assert row["status"] == "posted" and row["base_amount"] == 883 and "883" in json.dumps(posted[-1])
    await db.close()
