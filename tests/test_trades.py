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

AUTRE = "22222222-2222-2222-2222-222222222222"
# 28/10 : Jeffrey Epstein (L) se vendait ~12 000, Emma Watson et Zendaya ~5 500 (ventes observées du 23 au 28/10).
SALES = {"jeffrey": ("Jeffrey Epstein", [11500, 12600, 13500, 12100, 11800, 12200]),
         "emma": ("Emma Watson", [5400, 5600, 5500, 5450, 5550]),
         "zendaya": ("Zendaya", [5600, 5700, 5650, 5500, 6000, 5800])}


def item(card: str, by: str) -> dict:
    return {"card_id": card, "user_card_id": f"u-{card}", "offered_by": by, "snapshot_rarity": "L",
            "card": {"id": card, "wikipedia_title": SALES[card][0], "rarity": "L"}}


def trade(tid: str, initiator: str, recipient: str, items: list[dict], status: str = "pending") -> dict:
    return {"id": tid, "status": status, "initiator_id": initiator, "recipient_id": recipient,
            "initiator": {"username": "Camille" if initiator == AUTRE else "Moi"}, "initiator_wikibidous": 0,
            "recipient_wikibidous": 0, "items": items}


async def context(tmp_path, active: list[dict], finished: list[dict] | None = None):
    def handler(request):
        if request.url.path == "/api/trades" and "active" not in request.url.params:
            return httpx.Response(200, json={"trades": active + (finished or [])})
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
    """Camille propose Emma Watson contre Jeffrey Epstein : défavorable, contre-offre Emma + ~6 500 WB ou + Zendaya."""
    offer = trade("t1", AUTRE, ME, [item("emma", AUTRE), item("jeffrey", ME)])
    mine = trade("t0", ME, AUTRE, [item("jeffrey", ME), item("emma", AUTRE), item("zendaya", AUTRE)])
    ctx = await context(tmp_path, [offer, mine])
    worker = TradeWatch(ctx)
    worker.notifier = sent = Sent()  # type: ignore[assignment]
    await worker.setup()
    await worker.step()
    assert len(sent.messages) == 1  # le mien (t0) n'est pas annoncé
    level, title, headline, kwargs = sent.messages[0]
    text = "\n".join([headline, *(f"{n}\n{v}" for n, v in kwargs["columns"] + kwargs["sections"])])
    text = text.replace("\u202f", " ")  # wb() : espace fine entre les milliers
    assert level is Level.WARNING and title == "Échange proposé par Camille"
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
    raw = trade("t", AUTRE, ME, [{"card_id": "emma", "user_card_id": "u-emma", "offered_by": ME},
                               {"card_id": "zendaya", "user_card_id": "u-z", "offered_by": AUTRE}])
    e = await trades.evaluate_trade(ctx, Trade.model_validate(raw), ME)
    assert (e.given[0].label, e.given[0].value) == ("L · Emma Watson", 5500)
    assert (e.received[0].label, e.received[0].value) == ("? · Zendaya", None)
    assert e.verdict == "incomplet"
    await ctx.db.close()


@pytest.mark.asyncio
async def test_every_trade_is_kept_with_both_sides_values_and_its_final_status(tmp_path):
    """12/11 : rendement des échanges. Chaque échange en attente, reçu ou proposé par toi, est gardé une fois avec la
    valeur de chaque côté ; quand il n'est plus en cours, son statut final est relu sur le site."""
    from src.workers.trade_watch import LOG_KEY

    offer = trade("t1", AUTRE, ME, [item("emma", AUTRE), item("jeffrey", ME)])
    mine = {**trade("t0", ME, AUTRE, [item("jeffrey", ME), item("emma", AUTRE), item("zendaya", AUTRE)]),
            "recipient": {"username": "Camille"}, "initiator_wikibidous": 500}
    active, finished = [offer, mine], []
    ctx = await context(tmp_path, active, finished)
    worker = TradeWatch(ctx)
    worker.notifier = Sent()  # type: ignore[assignment]
    await worker.setup()
    await worker.step()
    await worker.step()
    kept = await ctx.db.kv_get(LOG_KEY)
    assert list(kept) == ["t1", "t0"] and all(t["status"] == "pending" for t in kept.values())
    assert (kept["t1"]["with"], kept["t1"]["mine"], kept["t1"]["given_total"]) == ("Camille", False, 12150)
    assert [c["title"] for c in kept["t0"]["received"]] == ["Emma Watson", "Zendaya"]
    assert kept["t0"]["mine"] and kept["t0"]["wb_given"] == 500 and kept["t0"]["given_total"] == 12650
    assert kept["t0"]["received_total"] == kept["t1"]["received_total"] + 5675

    # t0 accepté, t1 disparu sans trace : statuts finaux.
    active.clear()
    finished.append({**mine, "status": "accepted"})
    await worker.step()
    kept = await ctx.db.kv_get(LOG_KEY)
    assert (kept["t0"]["status"], kept["t1"]["status"]) == ("accepted", "ended") and kept["t0"]["ended"]
    await worker.step()  # plus rien à relire
    assert await ctx.db.kv_get(KV_KEY) == []
    await ctx.db.close()


def test_trade_history_counts_only_concluded_trades():
    """/echanges : seuls les échanges conclus font le résultat ; un échange terminé sans issue connue est à part."""
    def kept(tid, seen, status, given, received):
        return {"id": tid, "seen": seen, "with": "Camille", "mine": True, "status": status, "wb_given": 500,
                "wb_received": 0, "given_total": given, "received_total": received, "unknown": 0,
                "given": [{"title": "Lucas Hernandez", "rarity": "L", "shiny": True, "value": given - 500}],
                "received": [{"title": "Philippe Lacheau", "rarity": "L", "shiny": True, "value": received}]}

    history = {t["id"]: t for t in (kept("a", "2026-11-10T22:00:00+00:00", "accepted", 9000, 6600),
                                    kept("b", "2026-11-11T08:00:00+00:00", "ended", 1000, 1500),
                                    kept("c", "2026-11-12T08:00:00+00:00", "declined", 1000, 5000),
                                    kept("d", "2026-11-12T09:00:00+00:00", "pending", 1000, 1200),
                                    kept("old", "2026-09-01T09:00:00+00:00", "accepted", 1, 99999))}
    head, lines = trades.history_text(history, "2026-11-01T00:00:00+00:00")
    head = head.replace("\u202f", " ")
    assert "**1 échange conclu** : donné ~9 000 WB, reçu ~6 600 WB, soit **-2 400 WB**" in head
    assert "1 terminé sans issue connue : +500 WB s'ils ont été acceptés" in head and "1 en attente" in head
    assert len(lines) == 4 and lines[0].startswith("**12/11 · Camille** · ⏳ en attente")
    assert "✖️ declined" in lines[1] and "✅ conclu" in lines[3]
    assert "donné : L · Lucas Hernandez ✨ + 500 WB (~9 000)" in lines[3].replace("\u202f", " ")
    assert trades.history_text({}, "2026-11-01")[0] == "Aucun échange vu sur la période."


@pytest.mark.asyncio
async def test_trades_ended_before_tracking_are_taken_back_once(tmp_path):
    """12/11 : rendement rétroactif. Les échanges terminés que le site donne encore sont repris une seule fois, à la
    date de l'échange ; la rareté d'un exemplaire déjà mis en vente par le bot est connue, même parti."""
    from src.workers.trade_watch import BACKFILL_KEY, LOG_KEY

    when = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    past = {**trade("old", ME, AUTRE, [{"card_id": "jeffrey", "user_card_id": "u-jef", "offered_by": ME},
                                       item("emma", AUTRE)], status="accepted"),
            "recipient": {"username": "Camille"}, "recipient_wikibidous": 1000, "updated_at": when}
    refused = {**trade("no", AUTRE, ME, [item("emma", AUTRE)], status="declined"), "updated_at": when}
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": [] if "active" in request.url.params else [past, refused]})
        if request.url.path.endswith("/sales"):
            return httpx.Response(200, json={"summary": {}, "wikipedia_title": "Jeffrey Epstein"})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at) VALUES (?, ?, ?, 'L', ?, ?)",
        [(f"{card}{i}", card, title, price, (now - timedelta(hours=i + 3)).isoformat())
         for card, (title, prices) in SALES.items() for i, price in enumerate(prices)])
    await ctx.db.db.commit()
    await ctx.db.schedule_listing(owned_card_id="u-jef", card_id="jeffrey", title="Jeffrey Epstein", rarity="L",
                                  base_amount=12000, duration_minutes=60, post_at=now, target_end=now, reason="x")
    worker = TradeWatch(ctx)
    worker.notifier = Sent()  # type: ignore[assignment]
    await worker.setup()
    await worker.step()
    kept = await ctx.db.kv_get(LOG_KEY)
    assert set(kept) == {"old", "no"} and await ctx.db.kv_get(BACKFILL_KEY)
    old = kept["old"]
    assert (old["status"], old["retro"], old["with"], old["wb_received"]) == ("accepted", True, "Camille", 1000)
    assert old["given"] == [{"title": "Jeffrey Epstein", "rarity": "L", "shiny": False, "value": 12150}]
    assert old["received_total"] == 5500 + 1000 and old["unknown"] == 0
    head, lines = trades.history_text(kept, "2026-01-01")
    assert "**1 échange conclu**" in head and "↩︎" in lines[0] + lines[1]
    before = calls.count("/api/trades")
    await worker.step()
    assert calls.count("/api/trades") == before + 1  # seulement les échanges actifs : la reprise n'est pas refaite
    await ctx.db.close()

