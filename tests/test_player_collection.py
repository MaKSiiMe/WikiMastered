"""Collection publique d'un autre joueur, comparée à la liste des GIF animés."""

from __future__ import annotations

import httpx
import pytest

from src.core.models import OwnedCard
from src.player_collection import analyse, summary
from tests.test_integration import make_client, make_context


def copy(oid, title, rarity="C", image="Anim.gif", card=None):
    return {"id": oid, "card_id": card or title, "snapshot_rarity": rarity,
            "card": {"id": card or title, "wikipedia_title": title, "rarity": rarity,
                     "image_url": f"https://upload.wikimedia.org/{image}?utm_source=x" if image else None}}


def test_analyse_counts_cards_by_rarity_and_checks_the_list():
    rows = [OwnedCard.model_validate(r) for r in (
        copy("a1", "Listée", "L"), copy("a2", "Listée", "L"), copy("b", "Listée 2", "R"), copy("b2", "Listée 2", "SR"),
        copy("c", "Oubliée", "SR"), copy("d", "Fixe", "C", "Fixe.gif"), copy("e", "Photo", "C", "x.jpg"),
        copy("f", "Sans lecture", "C", "Inconnu.gif"))]
    state = analyse(rows, ("Listée", "Listée 2", "Absente"), {"Anim.gif": 17, "Fixe.gif": 1})
    assert state["copies"] == 8 and state["cards"] == 6
    assert state["by_rarity"] == {"L": 1, "SR": 2, "R": 1, "C": 3}
    assert (state["in_list"], state["list_total"]) == (2, 3)
    assert state["outside_animated"] == ["Oubliée"]
    assert (state["outside_fixed"], state["outside_unknown"]) == (2, 1)


@pytest.mark.asyncio
async def test_reads_every_page_of_the_tag_and_keeps_the_result(tmp_path, monkeypatch):
    from src import player_collection

    seen = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        page = int(request.url.params["page"])
        rows = [copy(f"o{i}", f"Carte {i}", "R", "x.jpg") for i in range(50)] if page == 0 else [copy("z", "Fin", "L", "x.jpg")]
        return httpx.Response(200, json={"collection": rows, "stats": {"total": 51}})

    client, notifier = make_client(tmp_path, site)
    ctx = await make_context(tmp_path, client, notifier)
    state = await player_collection.scan(ctx, "un_joueur", "tag-1")
    assert [u.path for u in seen] == ["/api/profile/un_joueur/collection"] * 2
    assert seen[0].params["tag_id"] == "tag-1" and seen[1].params["page"] == "1"
    assert state["cards"] == 51 and state["by_rarity"] == {"L": 1, "R": 50}
    assert '"total": 51' in state["extra"]
    assert (await ctx.db.kv_get(player_collection.KV_KEY))["username"] == "un_joueur"
    assert "51 cartes (51 exemplaires)" in summary(state)
    await ctx.db.close()


@pytest.mark.asyncio
async def test_unknown_answer_is_shown_rather_than_read_as_empty(tmp_path):
    from src import player_collection

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"cards": [{"x": 1}]}))
    ctx = await make_context(tmp_path, client, notifier)
    text = summary(await player_collection.scan(ctx, "un_joueur"))
    assert "Réponse du site sans carte reconnue" in text and '"cards"' in text
    await ctx.db.close()
