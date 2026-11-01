"""Échanges : valeur de chaque côté, verdict, contre-offre (src/trades.py, worker echanges, /echange)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import trades
from src.core.models import Trade
from src.core.notifier import Level
from src.workers.trade_watch import KV_KEY, TradeWatch
from tests.test_integration import ME, make_client, make_context

OLE = "22222222-2222-2222-2222-222222222222"
# 28/10 : Jeffrey Epstein (L) se vendait ~12 000, Emma Watson et Zendaya ~5 500 (ventes observées du 23 au 28/10).
SALES = {"jeffrey": ("Jeffrey Epstein", [11500, 12600, 13500, 12100, 11800, 12200]),
         "emma": ("Emma Watson", [5400, 5600, 5500, 5450, 5550]),
         "zendaya": ("Zendaya", [5600, 5700, 5650, 5500, 6000, 5800])}


def item(card: str, by: str) -> dict:
    return {"card_id": card, "user_card_id": f"u-{card}", "offered_by": by, "snapshot_rarity": "L",
            "card": {"id": card, "wikipedia_title": SALES[card][0], "rarity": "L"}}


def trade(tid: str, initiator: str, recipient: str, items: list[dict], status: str = "pending") -> dict:
    return {"id": tid, "status": status, "initiator_id": initiator, "recipient_id": recipient,
            "initiator": {"username": "Olé" if initiator == OLE else "MaKSiiMe"}, "initiator_wikibidous": 0,
            "recipient_wikibidous": 0, "items": items}


async def context(tmp_path, active: list[dict]):
    def handler(request):
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": active})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    rows = [(f"{card}{i}", card, title, price, (now - timedelta(hours=i + 1)).isoformat())
            for card, (title, prices) in SALES.items() for i, price in enumerate(prices)]
    await ctx.db.db.executemany("INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at) "
                                "VALUES (?, ?, ?, 'L', ?, ?)", rows)
    await ctx.db.db.commit()
    return ctx


class Sent:
    def __init__(self):
        self.messages: list[tuple[Level, str, str, dict]] = []

    def send(self, level, title, description="", **kwargs):
        self.messages.append((level, title, description, kwargs))


@pytest.mark.asyncio
async def test_unfair_offer_is_announced_once_with_a_counter_offer(tmp_path):
    """Olé propose Emma Watson contre Jeffrey Epstein : défavorable, contre-offre Emma + ~6 500 WB ou + Zendaya."""
    offer = trade("t1", OLE, ME, [item("emma", OLE), item("jeffrey", ME)])
    mine = trade("t0", ME, OLE, [item("jeffrey", ME), item("emma", OLE), item("zendaya", OLE)])
    ctx = await context(tmp_path, [offer, mine])
    worker = TradeWatch(ctx)
    worker.notifier = sent = Sent()  # type: ignore[assignment]
    await worker.setup()
    await worker.step()
    assert len(sent.messages) == 1  # le mien (t0) n'est pas évalué
    level, title, headline, kwargs = sent.messages[0]
    text = "\n".join([headline, *(f"{n}\n{v}" for n, v in kwargs["columns"] + kwargs["sections"])])
    text = text.replace("\u202f", " ")  # wb() : espace fine entre les milliers
    assert level is Level.WARNING and title == "Échange proposé par Olé"
    assert "❌ Défavorable : -6 650 WB" in text
    assert "Tu donnes ~12 150 WB\n**L · Jeffrey Epstein**\n~12 150 WB · 6 ventes en 7 j" in text
    assert "L · Emma Watson + **6 700 WB**" in text
    assert "une carte vers 6 650 WB" not in text  # Zendaya (~5 675) est hors de la fourchette 0,9 – 1,6 × l'écart
    await worker.step()
    assert len(sent.messages) == 1  # déjà annoncé
    assert await ctx.db.kv_get(KV_KEY) == ["t1"]
    await ctx.db.close()


@pytest.mark.asyncio
async def test_two_for_one_is_fair_and_counter_offer_suggests_a_liquid_card(tmp_path):
    ctx = await context(tmp_path, [])
    jeffrey, emma, zendaya = [await trades.value_of_title(ctx, t) for t in
                              ("Jeffrey Epstein", "L · Emma Watson", "zendaya (L)")]
    assert (emma.title, emma.rarity, emma.value, emma.sales) == ("Emma Watson", "L", 5500, 5)
    fair = await trades.evaluate(ctx, [jeffrey], [emma, zendaya])
    assert fair.verdict == "equitable" and fair.offers == []
    assert "Tu reçois 92 % de ce que tu donnes." in fair.text and "**L · Zendaya**" in fair.text
    # Emma contre 1 500 WB : il manque 4 000 ; Zendaya est proposée, pas Jeffrey (trop cher) ni Emma (déjà là).
    bad = await trades.evaluate(ctx, [emma], [], 0, 1500)
    assert bad.verdict == "defavorable"
    offers = [o.replace("\u202f", " ") for o in bad.offers]
    assert offers[0] == "rien + **4 000 WB**"
    assert "une carte vers 4 000 WB" in offers[1] and "L · Zendaya (~5 708" in offers[1] and "Jeffrey" not in offers[1]
    # Deux cartes données pour une : en retirer une rend l'échange équitable.
    two = await trades.evaluate(ctx, [emma, zendaya], [await trades.value_of_title(ctx, "Emma Watson")])
    assert any(o.startswith("ou retire **L · Emma Watson**") for o in two.offers)  # Zendaya, plus chère, reste
    unknown = await trades.value_of_title(ctx, "Carte inconnue ✨")
    assert unknown.shiny and unknown.value is None
    partial = await trades.evaluate(ctx, [unknown], [emma])
    assert "sans valeur connue, comptée 0" in partial.text and partial.verdict == "incomplet"
    # Cartes illisibles contre rien (jarry0, 30/10) : pas « Équitable ».
    blind = await trades.evaluate(ctx, [trades.Priced("?", None), trades.Priced("?", None)], [])
    assert blind.verdict == "incomplet" and blind.headline.startswith("❓") and blind.offers == []
    await ctx.db.close()


def test_card_text_is_parsed():
    assert trades.parse_card("L · Emma Watson") == ("Emma Watson", "L", False)
    assert trades.parse_card("Zendaya (L) ✨") == ("Zendaya", "L", True)
    assert trades.parse_card("Jeffrey Epstein shiny") == ("Jeffrey Epstein", None, True)
    assert trades.parse_card("Affaire (UR) X") == ("Affaire (UR) X", None, False)


@pytest.mark.asyncio
async def test_trade_items_with_ids_only_are_named_and_priced(tmp_path):
    """Lpancho, 01/11 : le site ne donne que les identifiants. Ma carte : titre et rareté retrouvés, cotée ; la sienne :
    titre retrouvé, rareté inconnue, donc pas de valeur inventée."""
    def handler(request):
        path = request.url.path
        if path.endswith("/emma/sales"):
            return httpx.Response(200, json={"summary": {}, "wikipedia_title": "Emma Watson"})
        if path.endswith("/zendaya/sales"):
            return httpx.Response(200, json={"summary": {}, "wikipedia_title": "Zendaya"})
        if path == "/api/my-collection":
            assert request.url.params["q"] == "Emma Watson"
            return httpx.Response(200, json={"collection": [
                {"id": "u-emma", "card_id": "emma", "snapshot_rarity": "L",
                 "card": {"id": "emma", "wikipedia_title": "Emma Watson", "rarity": "L"}}]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at) VALUES (?, ?, ?, 'L', ?, ?)",
        [(f"e{i}", "emma", "Emma Watson", p, (now - timedelta(hours=i + 1)).isoformat())
         for i, p in enumerate(SALES["emma"][1])])
    await ctx.db.db.commit()
    raw = trade("t", OLE, ME, [{"card_id": "emma", "user_card_id": "u-emma", "offered_by": ME},
                               {"card_id": "zendaya", "user_card_id": "u-z", "offered_by": OLE}])
    e = await trades.evaluate_trade(ctx, Trade.model_validate(raw), ME)
    assert (e.given[0].label, e.given[0].value) == ("L · Emma Watson", 5500)
    assert (e.received[0].label, e.received[0].value) == ("? · Zendaya", None)
    assert e.verdict == "incomplet"
    await ctx.db.close()

