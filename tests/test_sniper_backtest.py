"""Rejeu du sniper : estimation d'époque, mise, victoire et bénéfice de chaque réglage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import Settings
from src.core.database import Database
from src.sniper_backtest import Case, Combo, cases_from, render, replay

SETTINGS = Settings(_env_file=None, sniper_bid_push=0.5, portfolio_min_profit=30, sniper_max_bid=0,
                    sniper_min_contested_sales=3, sniper_max_price_spread=2.0, price_min_sales=2,
                    price_recent_sales=5, sniper_deal_ratio_max=0.70, night_deal_ratio_max=0.80,
                    portfolio_min_profit_ratio=0.10, sniper_min_bid_share=0.40)


@pytest.mark.asyncio
async def test_estimate_uses_only_earlier_sales_capped_by_the_site(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    async def sale(aid, card, price, base, hours_ago, seller=None, buyer=None):
        await db.db.execute(
            "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, base_amount, ended_at, seller_id, "
            "buyer_id) VALUES (?, ?, 't', 'L', ?, ?, ?, ?, ?)",
            (aid, card, price, base, (now - timedelta(hours=hours_ago)).isoformat(), seller, buyer))

    for i, price in enumerate((900, 1000, 1100)):  # disputées (prix final > départ), avant la période
        await sale(f"old{i}", "c1", price, 500, 300 - i)
    await sale("new", "c1", 1300, 600, 2)          # rejouée avec la médiane des trois précédentes
    await sale("later", "c1", 5000, 4000, 1)       # rejouée, mais n'a pas compté dans l'estimation de « new »
    await sale("wash", "c1", 50, 40, 1.5, "x", "x")  # vendeur = acheteur : écartée
    await db.db.execute("INSERT INTO price_history (card_id, rarity, average, sales_count, observed_at) "
                        "VALUES ('c1', 'L', 950, 9, ?)", ((now - timedelta(hours=10)).isoformat(),))
    await db.db.commit()

    cases = cases_from(await db.backtest_sales("L"), await db.site_average_history("L"),
                       now - timedelta(days=1), SETTINGS)
    assert [(c.estimate, c.final, c.start) for c in cases] == [(950, 1300, 600), (950, 5000, 4000)]
    await db.close()


def test_win_needs_the_ceiling_above_the_next_bid_and_pays_the_pushed_bid():
    # Estimée 2000, partie à 1358 (Barack Obama, 19/10) : la mise suivante (1494) passe à 0.75, pas à 0.70.
    case = Case(2000, 1358, 800, night=False)
    lost = replay([case], Combo(0.70, 0.80, 0.10, 0.40), SETTINGS, budget=3600)
    won = replay([case], Combo(0.75, 0.85, 0.10, 0.40), SETTINGS, budget=3600)
    assert (lost.bids, lost.wins, lost.gain) == (1, 0, 0)
    assert (won.bids, won.wins) == (1, 1) and won.gain == 2000 - (1494 + 0.5 * (1500 - 1494))


def test_profit_floor_and_bid_share_decide_whether_a_cheap_card_is_bid():
    case = Case(640, 405, 200, night=False)  # Stendhal : 360 WB exigés laissent un plafond de 280
    assert replay([case], Combo(0.70, 0.80, 0.10, 0.0), SETTINGS, budget=3600).wins == 0
    assert replay([case], Combo(0.70, 0.80, 0.05, 0.0), SETTINGS, budget=3600).wins == 1
    # Départ à 200, plafond 280 : la mise poussée (240) reste sous 40 % de l'estimation (256), pas de mise.
    assert replay([case], Combo(0.70, 0.80, 0.10, 0.40), SETTINGS, budget=3600).bids == 0


def test_table_scales_by_real_wins_and_always_shows_the_current_setting():
    cases = [Case(2000, 1358, 800, night=False), Case(1000, 600, 300, night=True)]
    text = render(cases, {"bids": 4, "wins": 1, "real_bids": 3}, SETTINGS, budget=3600, days=7, top=1)
    assert "réel 4 mises, 1 gagnées → victoires du modèle ×1.00 · quota : 3 mises réelles" in text
    assert text.count("← actuel") == 1 and " 0.70  0.80    0.10  0.40" in text
