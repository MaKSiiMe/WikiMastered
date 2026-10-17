"""Validation avant le passage en réel : simulation réaliste, bilan, premières vraies réponses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.main import validation_text
from src.workers.base import Context
from src.workers.inventory_manager import InventoryManager
from tests.test_integration import make_client, make_context, seed_observed


async def simulated_position(db: Database, base_amount: int) -> int:
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="a1", card_id="c1", title="Mème", rarity="C", is_shiny=False, amount=100,
                              reference_price=400, reason="revente ~400 WB", night=False, dry_run=True,
                              status="sim_won", end_at=now)
    lid = await db.schedule_listing(owned_card_id=f"sim-{bid}", card_id="c1", title="Mème", rarity="C",
                                    base_amount=base_amount, duration_minutes=360, post_at=now,
                                    target_end=now - timedelta(minutes=5), reason="position", bid_id=bid)
    await db.update_listing(lid, status="simulated")
    return bid


@pytest.mark.asyncio
async def test_simulated_resale_follows_the_observed_market(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [300, 310, 290])  # la tendance est retombée sous le prix de départ
    await inv.settle_listings()
    rows = await db._all("SELECT status, base_amount, relist_count FROM listings ORDER BY id")
    assert [r["status"] for r in rows] == ["sim_unsold", "scheduled"]
    assert rows[1]["base_amount"] == 360 and rows[1]["relist_count"] == 1  # remise à -10 %
    assert (await db.get_bid(bid))["closed"] == 0
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 2",
                   ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 3",
                   ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()  # 2 remises déjà faites : carte gardée
    assert (await db.get_bid(bid))["closed"] == 1 and (await db.get_bid(bid))["sale_price"] is None
    stats = await db.validation_stats(datetime.now(UTC) - timedelta(days=1), 2)
    assert stats["sim_kept"] == 1 and stats["sim_kept_cost"] == 100 and stats["sim_sold"] == 0
    assert ("WARNING", "Position invendue") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_simulated_resale_sells_when_the_market_pays(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [420, 400, 450])
    await inv.settle_listings()
    # Une enchère : les joueurs la font monter au prix du marché (médiane observée), pas seulement au départ.
    assert (await db.get_bid(bid))["sale_price"] == 420
    assert (await db.validation_stats(datetime.now(UTC), 2))["sim_profit"] == 320
    await db.close()


def test_list_price_is_cost_plus_margin():
    from src.workers.inventory_manager import list_price

    assert list_price(100, 0.15) == 115
    assert list_price(100, 0.15, fee=0.05) == 122  # la commission payée, il reste 15 % de marge
    assert list_price(3, 0.0) == 4                 # toujours au-dessus du prix d'achat
    # Achats simulés du 15/10 : moitié du bénéfice attendu (Lukaku 481 → ~881, Le Parrain 150 → ~550).
    assert list_price(481, 0.15, estimate=881, share=0.5) == 681
    assert list_price(150, 0.15, estimate=550, share=0.5) == 350
    assert list_price(100, 0.15, estimate=105, share=0.5) == 115  # tendance retombée : la marge minimale reste


@pytest.mark.asyncio
async def test_relist_never_below_purchase_price(tmp_path):
    """Achetée 100, en vente à 105 : la remise à −10 % (95) vendrait à perte, elle repart à 101."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    await simulated_position(db, 105)
    await seed_observed(db, "c1", [80, 90, 85])
    await inv.settle_listings()
    rows = await db._all("SELECT status, base_amount FROM listings ORDER BY id")
    assert [(r["status"], r["base_amount"]) for r in rows] == [("sim_unsold", 105), ("scheduled", 101)]
    await db.close()


def test_validation_verdicts():
    base = {"observed_today": 120, "observed_total": 900, "cards_priced": 80, "sim_bought": 14, "sim_held": 2,
            "uncertain": 0}
    assert "assez" in validation_text({**base, "sim_sold": 3, "sim_profit": 400, "sim_kept": 1, "sim_kept_cost": 90})
    assert "envisageable" in validation_text({**base, "sim_sold": 10, "sim_profit": 900, "sim_kept": 2,
                                              "sim_kept_cost": 300})
    assert "rester en simulation" in validation_text({**base, "sim_sold": 8, "sim_profit": 200, "sim_kept": 4,
                                                      "sim_kept_cost": 900})


@pytest.mark.asyncio
async def test_first_real_writes_are_kept_for_checking(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"ok": True, "auction": {"id": "x"}}),
                                   dry_run=False)
    for _ in range(4):
        await WikiMastersAPI(client).create_listing("o1", 400, 360)
    files = sorted(p.name for p in (tmp_path / "inspect" / "writes").iterdir())
    assert files == ["create_listing-1.json", "create_listing-2.json", "create_listing-3.json"]
    assert [t for _, t in notifier.sent] == ["Première réponse réelle : create_listing"]


def test_position_posted_late_morning_for_12_hours():
    """Revente publiée vers 11 h-12 h pour 12 h (fin vers 23 h-minuit), sans y penser."""
    import random
    from datetime import time

    from src.core.timeutils import PARIS
    from src.workers.inventory_manager import plan_position_time

    durations = [10, 30, 60, 180, 360, 720, 1440]
    night = datetime(2026, 10, 17, 3, 0, tzinfo=PARIS)  # achetée à 3 h : publiée le matin même
    post_at, minutes, end = plan_position_time(night, time(11), time(12), 720, durations, random.Random(1))
    assert post_at.date() == night.date() and time(11) <= post_at.time() <= time(12)
    assert minutes == 720 and end == post_at + timedelta(hours=12)
    noon = datetime(2026, 10, 17, 11, 30, tzinfo=PARIS)  # dans la fenêtre : avant midi, le jour même
    for seed in range(20):
        post_at = plan_position_time(noon, time(11), time(12), 720, durations, random.Random(seed))[0]
        assert noon <= post_at <= datetime(2026, 10, 17, 12, 0, tzinfo=PARIS)
    evening = datetime(2026, 10, 17, 21, 0, tzinfo=PARIS)  # achetée le soir : le lendemain matin
    post_at, _, _ = plan_position_time(evening, time(11), time(12), 720, durations)
    assert post_at.date() == evening.date() + timedelta(days=1) and post_at.hour == 11


@pytest.mark.asyncio
async def test_inventory_report_values_every_card(tmp_path, capsys):
    """« Sur les presque 10k cartes » (16/10) : chaque doublon reçoit un verdict, le détail part dans un CSV."""
    import csv

    from src.workers.inventory_manager import cli

    def copy(oid, card, rarity, **kw):
        return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": card.title(), "rarity": rarity},
                **kw}

    collection = ([copy(f"a{i}", "alpha", "C") for i in range(4)]        # cote 50 : à vendre
                  + [copy(f"b{i}", "beta", "C") for i in range(3)]       # cote 1 : 2 à défausser
                  + [copy(f"c{i}", "gamma", "R") for i in range(2)]      # cote 8, R jamais défaussée : gardée
                  + [copy(f"d{i}", "delta", "C") for i in range(2)]      # pas de cote
                  + [copy("a9", "alpha", "C", starred=True)])            # favori : l'exemplaire gardé
    cotes = {"alpha": {"C": {"average": 50}}, "beta": {"C": {"average": 1}}, "gamma": {"R": {"average": 8}},
             "delta": {}}

    def handler(request):
        if request.url.path == "/api/my-collection":
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"collection": collection if page == 0 else []})
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if request.url.path.endswith("/sales"):
            return httpx.Response(200, json={"summary": cotes[request.url.path.split("/")[-2]]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    await cli(inv.ctx, "report", apply=False, lookups=10)  # par la commande, comme dans la console
    out = capsys.readouterr().out
    assert "12 exemplaires, 4 cartes différentes" in out
    assert "À vendre      : 4 doublon(s)" in out and "~200 WB" in out
    assert "À défausser   : 2 doublon(s)" in out and "Gardés        : 1 doublon(s)" in out
    assert "Sans cote     : 1 doublon(s)" in out
    path = next(tmp_path.glob("inventaire-*.csv"))
    rows = {r["carte"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}
    assert rows["Alpha"]["exemplaires"] == "5" and rows["Alpha"]["à vendre"] == "4"
    assert rows["Beta"]["à défausser"] == "2" and rows["Delta"]["cote"] == ""
    await db.close()


@pytest.mark.asyncio
async def test_position_posted_now_and_old_schedules_advanced(tmp_path):
    """16/10 : 2 L sur 3 attendaient le lendemain 11 h. Une revente part dès que la carte est là."""
    from src.workers.inventory_manager import InventoryManager
    from tests.test_integration import make_client

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    tomorrow = now + timedelta(hours=17)
    await db.schedule_listing(owned_card_id="o1", card_id="c", title="Fabrice Arfi", rarity="L", base_amount=488,
                              duration_minutes=720, post_at=tomorrow, target_end=tomorrow + timedelta(hours=12),
                              reason="position", bid_id=1)
    await db.schedule_listing(owned_card_id="o2", card_id="d", title="doublon", rarity="C", base_amount=20,
                              duration_minutes=360, post_at=tomorrow, target_end=tomorrow, reason="cote")
    inv = InventoryManager(ctx)
    await inv.setup()
    rows = {r["title"]: r for r in await db._all("SELECT title, post_at, target_end FROM listings")}
    assert datetime.fromisoformat(rows["Fabrice Arfi"]["post_at"]) <= datetime.now(UTC)
    assert datetime.fromisoformat(rows["doublon"]["post_at"]) > now + timedelta(hours=16)  # doublon : inchangé
    post_at, minutes, end = inv._plan(now, position=True, at_peak=True)
    assert post_at == now and minutes == 360 and end == now + timedelta(hours=6)
    await db.close()


@pytest.mark.asyncio
async def test_favorite_copy_is_never_listed_and_abandoned_resale_resumes(tmp_path):
    """16/10 : Place publique refusée 3 fois « en favori », revente abandonnée et 270 WB immobilisés."""
    rows = [{"id": "fav", "card_id": "pp", "starred": True, "obtained_at": "2026-10-16T16:04:54+00:00",
             "card": {"id": "pp", "wikipedia_title": "Place publique", "rarity": "L"}}]

    def handler(request):
        return httpx.Response(200, json={"collection": rows})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="a", card_id="pp", title="Place publique", rarity="L", is_shiny=False,
                              amount=270, reference_price=442, reason="revente ~442 WB", night=False, dry_run=False,
                              status="won", end_at=now)
    for i in range(3):
        lid = await db.schedule_listing(owned_card_id=f"fav{i}", card_id="pp", title="Place publique", rarity="L",
                                        base_amount=356, duration_minutes=720, post_at=now, target_end=now,
                                        reason="position", bid_id=bid)
        await db.update_listing(lid, status="failed", error="Cette carte est en favori : retirez-la des favoris "
                                                            "pour la mettre en vente")
    await db.close_position(bid, None)  # « Revente abandonnée »

    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await inv.setup()  # reprise
    pending = await db.positions_to_list()
    assert [r["id"] for r in pending] == [bid] and await db.failed_listings_count(bid) == 0

    # Seul exemplaire en favori : pas de mise en vente, un seul avertissement.
    assert await inv._find_copy(pending[0]) is None and await inv._find_copy(pending[0]) is None
    assert notifier.sent.count(("WARNING", "Revente en attente : carte en favori")) == 1
    # Étoile retirée sur un autre exemplaire : c'est lui qui est vendu.
    rows.append({**rows[0], "id": "free", "starred": False})
    assert await inv._find_copy(pending[0]) == "free"
    await db.close()


@pytest.mark.asyncio
async def test_missing_copy_is_retried_slowly_then_closed(tmp_path):
    """16/10 : Place publique introuvable, collection relue et avertissement toutes les ~35 s (206 en 2 h)."""
    from src.workers import inventory_manager as im

    reads = []

    def handler(request):
        reads.append(request.url.path)
        return httpx.Response(200, json={"collection": []})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    bid = await db.record_bid(auction_id="a", card_id="pp", title="Place publique", rarity="L", is_shiny=False,
                              amount=270, reference_price=442, reason="revente ~442 WB", night=False, dry_run=False,
                              status="won", end_at=datetime.now(UTC))
    inv = InventoryManager(ctx)
    await inv.setup()
    await inv.manage_positions()
    await inv.manage_positions()  # pas de nouvelle lecture avant POSITION_COPY_RETRY
    assert len(reads) == 1
    for _ in range(im.POSITION_MAX_COPY_MISSES - 1):
        inv._copy_retry[bid] = (inv._copy_retry[bid][0], datetime.now(UTC))  # délai écoulé
        await inv.manage_positions()
    assert await db.positions_to_list() == []  # close après ~2 h d'absence
    assert notifier.sent.count(("WARNING", "Revente abandonnée")) == 1
    await db.close()


@pytest.mark.asyncio
async def test_relist_waits_for_the_unsold_card_to_come_back(tmp_path):
    """16/10 à 23:11 : Cyril Féraud invendue, remise à 375 refusée (« Vous ne possédez pas cette carte ») puis
    reprise au prix de départ (417) : la remise de 10 % était perdue. Un nouvel essai part 5 min plus tard, à 375."""
    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        return httpx.Response(400, json={"error": "Vous ne possédez pas cette carte"})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    lid = await db.schedule_listing(owned_card_id="cf", card_id="c", title="Cyril Féraud", rarity="L", base_amount=375,
                                    duration_minutes=720, post_at=now - timedelta(seconds=1), target_end=now,
                                    reason="position", bid_id=3, relist_count=1)
    inv = InventoryManager(ctx)
    await inv.post_due_listings()
    row = await db._one("SELECT status, post_at, base_amount FROM listings WHERE id = ?", (lid,))
    assert row["status"] == "scheduled" and row["base_amount"] == 375
    assert datetime.fromisoformat(row["post_at"]) > now + timedelta(minutes=4)
    assert await db.failed_listings_count(3) == 0 and not any(t == "Mise en vente refusée" for _, t in notifier.sent)

    await db.update_listing(lid, post_at=now - timedelta(seconds=1))  # toujours refusée : vrai échec
    await inv.post_due_listings()
    assert await db.failed_listings_count(3) == 1
    await db.close()


@pytest.mark.asyncio
async def test_report_separates_rejected_bids_collection_and_simulated_positions(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    async def bid(aid, status, amount, reason="revente ~500 WB", dry_run=False):
        await db.record_bid(auction_id=aid, card_id=aid, title=aid, rarity="L", is_shiny=False, amount=amount,
                            reference_price=500, reason=reason, night=False, dry_run=dry_run, status=status, end_at=now)

    await bid("p", "placed", 300)
    await bid("r1", "rejected", 200)
    await bid("r2", "rejected", 250)
    await bid("w", "won", 270)
    await bid("c", "won", 15, reason="collection : carte manquante ≤ 20")
    await db._exec("UPDATE bids SET closed = 1 WHERE auction_id = 'c'")  # gardée, comme le fait le sniper
    await bid("s", "sim_won", 600, dry_run=True)
    report = await db.report(now - timedelta(hours=1))
    assert (report["bids_placed"], report["bids_rejected"], report["bids_simulated"]) == (3, 2, 1)
    assert (report["auctions_won"], report["spent_won"]) == (1, 270)
    assert (report["won_collection"], report["spent_collection"]) == (1, 15)
    assert (report["positions_held"], report["capital_in_positions"]) == (1, 270)
    assert (report["positions_held_sim"], report["capital_simulated"]) == (1, 600)
    await db.close()
