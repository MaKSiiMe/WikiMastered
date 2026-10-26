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
