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


@pytest.mark.asyncio
async def test_estimates_compare_same_rarity_and_shiny(tmp_path):
    """Un exemplaire Commun ne s'estime pas sur les ventes de la même carte en UR ou shiny."""
    from src.core.client import ApiError
    from src.core.pricing import PriceService, Quote

    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    rows = [("a1", "UR", 0, 500), ("a2", "UR", 0, 520), ("a3", "C", 0, 12), ("a4", "C", 0, 10), ("a5", "C", 1, 90)]
    await db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at) "
        "VALUES (?, 'x', 't', ?, ?, ?, ?)", [(a, r, s, p, now.isoformat()) for a, r, s, p in rows])
    await db.db.commit()

    prices = PriceService(None, db, 30)  # type: ignore[arg-type]

    async def no_market(*_args):
        raise ApiError(429, {}, "card_sales_summary")

    prices._market = no_market  # type: ignore[method-assign]
    assert await prices.quote("x", "C") == Quote(11.0, 2)
    assert await prices.quote("x", "UR") == Quote(510.0, 2)
    assert await prices.quote("x", "C", shiny=True) == Quote(90.0, 1)
    assert await prices.quote("x", "L") == Quote(None, 0)  # jamais vue en L : pas d'estimation
    assert set(await prices.observed_quote("x")) == {"C", "UR"}
    assert sorted(await db.observed_prices_since("x", now - timedelta(days=1), 5, rarity="C", shiny=False)) == [10, 12]
    await db.close()
