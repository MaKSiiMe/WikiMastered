"""Commandes Discord : ROI par jour, cartes demandées au sniper (/snipe)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from src import snipes
from src.core.models import Auction
from src.roi import roi_text
from src.workers.auction_sniper import AuctionSniper, Target, snipe_params
from tests.test_integration import make_client, make_context, seed_observed
from tests.test_sniper_discovery import sale


def test_roi_groups_resales_by_paris_day():
    """Une revente à 23:30 UTC compte pour le lendemain à Paris ; une invendue gardée est à part."""
    rows = [{"amount": 100, "sale_price": 150, "sold_at": "2026-10-25T10:00:00+00:00"},
            {"amount": 200, "sale_price": 180, "sold_at": "2026-10-25T23:30:00+00:00"},
            {"amount": 300, "sale_price": None, "sold_at": "2026-10-26T08:00:00+00:00"}]
    text = roi_text(rows, 2, date(2026, 10, 26), held=(3, 900))
    assert "2 revente(s), bénéfice +30 WB, soit +15 WB/jour · ROI +10.0 %" in text
    assert "En les comptant perdues : -270 WB" in text and "3 position(s) détenue(s), 900 WB" in text
    day25, day26 = text.splitlines()[-2:]
    assert day25.startswith("25/10") and "+50" in day25 and "+50 %" in day25
    assert day26.startswith("26/10") and "-20" in day26 and "1 (300 WB)" in day26


@pytest.mark.asyncio
async def test_snipes_are_stored_by_title(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404))
    ctx = await make_context(tmp_path, client, notifier)
    await snipes.add(ctx.db, "Napoléon Ier", 500, "l")
    await snipes.add(ctx.db, "napoleon ier", 800)  # même carte : la demande est remplacée
    found = await snipes.load(ctx.db)
    assert [s.label for s in found.values()] == ["napoleon ier ≤ 800 WB"]
    only_l = snipes.Snipe("Ada", 50, "L")
    assert only_l.matches(Auction.model_validate(sale("a", "Ada", "L", 60)))
    assert not only_l.matches(Auction.model_validate(sale("b", "Ada", "UR", 60)))
    assert await snipes.remove(ctx.db, "Napoléon Ier") is not None
    assert await snipes.load(ctx.db) == {}
    await ctx.db.close()


@pytest.mark.asyncio
async def test_sniper_searches_and_bids_on_a_requested_card(tmp_path):
    """Une carte demandée est cherchée par son titre, misée en priorité au-delà de SNIPER_MAX_BID et jusqu'à sa mise
    maximale, jamais pour la revente ; gagnée, elle n'est plus demandée."""
    market = {"n1": sale("n1", "Napoléon Ier", "UR", 300, base=900)}
    queries = []

    def handler(request):
        path, params = request.url.path, request.url.params
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 5000})
        if "mine" in params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(404, json={})
        if path.endswith("/bid"):
            return httpx.Response(200, json={"current_bid": 900, "bidder_balance": 4100})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        if path == "/api/marketplace":
            queries.append(params.get("q"))
            return httpx.Response(200, json={"auctions": list(market.values()) if params.get("q") else [],
                                             "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_resale_rarities=["UR"], sniper_max_bid=100,
                                   dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "card-n1", [3000, 3100, 2900], rarity="UR")  # revente rentable : ignorée
    await snipes.add(db, "Napoléon Ier", 1000)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    for _ in range(6):
        await sniper.step()
    assert "Napoléon Ier" in queries and "n1" in sniper._targets

    now = datetime.now(UTC)
    market["n1"]["end_at"] = (now + timedelta(seconds=16)).isoformat()
    sniper._targets["n1"] = Target(Auction.model_validate(market["n1"]), now - timedelta(seconds=30))
    assert sniper._next_due(snipe_params(client.settings, night=False), now).auction.id == "n1"
    await sniper.step()
    await sniper.step()
    bid = await db._one("SELECT * FROM bids WHERE auction_id = 'n1'")
    assert bid["amount"] == 900 and bid["reason"] == f"{snipes.REASON} ≤ 1000"

    await sniper._close_bid(bid, won=True, simulated=False)
    assert await snipes.load(db) == {} and not sniper._snipes
    assert (await db.get_bid(bid["id"]))["closed"] == 1  # gardée, pas une position à revendre
    assert ("SUCCESS", "Carte demandée obtenue !") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_discord_commands_are_registered(tmp_path):
    import sys

    import src.main
    from src.discord_bot import build_client, text_embed

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404), discord_owner_id=42)
    ctx = await make_context(tmp_path, client, notifier)
    bot = build_client(ctx, None, sys.modules["src.main"])
    names = {c.name for c in bot.tree.get_commands()}  # type: ignore[attr-defined]
    assert names == {"bilan", "roi", "solde", "positions", "ventes", "prix", "collection", "sniper", "snipe",
                     "snipes", "annuler"}
    assert src.main.roi_status  # les fonctions appelées par les commandes existent
    embed, file = text_embed("t", "ligne\n" * 1000)
    assert file is not None and len(embed.description) < 4096
    await bot.close()
    await ctx.db.close()


def test_bad_discord_ids_never_stop_the_bot():
    """Un pseudo à la place de l'identifiant : commandes coupées, mais le bot démarre."""
    from src.config import Settings

    s = Settings(_env_file=None, discord_owner_id="MaKSiiMe", discord_guild_id=" 123 ")
    assert s.discord_owner_id == -1 and s.discord_guild_id == 123
    assert Settings(_env_file=None, discord_owner_id="").discord_owner_id == 0
