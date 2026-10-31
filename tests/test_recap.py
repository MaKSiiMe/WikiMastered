"""Récap périodique : activité par worker, santé, anomalies."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.config import Settings
from src.core.database import Database
from src.recap import anomalies, build_recap, next_recap, traffic_line

PARIS = ZoneInfo("Europe/Paris")


def test_next_recap_on_round_hours():
    assert next_recap(datetime(2026, 10, 14, 20, 5, tzinfo=PARIS), 4).hour == 0
    assert next_recap(datetime(2026, 10, 14, 7, 59, tzinfo=PARIS), 4) == datetime(2026, 10, 14, 8, 0, tzinfo=PARIS)
    assert next_recap(datetime(2026, 10, 14, 8, 0, tzinfo=PARIS), 4) == datetime(2026, 10, 14, 12, 0, tzinfo=PARIS)


def worker(name, *, last_ok=None, next_due=None, errors=0, last_error="", lines=(), blocked=None):
    return SimpleNamespace(name=name, last_ok=last_ok, next_due=next_due, recent_errors=errors,
                           last_error=last_error, recap=lambda: list(lines), blocked=blocked)


@pytest.mark.asyncio
async def test_recap_reports_activity_health_and_problems(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)
    since = now - timedelta(hours=4)
    for i in range(17):  # 17 positions simulées détenues, plafond 15
        await db.record_bid(auction_id=f"a{i}", card_id="c", title="t", rarity="C", is_shiny=False, amount=16,
                            reference_price=300, reason="revente ~300 WB", night=False, dry_run=True,
                            status="sim_won", end_at=now)
    ctx = SimpleNamespace(settings=Settings(_env_file=None, portfolio_max_positions=15), db=db, client=SimpleNamespace(session_ok=True))
    workers = [
        worker("sniper", last_ok=now, next_due=now + timedelta(seconds=3),
               lines=["40 enchère(s) examinée(s) : pas assez vue vendue 30 · misée 10"]),
        worker("succes", last_ok=now - timedelta(hours=2), next_due=now - timedelta(hours=1)),  # bloqué
        worker("inventory", last_ok=now, next_due=now + timedelta(minutes=10), errors=2, last_error="HTTP 500"),
        worker("collection", last_ok=now, next_due=now + timedelta(hours=6)),
    ]
    await db.kv_set("collection", {"owned": ["1 janvier"] * 37, "missing": [], "total": 366})
    problem, description, sections = await build_recap(ctx, workers, since, now - timedelta(days=1))
    blocks = dict(sections)
    assert problem and sections[0][0] == "À vérifier"
    assert "sniper : 17 positions ouvertes, plafond 15" in blocks["À vérifier"]
    assert "succes : aucun passage depuis" in blocks["À vérifier"]
    assert "inventory : 2 erreur(s), dernière : HTTP 500" in blocks["À vérifier"]
    assert "pas assez vue vendue 30" in blocks["🎯 Sniper"] and "✅" in blocks["🎯 Sniper"]
    assert "❌ arrêté" in blocks["🏆 Succès"]
    assert "⚠️ 2 erreur(s)" in blocks["💰 Ventes"]
    assert blocks["📅 Collection"].startswith("Collection : 37/366 (10,1 %)\nAucune nouvelle carte en 7 jours\n"
                                             "Sniper : 0 gagnée(s) · mises 0")
    assert "💎 Valeur" not in blocks  # worker absent : pas de bloc
    assert description.startswith("Depuis ")
    assert workers[2].recent_errors == 0  # erreurs remises à zéro après le récap
    await db.close()


def test_traffic_line_per_worker():
    """Bloc Réseau : lectures par worker (les plus nombreuses d'abord), débit moyen, écritures et limitations."""
    traffic = Counter({"cotes": 600, "sniper": 7900, "écritures": 45})
    assert traffic_line(traffic, 1, 4 * 3600) == ("Lectures : 8500 (0.59/s) · sniper 7900 · cotes 600\n"
                                                  "Écritures : 45 · limitées par le site : 1")
    assert traffic_line(Counter(), 0, 0) == "Lectures : 0 (0.00/s)\nÉcritures : 0 · limitées par le site : 0"


@pytest.mark.asyncio
async def test_recap_flags_worker_waiting_for_you(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)
    ctx = SimpleNamespace(settings=Settings(_env_file=None), db=db, client=SimpleNamespace(session_ok=True))
    sniper = worker("sniper", last_ok=now, next_due=now + timedelta(minutes=7), blocked="vérification humaine en attente")
    problem, _, sections = await build_recap(ctx, [sniper], now - timedelta(hours=4), now - timedelta(days=1))
    blocks = dict(sections)
    assert problem and "sniper : vérification humaine en attente" in blocks["À vérifier"]
    assert "⏸️ vérification humaine en attente" in blocks["🎯 Sniper"] and "✅" not in blocks["🎯 Sniper"]
    await db.close()


def test_inventory_lines_say_what_unsold_means():
    """Récap du 16/10 : « 8 invendue(s) » contre « 0 gardées invendues » au bilan, sans contradiction réelle."""
    from src.recap import activity

    stats = {"posted": 0, "sold": 0, "sold_wb": 0, "unsold": 1, "sim_sold": 5, "sim_unsold": 8}
    assert activity("inventory", stats) == [
        "Mises en vente : 0 · vendues 0 (0 WB) · sans acheteur 1",
        "Reventes simulées : 5 vendue(s), 8 vente(s) sans acheteur (remise en vente ou carte gardée)"]


@pytest.mark.asyncio
async def test_recap_live_counts_only_real_positions_against_the_cap(tmp_path):
    """Le 16/10 à 16 h : « 11 positions ouvertes, plafond 5 » alors que 6 étaient simulées (d'avant le réel)."""
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    async def hold(i, dry_run):
        await db.record_bid(auction_id=f"a{i}", card_id="c", title="t", rarity="L", is_shiny=False, amount=100,
                            reference_price=300, reason="revente ~300 WB", night=False, dry_run=dry_run,
                            status="sim_won" if dry_run else "won", end_at=now)

    for i in range(6):
        await hold(i, True)
    for i in range(6, 9):
        await hold(i, False)
    live = SimpleNamespace(settings=Settings(_env_file=None, dry_run=False, portfolio_max_positions=5), db=db,
                           client=SimpleNamespace(session_ok=True))
    stats = await db.recap_stats(now - timedelta(hours=4))
    pending = await db.pending_bids()
    assert stats["held"] == 9 and stats["held_real"] == 3
    assert not any("positions" in p for p in anomalies(live, stats, pending))
    for i in range(9, 12):
        await hold(i, False)
    stats = await db.recap_stats(now - timedelta(hours=4))
    assert "sniper : 6 positions réelles ouvertes, plafond 5" in anomalies(live, stats, pending)
    await db.close()
