"""Validation avant le passage en réel : simulation réaliste, bilan, premières vraies réponses."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.core.timeutils import PARIS
from src.main import validation_text
from src.workers.base import Context
from src.workers.inventory_manager import InventoryManager
from tests.test_integration import make_client, make_context, seed_observed


async def simulated_position(db: Database, base_amount: int, auction_id: str = "a1", minutes: int = 360) -> int:
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id=auction_id, card_id="c1", title="Mème", rarity="C", is_shiny=False, amount=100,
                              reference_price=400, reason="revente ~400 WB", night=False, dry_run=True,
                              status="sim_won", end_at=now)
    lid = await db.schedule_listing(owned_card_id=f"sim-{bid}", card_id="c1", title="Mème", rarity="C",
                                    base_amount=base_amount, duration_minutes=minutes, post_at=now,
                                    target_end=now - timedelta(minutes=5), reason="position", bid_id=bid)
    await db.update_listing(lid, status="simulated")
    return bid


async def past_unsold(db: Database, bid: int, count: int, minutes: int = 360) -> None:
    """`count` ventes sans acheteur de `minutes` déjà passées pour cette position (étapes de l'échelle de remises)."""
    now = datetime.now(UTC)
    for i in range(count):
        lid = await db.schedule_listing(owned_card_id=f"sim-{bid}-{i}", card_id="c1", title="Mème", rarity="C",
                                        base_amount=400, duration_minutes=minutes, post_at=now, target_end=now,
                                        reason="position", bid_id=bid)
        await db.update_listing(lid, status="sim_unsold")


@pytest.mark.asyncio
async def test_simulated_resale_follows_the_observed_market(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500), position_list_duration_minutes=360)
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
    await inv.settle_listings()  # 3 invendus (POSITION_GIVE_UP_AFTER) : le bot renonce, jamais de revente à 1 WB
    assert not await db._all("SELECT id FROM listings WHERE status = 'scheduled'")
    assert (await db._one("SELECT MIN(base_amount) AS low FROM listings"))["low"] == 324
    closed = await db.get_bid(bid)
    assert closed["closed"] == 1 and closed["sale_price"] is None
    assert ("WARNING", "Position invendue") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_floor_after_relists_when_the_bot_never_gives_up(tmp_path):
    """POSITION_GIVE_UP_AFTER=0 : après 2 remises à -10 %, revente au plancher, jusqu'à POSITION_MAX_LOSS (10 %)
    sous le prix d'achat (101 commission comprise)."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500), position_give_up_after=0)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [300, 310, 290])
    await past_unsold(db, bid, 2)
    await db._exec("UPDATE listings SET relist_count = 2 WHERE status = 'simulated'")
    await inv.settle_listings()
    row = await db._one("SELECT status, base_amount, relist_count FROM listings ORDER BY id DESC LIMIT 1")
    assert (row["status"], row["base_amount"], row["relist_count"]) == ("scheduled", 91, 3)
    await db.close()


@pytest.mark.asyncio
async def test_hourly_resales_step_down_every_six_hours(tmp_path):
    """01/11 : reventes d'1 h. Les remises suivent le temps passé en vente (POSITION_STEP_MINUTES=360), pas le nombre
    de ventes : même prix 5 h durant, −10 % à 6 h et 12 h, gardée à 18 h (comme 3 ventes de 6 h sans acheteur)."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 400, minutes=60)
    await seed_observed(db, "c1", [300, 310, 290])
    prices = []
    for _ in range(17):
        await inv.settle_listings()
        row = await db._one("SELECT id, base_amount, duration_minutes FROM listings ORDER BY id DESC LIMIT 1")
        prices.append(row["base_amount"])
        await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = ?",
                       ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), row["id"]))
    assert prices[:5] == [400] * 5 and prices[5:11] == [360] * 6 and prices[11:17] == [324] * 6
    await inv.settle_listings()  # 18e heure sans acheteur : le bot renonce, aucune vente de plus
    assert not await db._all("SELECT id FROM listings WHERE status = 'scheduled'")
    assert (await db.get_bid(bid))["closed"] == 1
    await db.close()


def test_unsold_steps_count_time_on_sale():
    from src.workers.inventory_manager import unsold_steps

    assert unsold_steps([60] * 5, 360) == 0 and unsold_steps([60] * 6, 360) == 1
    assert unsold_steps([720, 360], 360) == 2  # une vente de 12 h compte pour une étape, comme avant le 01/11
    assert unsold_steps([60, 60], 0) == 2  # 0 : une étape par vente


@pytest.mark.asyncio
async def test_unsold_position_kept_only_when_every_slot_is_taken(tmp_path):
    """20/10 : Jorge Jesus et Les Feux de l'amour, gardées après 3 essais, se sont vendues à la main. Le bot ne garde
    une position invendue que si toutes les places de vente sont prises."""
    selling = {"n": 5}

    def handler(request):
        return httpx.Response(200, json={"sellingCount": selling["n"], "maxConcurrentAuctions": 5})

    client, notifier = make_client(tmp_path, handler, position_give_up_after=0)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 101)
    await past_unsold(db, bid, 2)
    await inv.settle_listings()  # 5 ventes sur 5 : carte gardée
    assert (await db.get_bid(bid))["closed"] == 1
    assert ("WARNING", "Position invendue") in notifier.sent
    stats = await db.validation_stats(datetime.now(UTC) - timedelta(days=1), 2)
    assert stats["sim_kept"] == 1 and stats["sim_kept_cost"] == 100 and stats["sim_sold"] == 0

    selling["n"] = 4
    bid = await simulated_position(db, 101, "a2")
    await past_unsold(db, bid, 2)
    await inv.settle_listings()  # une place libre : remise en vente au plancher (-10 % après 2 invendus)
    rows = await db._all("SELECT status, base_amount FROM listings WHERE bid_id = ? ORDER BY id", (bid,))
    assert [r["status"] for r in rows] == ["sim_unsold"] * 3 + ["scheduled"] and rows[-1]["base_amount"] == 91
    assert (await db.get_bid(bid))["closed"] == 0
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
async def test_position_listed_12h_until_5_positions_are_held(tmp_path):
    """20/10 : tant que moins de 5 L attendent leur revente, une vente de 12 h est plus vue ; au-delà, 6 h pour
    faire tourner les 5 places de vente."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}), dry_run=False,
                                   position_list_duration_minutes=360, position_list_long_below=5)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)

    async def held(aid: str, *, dry_run: bool = False) -> None:
        await db.record_bid(auction_id=aid, card_id=f"c-{aid}", title=aid, rarity="L", is_shiny=False, amount=500,
                            reference_price=900, reason="revente ~900 WB", night=False, dry_run=dry_run,
                            status="sim_won" if dry_run else "won", end_at=now)

    inv = InventoryManager(ctx)
    for i in range(4):
        await held(f"a{i}")
    await held("sim", dry_run=True)  # ventes réelles : une position simulée n'occupe pas tes places
    night = datetime(2026, 10, 26, 2, 0, tzinfo=PARIS)  # fin à 08 h ou 14 h : aucune heure évitée
    assert await inv.position_minutes(night) == 720
    _, minutes, end = inv._plan(now, position=True, at_peak=True, position_minutes=720)
    assert minutes == 720 and end == now + timedelta(hours=12)
    await held("a4")
    assert await inv.position_minutes(night) == 360
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
    assert post_at == now and minutes == 60 and end == now + timedelta(hours=1)
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
    assert len(reads) == 2  # recherche par titre, puis derniers exemplaires obtenus
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
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": []})  # pas encore revenue
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


def owned(oid: str, card_id: str, title: str, rarity: str = "L") -> dict:
    return {"id": oid, "card_id": card_id, "starred": False, "obtained_at": "2026-10-18T03:48:00+00:00",
            "card": {"id": card_id, "wikipedia_title": title, "rarity": rarity}}


@pytest.mark.asyncio
async def test_bought_copy_found_among_recent_cards_when_the_title_search_misses(tmp_path):
    """18/10 : 5G (L, achetée 443) introuvable 2 h par la recherche « q=5G », revente abandonnée."""
    others = [owned(f"o{i}", f"c{i}", f"Réseau 5G n°{i}", "C") for i in range(50)]

    def handler(request):
        q, page = request.url.params.get("q"), int(request.url.params.get("page", 0))
        if q:  # la recherche renvoie d'autres cartes : 5G n'est pas sur la 1re page
            return httpx.Response(200, json={"collection": others if page == 0 else []})
        return httpx.Response(200, json={"collection": [owned("x5g", "5g", "5G")]})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    bid = await ctx.db.record_bid(auction_id="a", card_id="5g", title="5G", rarity="L", is_shiny=False, amount=443,
                                  reference_price=664, reason="revente ~664 WB", night=True, dry_run=False,
                                  status="won", end_at=datetime.now(UTC))
    inv = InventoryManager(ctx)
    assert await inv._find_copy((await ctx.db.positions_to_list())[0]) == "x5g"
    await ctx.db.close()


@pytest.mark.asyncio
async def test_relist_follows_the_copy_back_under_a_new_id(tmp_path):
    """18/10 : Liste des éclipses solaires invendue, remise à 936 refusée 2 fois (« Vous ne possédez pas cette
    carte ») puis reprise à 1040, son prix de départ : l'exemplaire était revenu sous un autre identifiant."""
    posted = []

    def handler(request):
        path = request.url.path
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": [owned("new", "ecl", "Liste des éclipses solaires")]})
        if request.method == "GET":  # ventes concurrentes : aucune
            return httpx.Response(200, json={"auctions": [], "hasMore": False})
        body = json.loads(request.content)
        posted.append(body)
        if "new" not in json.dumps(body):
            return httpx.Response(400, json={"error": "Vous ne possédez pas cette carte"})
        return httpx.Response(201, json={"auction_id": "auc"})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    lid = await db.schedule_listing(owned_card_id="old", card_id="ecl", title="Liste des éclipses solaires",
                                    rarity="L", base_amount=936, duration_minutes=360,
                                    post_at=now - timedelta(seconds=1), target_end=now, reason="position", bid_id=3,
                                    relist_count=1)
    inv = InventoryManager(ctx)
    await inv.post_due_listings()  # refusée : l'exemplaire revenu est retrouvé, nouvel essai tout de suite
    await inv.post_due_listings()
    row = await db._one("SELECT status, owned_card_id, base_amount FROM listings WHERE id = ?", (lid,))
    assert (row["status"], row["owned_card_id"], row["base_amount"]) == ("posted", "new", 936)
    assert len(posted) == 2 and not any(t == "Mise en vente refusée" for _, t in notifier.sent)
    await db.close()


@pytest.mark.asyncio
async def test_unsold_resale_is_relisted_under_the_returned_copy(tmp_path):
    """25-26/10 : chaque remise en vente partait sous l'ancien identifiant, refusée « vous ne possédez pas cette
    carte », puis reprise 1 min plus tard. L'exemplaire revenu est cherché avant la remise en vente."""
    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": [owned("back", "mw", "Mark Wahlberg")]})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 4, "maxConcurrentAuctions": 5})
        return httpx.Response(200, json={"auction": {
            "id": "auc", "card": {"id": "mw", "wikipedia_title": "Mark Wahlberg", "rarity": "L"},
            "status": "settled_unsold", "end_at": datetime.now(UTC).isoformat(), "base_amount": 5700}})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="a", card_id="mw", title="Mark Wahlberg", rarity="L", is_shiny=False,
                              amount=5000, reference_price=7000, reason="revente ~7000 WB", night=False,
                              dry_run=False, status="won", end_at=now)
    lid = await db.schedule_listing(owned_card_id="sold-copy", card_id="mw", title="Mark Wahlberg", rarity="L",
                                    base_amount=5700, duration_minutes=720, post_at=now - timedelta(hours=12),
                                    target_end=now - timedelta(minutes=5), reason="position", bid_id=bid)
    await db.update_listing(lid, status="posted", auction_id="auc")
    await InventoryManager(ctx).settle_listings()
    row = await db._one("SELECT owned_card_id, relist_count FROM listings ORDER BY id DESC LIMIT 1")
    assert (row["owned_card_id"], row["relist_count"]) == ("back", 1)
    await db.close()


@pytest.mark.asyncio
async def test_failed_relist_resumes_at_its_reduced_price(tmp_path):
    """Remise en vente refusée pour de bon : la position repart à son prix réduit, pas au prix de départ."""
    def handler(request):
        return httpx.Response(200, json={"collection": [owned("new", "ecl", "Liste des éclipses solaires")]})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="a", card_id="ecl", title="Liste des éclipses solaires", rarity="L",
                              is_shiny=False, amount=882, reference_price=1197, reason="revente ~1197 WB",
                              night=False, dry_run=False, status="won", end_at=now)
    for status, price, relists in (("unsold", 1040, 0), ("failed", 936, 1)):
        lid = await db.schedule_listing(owned_card_id=f"o{price}", card_id="ecl", title="Liste des éclipses solaires",
                                        rarity="L", base_amount=price, duration_minutes=360, post_at=now,
                                        target_end=now, reason="position", bid_id=bid, relist_count=relists)
        await db.update_listing(lid, status=status)
    await InventoryManager(ctx).manage_positions()
    row = await db._one("SELECT owned_card_id, base_amount, relist_count FROM listings ORDER BY id DESC LIMIT 1")
    assert (row["owned_card_id"], row["base_amount"], row["relist_count"]) == ("new", 936, 1)
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


@pytest.mark.asyncio
async def test_tagged_purchase_becomes_a_position_at_the_price_paid(tmp_path):
    """26/10 : le 11 septembre (L) acheté 7400 à la main ; étiqueté « revente », le bot la revend comme ses positions,
    au prix final relu sur le site. Hors des chiffres du sniper ; repris une seule fois."""
    from tests.test_integration import ME

    arrived = datetime.now(UTC) - timedelta(hours=20)
    copy = {**owned("911", "c911", "Attentats du 11 septembre 2001"), "obtained_at": arrived.isoformat(),
            "tags": [{"name": "Revente"}]}
    other = {**owned("x", "cx", "Pack"), "tags": [{"name": "revente"}]}  # carte d'un paquet : aucun achat
    # Arrivée il y a 1 min : sa vente n'est peut-être pas encore notée, elle attend (pas d'avertissement).
    fresh = {**owned("y", "cy", "Neuve"), "obtained_at": datetime.now(UTC).isoformat(), "tags": [{"name": "revente"}]}

    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            page = [copy, other, fresh] if request.url.params["page"] == "0" else []
            return httpx.Response(200, json={"collection": page})
        if path == "/api/marketplace/a911":
            return httpx.Response(200, json={"auction": {
                "id": "a911", "card": {"id": "c911", "wikipedia_title": "Attentats", "rarity": "L"},
                "status": "settled_sold", "end_at": arrived.isoformat(), "base_amount": 7400,
                "final_price": 7450, "winner_id": ME}})
        return httpx.Response(404, json={})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "c911", [8000, 8200, 8100], rarity="L")
    rows = [("a911", 7400, arrived - timedelta(minutes=1), ME.upper()),           # ton achat (vu 7400)
            ("old", 6000, arrived - timedelta(days=3), ME),                       # trop ancien
            ("rival", 8000, arrived - timedelta(minutes=5), "someone-else")]      # acheté par un autre
    await db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at, buyer_id) "
        "VALUES (?, 'c911', 'Attentats', 'L', ?, ?, ?)", [(a, p, t.isoformat(), b) for a, p, t, b in rows])
    await db.db.commit()
    inv = InventoryManager(ctx)
    assert await inv.adopt_manual(await inv._recent_copies()) == 1
    bid = (await db.positions_to_list())[0]
    assert (bid["auction_id"], bid["amount"], bid["status"], bid["reason"]) == ("a911", 7450, "won", "achat manuel")
    assert bid["reference_price"] > 7450  # estimation des ventes observées : revente au-dessus du prix payé
    assert ("SUCCESS", "Carte à revendre prise en charge") in notifier.sent
    assert [t for _, t in notifier.sent].count("Carte à revendre : ni achat ni cote") == 1  # Pack : aucune vente

    # Repris une fois : ni nouvelle position, ni nouvel avertissement.
    assert await inv.adopt_manual(await inv._recent_copies()) == 0
    assert len(await db.positions_to_list()) == 1
    assert [t for _, t in notifier.sent].count("Carte à revendre : ni achat ni cote") == 1
    # Une position comme les autres, mais hors des chiffres du sniper.
    since = datetime.now(UTC) - timedelta(days=3)
    assert (await db.positions_summary())["real_cost"] == 7450
    assert not await db.bids_since(since) and await db.committed_since(since) == 0
    stats = await db.recap_stats(since)
    assert stats["bids_real"] == stats["won"] == 0 and stats["held_real"] == 1
    # Revendue comme une position du bot : l'exemplaire étiqueté, au-dessus du prix payé.
    await inv.manage_positions()
    row = await db._one("SELECT owned_card_id, base_amount, reason FROM listings")
    assert row["owned_card_id"] == "911" and row["base_amount"] > 7450 and row["reason"] == "position (achetée 7450)"
    await db.close()


@pytest.mark.asyncio
async def test_tag_on_an_older_card_is_found_in_the_whole_collection(tmp_path):
    """10/11 : Katseye ✨ achetée le 05/11 et étiquetée « revente » cinq jours après n'était jamais reprise : elle
    n'était plus parmi les 100 dernières cartes arrivées, et la collection entière n'était lue qu'avec la défausse."""
    from tests.test_session import cookie_for

    arrived = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    old = {**owned("kat", "ckat", "Katseye"), "obtained_at": arrived, "tags": [{"name": "revente"}]}
    newer = [{**owned(f"n{i}", f"cn{i}", f"Carte {i}"), "obtained_at": arrived} for i in range(100)]
    reads = {"pages": 0, "tags": 0}

    def handler(request):
        path = request.url.path
        if request.url.host.endswith("supabase.co"):
            reads["tags"] += 1
            if path == "/rest/v1/tags":
                return httpx.Response(200, json=[{"id": "t-rev", "name": "Revente"}])
            assert path == "/rest/v1/user_card_tags" and request.url.params["tag_id"] == "eq.t-rev"
            return httpx.Response(200, json=[{"user_card_id": "kat"}])
        if path == "/api/my-collection":
            reads["pages"] += 1
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"collection": (newer + [old])[page * 50:page * 50 + 50]})
        return httpx.Response(404, json={})

    client, notifier = make_client(tmp_path, handler, dry_run=False, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    await seed_observed(ctx.db, "ckat", [1400, 1500, 1450], rarity="L")
    inv = InventoryManager(ctx)
    recent = await inv._recent_copies()
    assert len(recent) == 100 and await inv.adopt_manual(recent) == 0  # avant : jamais vue
    found = await inv._older_tagged(recent)
    assert [c.id for c in found] == ["kat"] and await inv.adopt_manual(recent + found) == 1
    assert (await ctx.db.positions_to_list())[0]["auction_id"] == "paquet-kat"
    # Reprise : plus aucune lecture de la collection, seulement la liste des cartes étiquetées.
    pages = reads["pages"]
    assert await inv._older_tagged(recent) == [] and reads["pages"] == pages
    await ctx.db.close()


@pytest.mark.asyncio
async def test_freed_slot_goes_to_the_most_profitable_sale(tmp_path):
    """26/10 : Gabby Williams (L d'un paquet, 1407 WB) attendait derrière des reventes au prix d'achat. Une place
    libérée va au plus grand gain au prix demandé."""
    posted = []

    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 3 + len(posted), "maxConcurrentAuctions": 5})
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={"auction_id": f"a{len(posted)}"})
        return httpx.Response(200, json={"auctions": [], "hasMore": False})  # aucune vente concurrente

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    sales = [("josiane", 3837, 3836, "position"), ("gabby", 1407, None, "cote 1407"),
             ("shoah", 5110, 4433, "position")]
    for oid, price, cost, reason in sales:
        bid = None
        if cost is not None:
            bid = await db.record_bid(auction_id=oid, card_id=oid, title=oid, rarity="L", is_shiny=False, amount=cost,
                                      reference_price=cost * 1.3, reason="revente", night=False, dry_run=False,
                                      status="won", end_at=now)
        await db.schedule_listing(owned_card_id=oid, card_id=oid, title=oid, rarity="L", base_amount=price,
                                  duration_minutes=360, post_at=now - timedelta(minutes=1),
                                  target_end=now + timedelta(hours=6), reason=reason, bid_id=bid)
    await InventoryManager(ctx).post_due_listings()  # 2 places libres sur 5
    # Le doublon (+1407) passe avant Shoah (+677) ; Josiane (+1) attend.
    assert [p["base_amount"] for p in posted] == [1407, 5110]
    await db.close()


@pytest.mark.asyncio
async def test_tagged_pack_card_is_sold_from_its_cote(tmp_path):
    """Une L tirée d'un paquet, étiquetée « revente » : aucun achat, position à 0 WB, départ à sa cote."""
    copy = {**owned("gw", "cgw", "Gabby Williams"), "obtained_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "tags": [{"name": "revente"}]}

    def handler(request):
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": [copy] if request.url.params["page"] == "0" else []})
        return httpx.Response(404, json={})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "cgw", [1400, 1500, 1450], rarity="L")
    inv = InventoryManager(ctx)
    assert await inv.adopt_manual(await inv._recent_copies()) == 1
    bid = (await db.positions_to_list())[0]
    assert (bid["auction_id"], bid["amount"], bid["reference_price"]) == ("paquet-gw", 0, 1450)
    await inv.manage_positions()
    row = await db._one("SELECT owned_card_id, base_amount, reason FROM listings")
    assert (row["owned_card_id"], row["base_amount"], row["reason"]) == ("gw", 1450, "position (paquet)")
    assert ("SUCCESS", "Carte à revendre prise en charge") in notifier.sent
    await db.close()


def test_position_duration_avoids_bad_end_hours():
    """26/10 : une L gagnée l'après-midi finissait en 6 h à 19-22 h (×0,91-0,96) ; en 12 h, à 01-04 h. Mais le 31/10,
    5 positions attendaient une place : 6 h n'est plus jamais allongée à 12 h."""
    from src.workers.inventory_manager import position_duration

    avoid = [11, 12, 13, 18, 19, 20, 21, 22]
    at = lambda h: datetime(2026, 10, 26, h, 30, tzinfo=PARIS)  # noqa: E731
    assert position_duration(at(14), 360, 720, prefer_long=False, avoid_hours=avoid) == 360   # 20:30, gardée
    assert position_duration(at(20), 360, 720, prefer_long=False, avoid_hours=avoid) == 360   # 02:30
    assert position_duration(at(9), 360, 720, prefer_long=True, avoid_hours=avoid) == 360     # 21:30 → 15:30
    assert position_duration(at(14), 360, 720, prefer_long=True, avoid_hours=avoid) == 720    # 02:30 en 12 h
    assert position_duration(at(7), 360, 720, prefer_long=True, avoid_hours=avoid) == 720     # 19:30 et 13:30 : 12 h
    assert position_duration(at(14), 360, 720, prefer_long=False, avoid_hours=[]) == 360


@pytest.mark.asyncio
async def test_waiting_position_duration_chosen_when_posted(tmp_path, monkeypatch):
    """26/10 : Bordeaux, gagnée à 18:05, programmée pour 12 h, attendait une place jusqu'au lendemain. La durée est
    revue à la publication : 1 h depuis le 01/11 (6 h le 31/10), et plus 12 h comme avant le 31/10."""
    from src.workers import inventory_manager
    from src.workers.inventory_manager import InventoryManager
    from tests.test_integration import make_client

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    for i in range(5):
        await db.record_bid(auction_id=f"a{i}", card_id=f"c{i}", title=f"L{i}", rarity="L", is_shiny=False,
                            amount=500, reference_price=900, reason="revente ~900 WB", night=False, dry_run=False,
                            status="won", end_at=now)
    await db.schedule_listing(owned_card_id="o1", card_id="c0", title="Bordeaux", rarity="L", base_amount=2599,
                              duration_minutes=360, post_at=now - timedelta(minutes=1),
                              target_end=now + timedelta(hours=6), reason="position (achetée 2198)", bid_id=1)
    monkeypatch.setattr(inventory_manager, "now_local", lambda: datetime(2026, 10, 26, 14, 30, tzinfo=PARIS))
    await InventoryManager(ctx).post_due_listings()
    row = await db._one("SELECT duration_minutes, status FROM listings WHERE title = 'Bordeaux'")
    assert row["status"] == "simulated" and row["duration_minutes"] == 60
    await db.close()


def test_expensive_positions_are_given_up_sooner():
    from src.config import Settings
    from src.workers.inventory_manager import relist_limits

    s = Settings(_env_file=None)
    assert relist_limits(6119, s) == (1, 2)  # une remise à -10 %, puis gardée au 2e invendu
    assert relist_limits(400, s) == (2, 3)
    assert relist_limits(6119, Settings(_env_file=None, position_give_up_after=0)) == (1, 0)
    assert relist_limits(6119, Settings(_env_file=None, position_expensive_from=0)) == (2, 3)


@pytest.mark.asyncio
async def test_expensive_position_given_up_after_two_unsold(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500), position_expensive_from=100,
                                   position_list_duration_minutes=360)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    bid = await simulated_position(db, 400)  # achetée 100 : « chère » avec POSITION_EXPENSIVE_FROM=100
    await seed_observed(db, "c1", [300, 310, 290])
    await inv.settle_listings()  # 1er invendu : une remise à -10 %
    row = await db._one("SELECT base_amount, relist_count FROM listings ORDER BY id DESC LIMIT 1")
    assert (row["base_amount"], row["relist_count"]) == (360, 1)
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 2",
                   ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()  # 2e invendu : le bot renonce, la carte est gardée
    assert not await db._all("SELECT id FROM listings WHERE status = 'scheduled'")
    assert (await db.get_bid(bid))["closed"] == 1
    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shiny", [True, False])
async def test_shiny_sale_notes_its_page(tmp_path, shiny):
    """05/11 : le message d'un shiny mis en vente et les logs donnent la page de la vente ; les autres cartes, non."""
    def handler(request):
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        return httpx.Response(201, json={"auction_id": "vente-1"})

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    sent = []
    notifier.send = lambda level, title, description="", **fields: sent.append((title, fields))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="achat-1", card_id="c", title="Six-seven", rarity="L", is_shiny=shiny,
                              amount=500, reference_price=900, reason="revente", night=False, dry_run=False,
                              status="won", ceiling=500, end_at=now)
    await db.schedule_listing(owned_card_id="o1", card_id="c", title="Six-seven", rarity="L", base_amount=700,
                              duration_minutes=60, post_at=now - timedelta(seconds=1), target_end=now,
                              reason="position", bid_id=bid)
    await InventoryManager(ctx).post_due_listings()
    fields = next(f for title, f in sent if title == "Carte mise en vente")
    page = "https://www.wiki-masters.com/marketplace/vente-1"
    assert fields.get("page") == (page if shiny else None)
    assert (f"[page de la vente]({page})" in fields["thread"].line) is shiny
    await db.close()


@pytest.mark.asyncio
async def test_given_up_card_is_kept_and_tagged_never_sold_for_one_wb(tmp_path):
    """Le bot ne brade jamais : après POSITION_GIVE_UP_AFTER étapes, la carte reste dans la collection, la position
    est close et l'exemplaire revenu (nouvel identifiant) reçoit l'étiquette POSITION_UNSOLD_TAG."""
    from tests.test_session import cookie_for

    tagged, tags = [], [{"id": 3, "name": "Doublon"}]

    def handler(request):
        if request.url.host.endswith("supabase.co"):
            if request.method == "GET":
                return httpx.Response(200, json=tags)
            tagged.append(json.loads(request.content))
            return httpx.Response(201)
        if request.url.path == "/api/marketplace/a1":
            return httpx.Response(200, json={"auction": {
                "id": "a1", "status": "settled_unsold", "end_at": datetime.now(UTC).isoformat(), "base_amount": 400,
                "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "L"}}})
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": [{
                "id": "revenu", "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "L"},
                "obtained_at": datetime.now(UTC).isoformat(), "tags": []}]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="achat", card_id="c1", title="Mème", rarity="L", is_shiny=False, amount=300,
                              reference_price=500, reason="revente ~500 WB", night=False, dry_run=False,
                              status="won", end_at=now)
    await past_unsold(db, bid, 2)
    lid = await db.schedule_listing(owned_card_id="parti", card_id="c1", title="Mème", rarity="L", base_amount=400,
                                    duration_minutes=360, post_at=now, target_end=now - timedelta(minutes=5),
                                    reason="position", bid_id=bid)
    await db.update_listing(lid, status="posted", auction_id="a1")
    inv = InventoryManager(ctx)
    await inv.setup()
    await inv.settle_listings()  # 3e étape sans acheteur
    assert (await db.get_bid(bid))["closed"] == 1
    assert not await db._all("SELECT id FROM listings WHERE status = 'scheduled'")
    assert ("WARNING", "Position invendue") in notifier.sent

    assert await inv.tag_unsold() == 0 and not tagged  # étiquette pas encore créée sur le site : nouvel essai
    tags.append({"id": 9, "name": "📦 Invendue"})
    assert await inv.tag_unsold() == 1 and tagged == [{"user_card_id": "revenu", "tag_id": 9}]
    assert await inv.tag_unsold() == 0 and len(tagged) == 1  # une seule fois
    await db.close()


@pytest.mark.asyncio
async def test_old_one_wb_sales_are_cancelled_at_startup(tmp_path):
    """Revente à 1 WB programmée par une ancienne version : annulée, jamais publiée ; la position reste à revendre."""
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    bid = await simulated_position(db, 400)
    now = datetime.now(UTC)
    lid = await db.schedule_listing(owned_card_id="sim-x", card_id="c1", title="Mème", rarity="C", base_amount=1,
                                    duration_minutes=360, post_at=now, target_end=now + timedelta(hours=6),
                                    reason="liquidation (achetée 100)", bid_id=bid)
    await InventoryManager(ctx).setup()
    assert (await db.get_listing(lid))["status"] == "cancelled"
    assert (await db.get_bid(bid))["closed"] == 0
    await db.close()
