"""/amis : les cartes manquantes chez les amis, en lecture seule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import friends
from tests.test_integration import make_client, make_context

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def catalogue_page(cards, owners):
    return {"cards": [{"id": cid, "wikipedia_title": title} for cid, title in cards], "friendOwners": owners}


def entry(eid, cid, title, rarity="R", **extra):
    return {"id": eid, "card_id": cid, "card": {"id": cid, "wikipedia_title": title, "rarity": rarity}, **extra}


async def context(tmp_path, pages=None, collection=None, calls=None, missing=("14 octobre", "2 mars", "7 juin")):
    def handler(request):
        assert request.method == "GET"  # lecture seule, quoi qu'il arrive
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/api/cards":
            assert request.url.params["wishlist"] == "1"
            return httpx.Response(200, json=pages[int(request.url.params["page"])])
        assert request.url.params["wishlisted_by_me"] == "1"
        return httpx.Response(200, json=collection)

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    await ctx.db.kv_set("collection", {"owned": [], "missing": list(missing), "total": 366})
    return ctx


@pytest.mark.asyncio
async def test_friends_are_ranked_without_the_players_own_wishes(tmp_path):
    full = [(f"x{i}", f"Autre {i}") for i in range(48)] + [("c14", "14 octobre"), ("c2", "2 mars")]
    pages = [catalogue_page(full, {"c14": [{"username": "bob"}, {"username": "alice"}], "c2": [{"username": "alice"}],
                                   "x1": [{"username": "carl"}]}),
             catalogue_page([("c7", "7 juin")], {"c7": []})]
    calls: list = []
    ctx = await context(tmp_path, pages, calls=calls)
    state = await friends.overview(ctx, now=NOW)
    assert state["friends"] == {"alice": ["14 octobre", "2 mars"], "bob": ["14 octobre"]}  # pas carl : carte du joueur
    assert len(calls) == 2
    text = friends.overview_text(state)
    assert "**2 cartes** chez **2 amis**, sur 3 manquantes" in text and text.index("alice") < text.index("bob")

    # Moins d'une heure après : aucune requête, sauf si on le demande.
    await friends.overview(ctx, now=NOW + timedelta(minutes=30))
    assert len(calls) == 2
    await friends.overview(ctx, now=NOW + timedelta(minutes=30), refresh=True)
    assert len(calls) == 4
    await friends.overview(ctx, now=NOW + timedelta(hours=2))
    assert len(calls) == 6
    await ctx.db.close()


@pytest.mark.asyncio
async def test_unexpected_answer_says_so(tmp_path):
    from src.core.client import ApiError

    ctx = await context(tmp_path, [{"cards": []}])
    with pytest.raises(ApiError):
        await friends.overview(ctx, now=NOW)
    assert friends.overview_text({"at": "", "cards": 3, "friends": {}}).startswith("Aucun ami")
    await ctx.db.close()


@pytest.mark.asyncio
async def test_friend_detail_puts_spare_copies_first(tmp_path):
    collection = {"collection": [entry("e1", "c14", "14 octobre", count=2),
                                 entry("e2", "c2", "2 mars"),
                                 entry("e3", "c7", "7 juin", count=3, starred=True),
                                 entry("e4", "cx", "Hors collection", count=5)],
                  "pendingTradeCardIds": []}
    ctx = await context(tmp_path, collection=collection)
    cards = await friends.friend_cards(ctx, "alice")
    assert [c.priced.title for c in cards][0] == "14 octobre"
    assert {c.priced.title for c in cards} == {"14 octobre", "2 mars", "7 juin"}
    assert [c.priced.title for c in cards if c.spare] == ["14 octobre"]
    text = friends.friend_text(cards)
    assert "×2, en double" in text and "son seul exemplaire" in text and "⭐ favorite" in text
    assert "pour 1 doublon" in text and "Hors collection" not in text
    assert friends.friend_text([]).startswith("Cet ami")
    await ctx.db.close()


@pytest.mark.asyncio
async def test_a_card_held_by_a_trade_is_not_offered(tmp_path):
    collection = {"collection": [entry("e1", "c14", "14 octobre", count=2)], "pendingTradeCardIds": ["c14"]}
    ctx = await context(tmp_path, collection=collection)
    cards = await friends.friend_cards(ctx, "alice")
    assert not cards[0].spare and "déjà dans un échange" in friends.friend_text(cards)
    await ctx.db.close()
