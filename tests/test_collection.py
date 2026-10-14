"""Collection : lecture tolérante, doublons, mode collection des jours de l'année."""

from __future__ import annotations

import httpx
import pytest

from src.core.api import WikiMastersAPI
from tests.test_integration import make_client


def copy(oid: str, card: str, title: str | None = None) -> dict:
    return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title or card, "rarity": "C"}}


@pytest.mark.asyncio
async def test_collection_read_survives_changes_during_paging(tmp_path):
    """Une carte gagnée pendant la lecture décale les pages : l'exemplaire revu est ignoré."""
    page0 = [copy(f"o{i}", f"c{i}") for i in range(50)]
    page1 = [copy("o49", "c49"), copy("o50", "c50")]  # o49 revu à cause du décalage

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"collection": page0 if page == 0 else page1})

    client, _ = make_client(tmp_path, handler)
    rows = await WikiMastersAPI(client).load_collection()
    assert [r.id for r in rows] == [f"o{i}" for i in range(51)]
