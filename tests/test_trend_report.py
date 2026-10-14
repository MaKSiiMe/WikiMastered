"""Mesure des signaux de tendance : achat en surenchérissant, revente au prix observé ensuite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.database import Database
from src.workers.trend_report import measure_signals, trends_text


@pytest.mark.asyncio
async def test_signals_measured_against_later_sales(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    ended = now - timedelta(days=2)

    async def signal(source, aid, card, min_bid, end):
        await db.record_signal(source=source, term=card, card_id=card, title=card, auction_id=aid,
                               min_bid=min_bid, end_at=end)

    async def sale(aid, card, price, at):
        await db.db.execute("INSERT INTO observed_sales (auction_id, card_id, price, ended_at) VALUES (?, ?, ?, ?)",
                            (aid, card, price, at.isoformat()))

    await signal("google", "s1", "Squid Game", 100, ended)   # vendue 200, puis revendue 500 et 520
    await sale("s1", "Squid Game", 200, ended)
    await sale("x1", "Squid Game", 500, ended + timedelta(hours=5))
    await sale("x2", "Squid Game", 520, ended + timedelta(hours=9))
    await signal("google", "s2", "Tommy Paul", 50, ended)    # personne n'a misé ; revendue 60 ensuite
    await sale("x3", "Tommy Paul", 60, ended + timedelta(hours=3))
    await signal("google", "s3", "Grand Raid", 80, now + timedelta(hours=1))  # pas encore finie
    await signal("wikipedia", "s4", "Berléand", 30, now - timedelta(days=9))  # aucune revente en 7 jours
    await db.db.commit()

    stats = await measure_signals(db, now - timedelta(days=10), now)
    google = stats["google"]
    assert google.signals == 3 and google.pending == 1
    assert sorted(google.profits) == [10, 289]  # 510 - 221 (surenchère sur 200, arrondi du site) ; 60 - 50
    assert google.hits(147) == 1
    assert stats["wikipedia"].no_data == 1
    text = await trends_text(db, 10, 147, now)
    assert "Bénéfice médian +150 WB, 1/2 gagnant(s)" in text and "trop tôt pour conclure" in text
    short = await trends_text(db, 10, 147, now, detailed=False)
    assert short == ("Google Trends : 2 mesuré(s), médiane +150 WB, 1 gagnant(s) → trop tôt pour conclure\n"
                     "Wikipédia : aucun signal mesuré.")
    await db.close()


@pytest.mark.asyncio
async def test_sources_compared_day_by_day(tmp_path):
    from src.workers.trend_report import day_line

    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    later = now + timedelta(hours=3)
    for source, aid, card in (("google", "g1", "Greta Thunberg"), ("google", "g2", "Greta Thunberg"),
                              ("google", "g3", "Lucie Bernardoni"), ("wikipedia", "w1", "Greta Thunberg"),
                              ("wikipedia", "w2", "Viking FK")):
        await db.record_signal(source=source, term=card, card_id=card, title=card, auction_id=aid,
                               min_bid=10, end_at=later)
    for term in ("greta thunberg", "lucie bernardoni", "décharge"):
        await db.term_is_new("google", term, 500, now)
    line = await day_line(db, now)
    assert "Google Trends 3 termes → 2 carte(s) (3 ventes)" in line
    assert "Wikipédia 2 carte(s) (2 ventes)" in line and "en commun : Greta Thunberg" in line
    text = await trends_text(db, 7, 147, now)
    assert "**Jour par jour**" in text and "en commun : 1" in text
    assert "Cartes signalées par les deux sources : Greta Thunberg" in text
    await db.close()


def test_trend_alerts_silent_by_default():
    from types import SimpleNamespace

    from src.workers.trend_common import flush

    sent = []
    notifier = SimpleNamespace(info=lambda *a, **k: sent.append(a[0]))
    flush(SimpleNamespace(settings=SimpleNamespace(trends_notify=False), notifier=notifier), "Tendances Google", ["x"])
    assert sent == []
    flush(SimpleNamespace(settings=SimpleNamespace(trends_notify=True), notifier=notifier), "Tendances Google", ["x"])
    assert sent == ["Tendances Google : 1 carte(s) en vente"]
