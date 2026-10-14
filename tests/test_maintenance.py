"""Entretien de la base : seules les données périmées disparaissent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.database import Database


@pytest.mark.asyncio
async def test_purge_keeps_accounting_and_recent_market(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    old, recent = (now - timedelta(days=200)).isoformat(), (now - timedelta(days=2)).isoformat()
    await db.db.executemany("INSERT INTO observed_sales (auction_id, card_id, price, ended_at) VALUES (?, 'c', 10, ?)",
                            [("old", old), ("new", recent)])
    await db.db.executemany("INSERT INTO price_history (card_id, rarity, average, observed_at) VALUES ('c', 'C', 1, ?)",
                            [(old,), (recent,)])
    await db.db.executemany("INSERT INTO pageviews (article, day, views) VALUES ('A', ?, 1)",
                            [(f"{now - timedelta(days=90):%Y%m%d}",), (f"{now - timedelta(days=3):%Y%m%d}",)])
    await db.record_bid(auction_id="a", card_id="c", title="t", rarity="C", is_shiny=False, amount=5,
                        reference_price=None, reason="r", night=False, dry_run=True, status="sim_lost",
                        end_at=now - timedelta(days=400))
    await db.db.commit()
    removed = await db.purge(now)
    assert removed["observed_sales"] == 1 and removed["price_history"] == 1 and removed["pageviews"] == 1
    assert await db.observed_prices("c", 5) == ([10], 1)
    assert len(await db._all("SELECT * FROM bids")) == 1  # comptabilité gardée
    await db.close()
