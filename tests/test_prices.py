"""Commande prices : ventes observées d'une carte, estimation du bot, achats et ventes en cours."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.main import prices_status
from tests.test_integration import ME, make_client, make_context

TITLE = "Liste des éclipses solaires du XXIe siècle"


def selling(aid: str, base: int, bid: int | None = None, seller: str = "s") -> dict:
    return {"id": aid, "card": {"id": "ecl21", "wikipedia_title": TITLE, "rarity": "L"}, "seller_id": seller,
            "status": "active", "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "base_amount": base, "current_bid": bid}


@pytest.mark.asyncio
async def test_prices_shows_observed_sales_estimate_and_competition(tmp_path):
    """18/10 : l'éclipse XXIe, achetée 882 et estimée ~1197, en vente à 1040 face à trois autres à 600-900."""
    market = [selling("m1", 600), selling("m2", 900), selling("me", 1040, seller=ME),
              {**selling("x", 275, 275), "card": {"id": "ecl20", "wikipedia_title": "Liste des éclipses solaires "
                                                  "du XXe siècle", "rarity": "L"}}]

    def handler(request):
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        if request.url.params.get("scope") == "summary":
            return httpx.Response(200, json={"summary": {"L": {"average": 1300}}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, base_amount, ended_at) "
        "VALUES (?, 'ecl21', ?, 'L', ?, ?, ?)",
        [(f"o{i}", TITLE, p, b, (now - timedelta(hours=i + 1)).isoformat())
         for i, (p, b) in enumerate([(1250, 900), (1197, 800), (1100, 1100), (1300, 700), (1150, 1000)])])
    await ctx.db.db.commit()
    await ctx.db.record_bid(auction_id="b", card_id="ecl21", title=TITLE, rarity="L", is_shiny=False, amount=882,
                            reference_price=1197, reason="revente ~1197 WB", night=False, dry_run=False,
                            status="won", end_at=now)
    text = await prices_status(ctx, TITLE.lower())
    assert text.startswith(TITLE + "\n")  # le vrai titre, pas celui tapé
    assert "L : 5 vente(s) observée(s) · estimation du bot ~1197 WB" in text and "moyenne du site : 1300 WB" in text
    assert "1100 WB  (départ 1100)  au prix de départ, sans surenchère" in text
    assert "réel : 882 WB, estimée ~1197" in text
    assert "En vente maintenant (3)" in text and "XXe" not in text  # l'autre carte n'est pas mêlée
    assert text.index("départ 600, aucune") < text.index("départ 900, aucune") < text.index("départ 1040, aucune")
    assert text.count("(toi)") == 1
    await ctx.db.close()


@pytest.mark.asyncio
async def test_prices_unknown_title(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"auctions": [], "hasMore": False}))
    ctx = await make_context(tmp_path, client, notifier)
    assert "ni vente observée ni vente en cours" in await prices_status(ctx, "Inconnue")
    await ctx.db.close()
