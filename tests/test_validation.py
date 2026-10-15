"""Validation avant le passage en réel : simulation réaliste, bilan, premières vraies réponses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.main import validation_text
from src.workers.base import Context
from src.workers.inventory_manager import InventoryManager
from tests.test_integration import make_client, seed_observed


async def simulated_position(db: Database, base_amount: int) -> int:
    now = datetime.now(timezone.utc)
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
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [300, 310, 290])  # la tendance est retombée sous le prix de départ
    await inv.settle_listings()
    rows = await db._all("SELECT status, base_amount, relist_count FROM listings ORDER BY id")
    assert [r["status"] for r in rows] == ["sim_unsold", "scheduled"]
    assert rows[1]["base_amount"] == 360 and rows[1]["relist_count"] == 1  # remise à -10 %
    assert (await db.get_bid(bid))["closed"] == 0
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 2",
                   ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()
    await db._exec("UPDATE listings SET status = 'simulated', target_end = ? WHERE id = 3",
                   ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
    await inv.settle_listings()  # 2 remises déjà faites : carte gardée
    assert (await db.get_bid(bid))["closed"] == 1 and (await db.get_bid(bid))["sale_price"] is None
    stats = await db.validation_stats(datetime.now(timezone.utc) - timedelta(days=1), 2)
    assert stats["sim_kept"] == 1 and stats["sim_kept_cost"] == 100 and stats["sim_sold"] == 0
    assert ("WARNING", "Position invendue") in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_simulated_resale_sells_when_the_market_pays(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(500))
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    bid = await simulated_position(db, 400)
    await seed_observed(db, "c1", [420, 400, 450])
    await inv.settle_listings()
    # Une enchère : les joueurs la font monter au prix du marché (médiane observée), pas seulement au départ.
    assert (await db.get_bid(bid))["sale_price"] == 420
    assert (await db.validation_stats(datetime.now(timezone.utc), 2))["sim_profit"] == 320
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
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
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

    from src.workers.inventory_manager import report

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
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    inv = InventoryManager(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await inv.setup()
    await report(inv.ctx, inv, lookups=10)
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
