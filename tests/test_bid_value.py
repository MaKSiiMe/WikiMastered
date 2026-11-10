"""Valeur d'une mise (core/winmodel.py) : le quota de la vérification anti-bot va aux mises qui rapportent le plus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.database import Database
from src.core.winmodel import CONTROL_MARK, fit, thresholds_text
from src.sniper_report import breakdown
from src.workers.auction_sniper import LOW_VALUE, AuctionSniper, in_control_group
from tests.test_integration import make_client, make_context, seed_observed

AUCTION = "33333333-3333-3333-3333-333333333333"


def row(amount, status, *, reference=1000, shiny=False, sale=None, reason="revente ~1000 WB, bénéfice ~1", aid=None):
    return {"auction_id": aid or f"a-{amount}-{status}", "amount": amount, "reference_price": reference,
            "is_shiny": int(shiny), "status": status, "sale_price": sale, "reason": reason}


def history() -> list[dict]:
    """300 mises : à 25 % de l'estimation, jamais gagnées ; à 65 %, une sur deux, revendues 90 % de l'estimation."""
    low = [row(250, "lost", aid=f"l{i}") for i in range(200)]
    high = [row(650, "won" if i % 2 else "outbid", sale=900 if i % 2 else None, aid=f"h{i}") for i in range(100)]
    return low + high


def test_no_model_without_enough_settled_bids():
    assert fit(history()[:150]) is None
    assert "aucun modèle" in thresholds_text(history()[:150], 7)


def test_a_low_bid_wins_rarely_so_it_is_worth_little():
    model = fit(history())
    assert model.samples == 300 and model.resale[False] == 0.9
    assert model.win_chance(250, 1000, False) < 0.03 < 0.4 < model.win_chance(650, 1000, False)
    # 750 WB de bénéfice attendu, presque jamais gagnés, contre 350 gagnés près d'une fois sur deux.
    assert model.value(250, 1000, False) < 15 < 100 < model.value(650, 1000, False)
    # Rapport jamais vu, et shiny sans aucune mise : le taux moyen.
    assert model.win_chance(900, 1000, False) == model.base[False]
    assert model.win_chance(650, 1000, True) == model.base[True] == pytest.approx(1 / 6)


def test_refused_and_control_bids_do_not_train_the_model():
    refused = [row(650, "rejected", aid=f"r{i}") for i in range(500)]
    control = [row(650, "lost", reason=f"revente ~1000 WB, bénéfice ~1, {CONTROL_MARK}", aid=f"c{i}")
               for i in range(500)]
    assert fit(history() + refused + control).rates == fit(history()).rates


def test_thresholds_show_what_each_one_would_keep():
    refused = [row(650, "rejected", aid=f"r{i}") for i in range(50)] + [row(650, "rejected", aid="h1")]
    text = thresholds_text(history() + refused, 10)
    assert "350 occasions (35/j), dont 300 mises acceptées par le site (30/j)" in text
    lines = {int(line.split()[0]): line.split() for line in text.splitlines() if line.strip()[:1].isdigit()}
    assert lines[0][1] == "35" and lines[100][1] == "15"  # seuil 100 : seules les mises à 65 % restent


def market(base_amount: int):
    end = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [{
                "id": AUCTION, "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active", "end_at": end,
                "base_amount": base_amount, "current_bid": None, "current_bidder_id": None}], "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")
    return handler


async def seed_history(db: Database) -> None:
    for r in history():
        bid_id = await db.record_bid(
            auction_id=r["auction_id"], card_id="x", title="x", rarity="C", is_shiny=False, amount=r["amount"],
            reference_price=r["reference_price"], reason=r["reason"], night=False, dry_run=False,
            status=r["status"], end_at=datetime.now(UTC))
        if r["sale_price"]:
            await db.update_bid(bid_id, closed=1, sale_price=r["sale_price"])


@pytest.mark.asyncio
@pytest.mark.parametrize("base_amount, status", [(100, None), (260, "simulated")])
async def test_sniper_skips_a_bid_worth_less_than_the_threshold(tmp_path, base_amount, status):
    """Estimation 400. À 100 WB (25 %), 300 de bénéfice attendu mais presque aucune chance : sautée. À 260 WB
    (65 %), 140 de bénéfice gagnés près d'une fois sur deux : misée."""
    client, notifier = make_client(tmp_path, market(base_amount), sniper_min_bid_value=20)
    ctx = await make_context(tmp_path, client, notifier)
    await seed_observed(ctx.db, "c1", [400, 410, 390])
    await seed_history(ctx.db)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    last = await ctx.db.last_bid(AUCTION)
    assert (last["status"] if last else None) == status
    assert (sniper._outcomes[AUCTION] == LOW_VALUE) == (status is None)
    await ctx.db.close()


@pytest.mark.asyncio
async def test_threshold_waits_for_enough_history(tmp_path):
    client, notifier = make_client(tmp_path, market(100), sniper_min_bid_value=20)
    ctx = await make_context(tmp_path, client, notifier)
    await seed_observed(ctx.db, "c1", [400, 410, 390])
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    assert (await ctx.db.last_bid(AUCTION))["status"] == "simulated"
    await ctx.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("share, control", [(0.0, False), (1.0, True)])
async def test_control_group_bids_the_minimum_despite_the_push(tmp_path, share, control):
    client, notifier = make_client(tmp_path, market(100), sniper_bid_push=0.5, sniper_bid_push_control_share=share)
    ctx = await make_context(tmp_path, client, notifier)
    await seed_observed(ctx.db, "c1", [400, 410, 390])
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    last = await ctx.db.last_bid(AUCTION)
    assert (last["amount"] == 100) == control and last["reason"].endswith(CONTROL_MARK) == control
    assert last["reason"].startswith("revente ~400 WB, bénéfice ~")
    await ctx.db.update_bid(last["id"], status="sim_won")
    text = breakdown(await ctx.db.bids_since(datetime.now(UTC) - timedelta(hours=1)))
    group = "au minimum (témoin)" if control else "poussée"
    assert f"Mise poussée au-dessus du minimum :\n  {group} : réelles 0 · simulées 1/1" in text
    await ctx.db.close()


def test_control_group_is_stable_and_sized():
    ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(2000)]
    picked = [i for i in ids if in_control_group(i, 0.3)]
    assert 500 < len(picked) < 700 and picked == [i for i in ids if in_control_group(i, 0.3)]
    assert not any(in_control_group(i, 0) for i in ids) and all(in_control_group(i, 1) for i in ids)
