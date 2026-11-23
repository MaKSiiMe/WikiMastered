"""/collection amis et le worker « friends » : les cartes de la liste de souhaits chez les amis, en lecture seule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import friends
from src.workers.friend_watch import NOTIFIED_KEY, FriendWatch
from tests.test_integration import make_client, make_context

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def catalogue_page(cards, owners):
    return {"cards": [{"id": cid, "wikipedia_title": title} for cid, title in cards], "friendOwners": owners}


def owner(name, *rarities):
    return {"id": f"u-{name}", "username": name, "rarities": list(rarities)}


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
async def test_friends_are_ranked_with_the_rarities_that_help(tmp_path):
    full = [(f"x{i}", f"Autre {i}") for i in range(47)] + [("c14", "14 octobre"), ("c2", "2 mars"), ("w1", "Napoléon")]
    pages = [catalogue_page(full, {
                 "c14": [owner("bob", "C"), owner("alice", "SR", "R")],
                 "c2": [owner("alice", "PC")],
                 # Carte souhaitée que j'ai en SR : seule la L de carl m'avance, pas la R de dave.
                 "w1": [owner("carl", "L", "R"), owner("dave", "R")],
                 # Ajoutée à ma liste depuis le dernier passage (ni manquante ni suivie) : toute rareté.
                 "x1": [owner("erin", "UR")]}),
             catalogue_page([("c7", "7 juin")], {"c7": []})]
    calls: list = []
    ctx = await context(tmp_path, pages, calls=calls)
    await ctx.db.kv_set("wishes", {"cards": {"w1": {"title": "Napoléon", "best": 3}}})
    state = await friends.overview(ctx, now=NOW)
    assert state["friends"] == {
        "alice": [{"id": "c14", "title": "14 octobre", "rarities": ["SR", "R"]},
                  {"id": "c2", "title": "2 mars", "rarities": ["PC"]}],
        "bob": [{"id": "c14", "title": "14 octobre", "rarities": ["C"]}],
        "carl": [{"id": "w1", "title": "Napoléon", "rarities": ["L"]}],
        "erin": [{"id": "x1", "title": "Autre 1", "rarities": ["UR"]}]}
    assert state["cards"] == 51 and len(calls) == 2
    text = friends.overview_text(state)
    assert "**4 cartes** de ta liste chez **4 amis**, sur 51 souhaitées" in text
    assert "**alice** (2) : 14 octobre (SR, R), 2 mars (PC)" in text and "dave" not in text

    # Moins d'une heure après : aucune requête, sauf si on le demande.
    await friends.overview(ctx, now=NOW + timedelta(minutes=30))
    assert len(calls) == 2
    await friends.overview(ctx, now=NOW + timedelta(minutes=30), refresh=True)
    assert len(calls) == 4
    await friends.overview(ctx, now=NOW + timedelta(hours=2))
    assert len(calls) == 6
    await ctx.db.close()


def test_a_favorite_already_in_l_helps_with_nothing():
    need = friends.Needs(frozenset({"14 octobre"}), {"f1": 5, "w1": 2})
    assert need.useful("f1", "Favori", ["L", "UR"]) is None
    assert need.useful("w1", "Souhait", ["PC", "R", "UR", "SR"]) == ["UR", "SR"]
    assert need.useful("c14", "14 octobre", ["c"]) == ["C"]  # casse du site ignorée
    assert need.useful("w1", "Souhait", []) == []  # raretés non données : gardée
    assert need.useful("w1", "Souhait", ["SHINY"]) == ["SHINY"]  # rareté inconnue du bot : gardée


@pytest.mark.asyncio
async def test_worker_announces_each_card_once(tmp_path):
    pages = [catalogue_page([("c14", "14 octobre"), ("c2", "2 mars")],
                            {"c14": [owner("alice", "R")], "c2": []})]
    ctx = await context(tmp_path, pages)
    sent: list = []
    ctx.notifier.send = lambda level, title, description="", **kw: sent.append((title, description, kw.get("topic")))
    watch = FriendWatch(ctx)
    ctx.settings.friends_check_hours = 6
    delay = await watch.step()
    assert 5 * 3600 < delay < 7 * 3600
    assert len(sent) == 1 and "**alice** (1) : 14 octobre (R)" in sent[0][1] and sent[0][2].name == "TRADES"

    await watch.step()  # rien de nouveau : pas de message
    assert len(sent) == 1

    pages[0] = catalogue_page([("c14", "14 octobre"), ("c2", "2 mars")],
                              {"c14": [owner("alice", "R")], "c2": [owner("bob", "SR")]})
    await watch.step()
    assert len(sent) == 2 and "bob" in sent[1][1] and "alice" not in sent[1][1]
    assert await ctx.db.kv_get(NOTIFIED_KEY) == ["alice|c14", "bob|c2"]

    # alice l'a échangée puis récupérée : annoncée de nouveau.
    pages[0] = catalogue_page([("c14", "14 octobre"), ("c2", "2 mars")], {"c14": [], "c2": [owner("bob", "SR")]})
    await watch.step()
    pages[0] = catalogue_page([("c14", "14 octobre"), ("c2", "2 mars")],
                              {"c14": [owner("alice", "R")], "c2": [owner("bob", "SR")]})
    await watch.step()
    assert len(sent) == 3 and "alice" in sent[2][1]
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
                                 entry("e4", "w1", "Souhait en SR", rarity="R", count=5),
                                 entry("e5", "w2", "Souhait récent", rarity="PC", count=4)],
                  "pendingTradeCardIds": []}
    ctx = await context(tmp_path, collection=collection)
    await ctx.db.kv_set("wishes", {"cards": {"w1": {"title": "Souhait en SR", "best": 3}}})
    cards = await friends.friend_cards(ctx, "alice")
    # Sa R d'une carte que j'ai en SR ne m'avance pas ; une carte ajoutée depuis le dernier passage compte.
    assert {c.priced.title for c in cards} == {"14 octobre", "2 mars", "7 juin", "Souhait récent"}
    assert {c.priced.title for c in cards if c.spare} == {"14 octobre", "Souhait récent"}
    text = friends.friend_text(cards)
    assert "×2, en double" in text and "son seul exemplaire" in text and "⭐ favorite" in text
    assert "pour 2 doublons" in text and "Souhait en SR" not in text
    assert friends.friend_text([]).startswith("Cet ami")
    await ctx.db.close()


@pytest.mark.asyncio
async def test_a_card_held_by_a_trade_is_not_offered(tmp_path):
    collection = {"collection": [entry("e1", "c14", "14 octobre", count=2)], "pendingTradeCardIds": ["c14"]}
    ctx = await context(tmp_path, collection=collection)
    cards = await friends.friend_cards(ctx, "alice")
    assert not cards[0].spare and "déjà dans un échange" in friends.friend_text(cards)
    await ctx.db.close()
