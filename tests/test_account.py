"""Valeur du compte (/valeur) : solde, cartes possédées, cartes en vente, mises en tête."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import account
from src.core.models import OwnedCard
from tests.test_integration import ME, make_client, make_context, seed_observed


def owned(oid: str, card: str, rarity: str, shiny: bool = False) -> OwnedCard:
    return OwnedCard.model_validate({"id": oid, "is_shiny": shiny,
                                     "card": {"id": card, "wikipedia_title": card.title(), "rarity": rarity}})


@pytest.mark.asyncio
async def test_account_value_adds_balance_cards_sales_and_leading_bids(tmp_path):
    end = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 1000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [
                {"id": "a1", "status": "active", "base_amount": 100, "current_bid": 300, "current_bidder_id": ME, "end_at": end,
                 "card": {"id": "x", "wikipedia_title": "X", "rarity": "L"}},
                {"id": "a2", "status": "active", "base_amount": 100, "current_bid": 999, "current_bidder_id": "autre", "end_at": end,
                 "card": {"id": "y", "wikipedia_title": "Y", "rarity": "L"}}]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "ada", [500, 520, 480], rarity="L")
    await db.freeze_values("bob", {"C": 2})
    now = datetime.now(UTC)
    listing = await db.schedule_listing(owned_card_id="o-sold", card_id="ada", title="Ada", rarity="L",
                                        base_amount=700, duration_minutes=360, post_at=now,
                                        target_end=now + timedelta(hours=6), reason="revente")
    await db.update_listing(listing, status="posted")
    rows = [owned("o1", "ada", "L"), owned("o2", "ada", "L", shiny=True), owned("o3", "bob", "C"),
            owned("o4", "zed", "C"), owned("o-sold", "ada", "L")]
    snapshot = await account.store_collection(ctx, rows)
    # Ada L 500 (×2 : le shiny jamais vu vendu vaut la carte ordinaire), Bob 2 (cote figée), Zed inconnue ;
    # l'exemplaire en vente est compté à part, à son prix de départ.
    assert (snapshot["copies"], snapshot["value"], snapshot["unknown"]) == (4, 1002, 1)
    assert snapshot["by_rarity"] == {"L": [2, 1000], "C": [2, 2]}
    assert sorted(snapshot["top"][:2]) == [[500, "Ada", "L", False], [500, "Ada", "L", True]]

    await db.kv_set(account.HISTORY_KEY, {"2000-01-01": 2000})
    value = await account.account_value(ctx, snapshot)
    assert (value["balance"], value["cards"], value["selling"], value["bids"]) == (1000, 1002, 700, 300)
    assert value["total"] == 3002 and value["previous"] == ("2000-01-01", 2000)
    assert value["leading"] == 1  # l'enchère où un autre est en tête ne bloque rien

    from src.discord_cards import value_embed
    embed = value_embed(value, snapshot)
    assert "3 002 WB" in embed.description and "+1 002 WB depuis le 01/01" in embed.description
    assert await account.collection_snapshot(ctx) == snapshot  # récente : pas relue
    await db.close()


@pytest.mark.asyncio
async def test_value_worker_reads_the_collection_only_when_nobody_did(tmp_path):
    from src.workers.account_value import AccountValue

    pages = []

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 50})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": []})
        if request.url.path == "/api/my-collection":
            pages.append(request.url.params["page"])
            return httpx.Response(200, json={"collection": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    await account.store_collection(ctx, [])  # lecture récente d'un autre worker
    worker = AccountValue(ctx)
    delay = await worker.step()
    assert not pages and delay > 5 * 3600  # rien relu, prochain passage dans ~6 h
    snapshot = await ctx.db.kv_get(account.KV_KEY)
    snapshot["at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    await ctx.db.kv_set(account.KV_KEY, snapshot)
    await worker.step()
    assert pages == ["0"]
    assert list((await ctx.db.kv_get(account.HISTORY_KEY)).values()) == [50]  # valeur du jour notée
    await ctx.db.close()


@pytest.mark.asyncio
async def test_evening_report_shows_the_account_value_only_from_a_recent_count(tmp_path):
    from src.main import daily_report_parts

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 1000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    report = await daily_report_parts(ctx)
    assert report.description.startswith("Solde") and not report.columns  # jamais comptée : pas de valeur inventée
    await seed_observed(db, "ada", [500, 520, 480], rarity="L")
    await account.store_collection(ctx, [owned("o1", "ada", "L")])
    await db.kv_set(account.HISTORY_KEY, {"2000-01-01": 1000})
    report = await daily_report_parts(ctx)
    assert report.description == "Compte : **1 500 WB** (+500 WB depuis le 01/01)"
    assert report.columns == [("Solde", "1 000 WB"), ("Cartes", "500 WB"), ("En vente", "0 WB")]
    snapshot = await db.kv_get(account.KV_KEY)
    snapshot["at"] = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    await db.kv_set(account.KV_KEY, snapshot)
    report = await daily_report_parts(ctx)
    assert not report.columns  # comptée il y a plus de 24 h
    await db.close()
