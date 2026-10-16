"""Cartes à image animée (GIF) : repérées par l'adresse de l'image, suivies dans les ventes observées."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.database import Database
from src.core.models import Card, OwnedCard
from src.main import gif_text, hours_text
from src.workers.inventory_manager import group_by_card, select_for_discard

# Relevé du 16/10 : « Royaumes francs » (UR), seule carte GIF signalée sur le marché.
FRANKS = ("https://upload.wikimedia.org/wikipedia/commons/thumb/2/23/Franks_expansion.gif/330px-Franks_expansion.gif"
          "?utm_source=fr.wikipedia.org&utm_campaign=api&utm_content=thumbnail")
AVATAR = ("https://thumb.wikimedia.org/wikipedia/fr/thumb/c/c7/Avatar_De_Feu_et_de_Cendres_Logo.png/"
          "330px-Avatar_De_Feu_et_de_Cendres_Logo.png?utm_source=fr.wikipedia.org&utm_campaign=api")


def test_gif_read_from_the_file_name_not_the_query():
    assert Card(id="1", image_url=FRANKS).is_gif
    assert not Card(id="2", image_url=AVATAR).is_gif
    assert not Card(id="3").is_gif


@pytest.mark.asyncio
async def test_gif_sales_are_tracked_and_compared(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    end = datetime.now(timezone.utc) - timedelta(minutes=5)

    def seen(aid, card, gif, price, base):
        return {"auction_id": aid, "card_id": card, "title": card, "norm_title": card, "rarity": "UR", "is_shiny": 0,
                "current_bid": price, "base_amount": base, "min_bid": price + 1, "end_at": end.isoformat(),
                "seen_at": (end - timedelta(seconds=5)).isoformat(), "is_gif": gif}

    await db.upsert_auctions([seen("g1", "franks", 1, 120, 40), seen("g2", "franks", 1, 90, 30),
                              seen("i1", "avatar", 0, 20, 20), seen("i2", "autre", 0, 30, 25)])
    assert await db.harvest_ended_auctions(datetime.now(timezone.utc)) == 4
    text = gif_text(await db.gif_stats())
    assert "UR GIF : 2 ventes (1 cartes), prix médian 105, final/départ ×3.00, 0 % sans surenchère" in text
    assert "UR img : 2 ventes (2 cartes), prix médian 25" in text
    await db.close()


def test_gif_duplicates_are_never_discarded():
    s = Settings(_env_file=None, discard_max_value=2)

    def copy(oid, card, url):
        return OwnedCard.model_validate({"id": oid, "card": {"id": card, "wikipedia_title": card, "rarity": "C",
                                                             "image_url": url}})

    groups = group_by_card([copy("a1", "franks", FRANKS), copy("a2", "franks", FRANKS),
                            copy("b1", "avatar", AVATAR), copy("b2", "avatar", AVATAR)])
    chosen = select_for_discard(groups, {}, set(), s.model_copy(update={"discard_unknown_price_is_worthless": True}))
    assert [c.owned.id for c in chosen] == ["b2"]  # le doublon GIF est gardé


@pytest.mark.asyncio
async def test_hours_groups_sales_by_paris_end_hour(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    day = datetime(2026, 10, 15, tzinfo=timezone.utc)  # Paris = UTC+2 : 10:00 UTC -> 12h, 21:00 UTC -> 23h

    def seen(aid, hour, price, base, rarity="L", shiny=0):
        end = day + timedelta(hours=hour)
        return {"auction_id": aid, "card_id": aid, "title": aid, "norm_title": aid, "rarity": rarity, "is_shiny": shiny,
                "current_bid": price, "base_amount": base, "min_bid": price + 1, "end_at": end.isoformat(),
                "seen_at": (end - timedelta(seconds=5)).isoformat(), "is_gif": 0}

    await db.upsert_auctions([seen("a", 10, 150, 100), seen("b", 10, 100, 100), seen("c", 21, 300, 100),
                              seen("d", 10, 5, 5, rarity="C"), seen("e", 10, 9, 5, shiny=1)])
    await db.harvest_ended_auctions(datetime.now(timezone.utc))
    text = hours_text(await db.hour_stats(["L"]), ["L"])
    assert "L : 3 ventes" in text
    rows = {line.split()[0]: line.split() for line in text.splitlines() if line.strip()[:2].isdigit()}
    assert rows["12h"][1:5] == ["2", "125", "×1.25", "50"] and rows["23h"][1:5] == ["1", "300", "×3.00", "0"]
    assert "Aucune vente observée" in hours_text(await db.hour_stats(["UR"]), ["UR"])
    await db.close()
