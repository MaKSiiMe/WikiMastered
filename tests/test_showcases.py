"""Vitrines du profil : réponse du site lue, meilleur exemplaire choisi, saisie vérifiée (relevé du 21/11)."""

from __future__ import annotations

import httpx
import pytest

from src import showcases
from tests.test_integration import make_client, make_context


def test_showcase_response_is_read_in_position_order():
    data = {"galleries": [{"gallery_index": 0, "name": "Égypte", "layout": "pyramide"},
                          {"gallery_index": 1, "name": " ", "layout": "inconnue"}],
            "showcase": [{"position": 1, "gallery_index": 0, "caption": None,
                          "user_card": {"id": "u2", "snapshot_rarity": "UR", "card": {"wikipedia_title": "Khéphren"}}},
                         {"position": 0, "gallery_index": 0,
                          "user_card": {"id": "u1", "snapshot_rarity": "L", "is_shiny": True,
                                        "card": {"wikipedia_title": "Khéops"}}},
                         {"position": 20, "gallery_index": 1, "caption": "1914",
                          "user_card": {"id": "u3", "card": {"wikipedia_title": "Sarajevo", "rarity": "SR"}}},
                         {"position": 2, "user_card": {"id": "x"}}]}
    galleries = showcases.parse(data)
    assert len(galleries) == 10
    assert galleries[0].name == "Égypte" and galleries[0].layout == "pyramide"
    assert galleries[0].cards == [("Khéops", "L", True, None), ("Khéphren", "UR", False, None)]
    assert galleries[1].name is None and galleries[1].layout == "classique"
    assert galleries[1].cards == [("Sarajevo", "SR", False, "1914")]
    text = showcases.overview(galleries)
    assert "**1. Égypte** · Pyramide" in text and "Khéops (L ✨)" in text and "Sarajevo (SR) — 1914" in text
    assert showcases.overview(showcases.parse({})) == "Aucune vitrine remplie."


def test_input_is_checked_against_the_layout():
    assert showcases.check("podium", ["Or", "Argent", "Bronze"], []) is None
    assert "3 cartes, pas 2" in showcases.check("podium", ["Or", "Argent"], [])
    assert "3 à 8 cartes" in showcases.check("frise", ["a", "b"], [])
    assert "2 légende(s) pour 3" in showcases.check("frise", ["a", "b", "c"], ["1914", "1918"])
    assert showcases.split(" Khéops ;Khéphren;; Sphinx de Gizeh ") == ["Khéops", "Khéphren", "Sphinx de Gizeh"]


@pytest.mark.asyncio
async def test_best_copy_is_the_highest_rarity_then_shiny(tmp_path):
    def row(oid, title, rarity, shiny=False):
        return {"id": oid, "card_id": f"c-{title}", "is_shiny": shiny, "snapshot_rarity": rarity,
                "card": {"id": f"c-{title}", "wikipedia_title": title, "rarity": "C"}}

    def handler(request):
        assert request.url.params["q"].casefold() == "charlemagne"
        return httpx.Response(200, json={"collection": [
            row("a", "Charlemagne", "UR"), row("b", "Charlemagne", "L"), row("c", "Charlemagne", "L", True),
            row("d", "Charlemagne (film)", "L", True)]})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    assert (await showcases.best_copy(ctx, "charlemagne")).id == "c"
    await ctx.db.close()
