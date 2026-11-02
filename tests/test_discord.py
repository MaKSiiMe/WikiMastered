"""Commandes Discord : ROI par jour, cartes demandées au sniper (/snipe)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from src import snipes
from src.core.models import Auction
from src.core import colors
from src.roi import roi, roi_discord, roi_text
from src.workers.auction_sniper import AuctionSniper, Target, snipe_params
from tests.test_integration import make_client, make_context, seed_observed
from tests.test_sniper_discovery import sale


def test_roi_groups_resales_by_paris_day():
    """Une revente à 23:30 UTC compte pour le lendemain à Paris ; une invendue gardée est à part."""
    rows = [{"amount": 100, "sale_price": 150, "sold_at": "2026-10-25T10:00:00+00:00"},
            {"amount": 200, "sale_price": 180, "sold_at": "2026-10-25T23:30:00+00:00"},
            {"amount": 300, "sale_price": None, "sold_at": "2026-10-26T08:00:00+00:00"},
            {"amount": 999, "sale_price": 9999, "sold_at": "2026-10-20T08:00:00+00:00"}]  # hors période
    report = roi(rows, 2, date(2026, 10, 26), held=(3, 900))
    text = roi_text(report)
    assert "2 revente(s), bénéfice +30 WB, soit +15 WB/jour · ROI +10,0 %" in text
    assert "En les comptant perdues : -270 WB" in text and "3 position(s) détenue(s), 900 WB" in text
    day25, day26 = text.splitlines()[-2:]
    assert day25.startswith("25/10") and "+50" in day25 and "+50 %" in day25
    assert day26.startswith("26/10") and "-20" in day26 and "1 (300 WB)" in day26
    head, columns, days = roi_discord(report)
    assert head.startswith("## +30 WB") and ("ROI", "+10,0 %") in columns
    assert days == ["`26/10 ███·····` 🔴 **-20 WB** · -10 % · 1 vente · 1 gardée",
                    "`25/10 ████████` 🟢 **+50 WB** · +50 % · 1 vente"]


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
    # 29/10 : The Weeknd L shiny (~6 fois la L normale) ; sans « shiny », le sniper aurait gardé une L normale.
    shiny = snipes.Snipe("The Weeknd", 35000, "L", shiny=True)
    assert shiny.label == "The Weeknd (L) ✨ shiny ≤ 35 000 WB"
    assert shiny.matches(Auction.model_validate({**sale("s", "The Weeknd", "L", 60), "is_shiny": True}))
    assert not shiny.matches(Auction.model_validate(sale("n", "The Weeknd", "L", 60)))
    await snipes.add(ctx.db, "The Weeknd", 35000, "L", shiny=True)
    assert (await snipes.load(ctx.db))[shiny.key].shiny
    assert await snipes.remove(ctx.db, "The Weeknd") is not None
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
    bot = build_client(ctx, [], sys.modules["src.main"])
    names = {c.name for c in bot.tree.get_commands()}  # type: ignore[attr-defined]
    assert names == {"bilan", "roi", "solde", "positions", "ventes", "prix", "collection", "sniper", "snipe",
                     "snipes", "annuler", "etat", "verif-faite", "pause", "reprise", "garder", "gardees", "liberer",
                     "sauvegarde",
                     "vendre", "plafond-collection", "carte", "opportunites", "top", "defausse", "doublons",
                     "valeur", "echange", "collection-joueur"}
    assert src.main.roi_status  # les fonctions appelées par les commandes existent
    # Boutons de /etat et /valeur : persistants, branchés même après un redémarrage.
    assert {"etat", "etatverif", "etatpause", "etatreprise", "recount", "valuehist", "stalefire", "stalekeep",
            "price"} <= set(bot.wm_handlers)
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


@pytest.mark.asyncio
async def test_paused_worker_skips_its_passes_until_resumed(tmp_path):
    """Un worker en pause ne fait aucun passage ; la pause est gardée en base, une pause finie est oubliée."""
    import asyncio

    from src import pauses
    from src.workers.base import Worker

    class Counter(Worker):
        name = "sniper"
        steps = 0

        async def step(self) -> float:
            self.steps += 1
            return 0.01

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404))
    ctx = await make_context(tmp_path, client, notifier)
    await pauses.pause(ctx, ["sniper", "garde"], None)
    assert ctx.pauses == {"sniper": None}  # la garde des sanctions n'est jamais en pause
    worker = Counter(ctx)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    assert worker.steps == 0 and worker.paused == "en pause jusqu'à /reprise"
    await pauses.resume(ctx, ["sniper"])
    await asyncio.sleep(1.5)
    assert worker.steps > 0 and worker.paused is None
    task.cancel()

    await pauses.pause(ctx, ["inventory"], datetime.now(UTC) + timedelta(hours=1))
    await pauses.pause(ctx, ["cotes"], datetime.now(UTC) - timedelta(minutes=1))
    ctx.pauses.clear()
    await pauses.load(ctx)  # redémarrage : la pause en cours revient, la finie est oubliée
    assert set(ctx.pauses) == {"inventory"}
    await ctx.db.close()


@pytest.mark.asyncio
async def test_state_shows_antibot_and_verification_done_unblocks_next_bid(tmp_path):
    from src.discord_bot import state_embed
    from src.workers.auction_sniper import _EPOCH

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"bidding": [], "balance": 0}))
    ctx = await make_context(tmp_path, client, notifier)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    assert not sniper.verification_done()  # rien n'attendait
    sniper._antibot_since = datetime.now(UTC) - timedelta(minutes=5)
    sniper._antibot_retry_at = datetime.now(UTC) + timedelta(minutes=10)
    ctx.pauses["inventory"] = None
    embed = state_embed(ctx, [sniper], datetime.now(UTC) - timedelta(hours=2))
    assert "Vérification anti-bot" in embed.description and embed.color.value == colors.WARN
    assert "🛑 sniper" in embed.fields[0].value and "À vérifier" not in embed.description  # dit une seule fois
    assert sniper.verification_done() and sniper._antibot_retry_at == _EPOCH
    await ctx.db.close()
