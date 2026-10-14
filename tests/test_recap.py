"""Récap périodique : activité par worker, santé, anomalies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.config import Settings
from src.core.database import Database
from src.recap import build_recap, next_recap

PARIS = ZoneInfo("Europe/Paris")


def test_next_recap_on_round_hours():
    assert next_recap(datetime(2026, 10, 14, 20, 5, tzinfo=PARIS), 4).hour == 0
    assert next_recap(datetime(2026, 10, 14, 7, 59, tzinfo=PARIS), 4) == datetime(2026, 10, 14, 8, 0, tzinfo=PARIS)
    assert next_recap(datetime(2026, 10, 14, 8, 0, tzinfo=PARIS), 4) == datetime(2026, 10, 14, 12, 0, tzinfo=PARIS)


def worker(name, *, last_ok=None, next_due=None, errors=0, last_error="", lines=()):
    return SimpleNamespace(name=name, last_ok=last_ok, next_due=next_due, recent_errors=errors,
                           last_error=last_error, recap=lambda: list(lines))


@pytest.mark.asyncio
async def test_recap_reports_activity_health_and_problems(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=4)
    await db.record_pack_open(5)
    for i in range(17):  # 17 positions simulées détenues, plafond 15
        await db.record_bid(auction_id=f"a{i}", card_id="c", title="t", rarity="C", is_shiny=False, amount=16,
                            reference_price=300, reason="revente ~300 WB", night=False, dry_run=True,
                            status="sim_won", end_at=now)
    ctx = SimpleNamespace(settings=Settings(_env_file=None), db=db, client=SimpleNamespace(session_ok=True))
    workers = [
        worker("sniper", last_ok=now, next_due=now + timedelta(seconds=3),
               lines=["40 enchère(s) examinée(s) : pas assez vue vendue 30 · misée 10"]),
        worker("packs", last_ok=now - timedelta(hours=2), next_due=now - timedelta(hours=1)),  # bloqué
        worker("inventory", last_ok=now, next_due=now + timedelta(minutes=10), errors=2, last_error="HTTP 500"),
        worker("collection", last_ok=now, next_due=now + timedelta(hours=6)),
    ]
    await db.kv_set("collection", {"owned": ["1 janvier"] * 37, "missing": [], "total": 366})
    problem, description, sections = await build_recap(ctx, workers, since, now - timedelta(days=1))
    blocks = dict(sections)
    assert problem and sections[0][0] == "À vérifier"
    assert "sniper : 17 positions ouvertes, plafond 15" in blocks["À vérifier"]
    assert "packs : aucun passage depuis" in blocks["À vérifier"]
    assert "inventory : 2 erreur(s), dernière : HTTP 500" in blocks["À vérifier"]
    assert "pas assez vue vendue 30" in blocks["🎯 Sniper"] and "✅" in blocks["🎯 Sniper"]
    assert "1 booster(s)" in blocks["🎴 Paquets"] and "❌ arrêté" in blocks["🎴 Paquets"]
    assert "⚠️ 2 erreur(s)" in blocks["💰 Ventes"]
    assert blocks["📅 Collection"].startswith("37/366 possédées · 0 gagnée(s) · mises 0")
    assert "📈 Tendances" not in blocks  # worker absent : pas de bloc
    assert description.startswith("Depuis ")
    assert workers[2].recent_errors == 0  # erreurs remises à zéro après le récap
    await db.close()
