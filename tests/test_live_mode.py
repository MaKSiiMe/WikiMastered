"""Mode mixte : tout est réel sauf ce qui est listé dans SIMULATE (ex. le sniper)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config import Settings
from src.workers.auction_sniper import AuctionSniper
from tests.test_integration import ME, make_client, make_context, seed_observed


def test_simulate_and_live_writes_groups():
    s = Settings(_env_file=None, dry_run=False, simulate=["sniper"])
    assert not s.is_live("place_bid") and s.is_live("collection_bid") and s.is_live("create_listing")
    assert s.is_live("open_pack") and s.mode_label == "RÉEL (simulé : sniper)"
    d = Settings(_env_file=None, dry_run=True, live_writes=["packs", "ventes"])
    assert d.is_live("open_pack") and d.is_live("create_listing") and not d.is_live("place_bid")
    assert Settings(_env_file=None, collection_sets='"jours"').collection_sets == ["jours"]  # guillemets retirés


def test_positions_live_only_during_sniper_live_hours():
    # 17/10 : le jour, le chasseur de l'extension achète les L depuis le navigateur ; le bot les simule.
    s = Settings(_env_file=None, dry_run=False, sniper_live_start="23:00", sniper_live_end="09:00")
    day, night = datetime(2026, 10, 17, 12, tzinfo=UTC), datetime(2026, 10, 17, 23, tzinfo=UTC)
    assert not s.sniper_live_now(day) and s.sniper_live_now(night)  # 14:00 et 01:00 à Paris
    assert s.mode_label == "RÉEL, positions réelles de 23:00 à 09:00"
    assert s.is_live("place_bid") == s.sniper_live_now() and s.is_live("collection_bid")
    assert Settings(_env_file=None, dry_run=False).sniper_live_now(day)  # sans réglage : toujours réelles


@pytest.mark.asyncio
async def test_sniper_simulated_while_collection_bids_are_real(tmp_path):
    now = datetime.now(UTC)

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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "card-Mème", [400, 410, 390], rarity="R")
    await db.kv_set("collection", {"owned": [], "missing": ["14 octobre"], "total": 366})
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    status = {r["auction_id"]: (r["status"], r["dry_run"]) for r in await db._all("SELECT * FROM bids")}
    assert status == {"pos": ("simulated", 1), "col": ("placed", 0)}
    assert posted == ["/api/marketplace/col/bid"]  # seule la mise de collection est partie
    assert sniper.recap()[1:] == ["2 enchère(s) examinée(s) : misée 1 · misée (collection) 1"]
    assert sniper.recap()[1:] == ["Aucune enchère dans la fenêtre de mise."]  # remis à zéro
    sniper._flush_digest(now + timedelta(days=1))
    assert ("INFO", "Sniper : 2 enchères (1 placée(s), 1 simulée(s))") in notifier.sent
    assert client.user_id == ME
    await db.close()


@pytest.mark.asyncio
async def test_duplicates_fill_free_slots_during_the_day(tmp_path, monkeypatch):
    from src.core import timeutils
    from src.workers import inventory_manager
    from src.workers.inventory_manager import InventoryManager

    # En journée : le soir, une vente de 6 h finirait la nuit et viserait le lendemain soir.
    morning = datetime.now(timeutils.PARIS).replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(inventory_manager, "now_local", lambda: morning)

    slots = {"selling": 3}
    reads = []

    def copy(oid, card):
        return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": card, "rarity": "C"}}

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": slots["selling"], "maxConcurrentAuctions": 5})
        if request.url.path == "/api/my-collection":
            reads.append(1)
            return httpx.Response(200, json={"collection": [copy(f"{c}{i}", c) for c in "ABC" for i in (1, 2)]})
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, simulate=["sniper"], auto_list=True,
                                   sell_price_lookups_per_run=0)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    for card, prices in (("A", [300, 310]), ("B", [100, 90]), ("C", [50, 60])):
        await seed_observed(db, card, prices)
    inv = InventoryManager(ctx)
    await inv.setup()
    assert await inv.fill_free_slots() == 2  # 2 places libres sur 5 : les 2 doublons les mieux cotés
    rows = await db._all("SELECT card_id, post_at FROM listings ORDER BY base_amount DESC")
    assert [r["card_id"] for r in rows] == ["A", "B"]
    soon = morning + timedelta(hours=10)
    assert all(datetime.fromisoformat(r["post_at"]) < soon for r in rows)  # pas d'attente jusqu'au soir
    slots["selling"] = 4  # une vente terminée libère une place (4 en cours + 2 programmées = plein)
    assert await inv.fill_free_slots() == 0
    await db._exec("UPDATE listings SET status = 'posted'")
    slots["selling"] = 4
    assert await inv.fill_free_slots() == 1 and len(reads) == 1  # la collection n'est pas relue
    assert not notifier.sent  # pas de message Discord pour les doublons
    await db.close()


@pytest.mark.asyncio
async def test_simulated_positions_never_block_real_collection_bids(tmp_path, caplog):
    """Des positions simulées qui dépassent le solde bloquent les mises simulées, pas les vraies."""
    now = datetime.now(UTC)

    def sale(aid, title, base):
        return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": (now + timedelta(seconds=15)).isoformat(), "base_amount": base,
                "current_bid": None, "current_bidder_id": None}

    def handler(request):
        if request.method == "POST":
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "card-Mème", [400, 410, 390], rarity="R")
    await db.kv_set("collection", {"owned": [], "missing": ["14 octobre"], "total": 366})
    await db.record_bid(auction_id="vieille", card_id="x", title="x", rarity="C", is_shiny=False, amount=6400,
                        reference_price=None, reason="revente", night=False, dry_run=True, status="sim_won",
                        end_at=now - timedelta(days=1))  # position simulée détenue : 6400 WB > solde
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    caplog.set_level("INFO", logger="worker.sniper")
    await sniper.step()
    logged = caplog.text
    assert "collection : 14 octobre (R) en vente, mise min 10 WB" in logged
    assert "collection : 14 octobre à 10 WB" in logged and ": misée (collection)" in logged
    status = {r["auction_id"]: r["status"] for r in await db._all("SELECT * FROM bids WHERE auction_id <> 'vieille'")}
    assert status == {"col": "placed"}  # la vraie mise de collection passe, la position simulée non
    assert "solde ou budget du jour insuffisant 1" in sniper.recap()[1]
    await db.close()
