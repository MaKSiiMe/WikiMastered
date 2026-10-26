"""Cotes de toute la collection : derniers exemplaires compris, cotes de fond, cotes basses figées."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config import Settings
from src.core.pricing import Quote
from src.workers.value_scan import ValueScan, frozen_value, to_quote
from tests.test_integration import make_client, make_context

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
WEEK, MONTH = timedelta(days=7), timedelta(days=30)


def test_cards_to_quote_most_viewed_first_then_oldest():
    cards = {"fresh": 0, "old": 0, "older": 0, "never": 10, "popular": 9000, "unsold": 0, "unsold_old": 0, "low": 0}
    cache = {"fresh": (NOW - timedelta(days=1), True), "old": (NOW - timedelta(days=20), True),
             "older": (NOW - timedelta(days=25), True), "unsold": (NOW - timedelta(days=20), False),
             "unsold_old": (NOW - timedelta(days=40), False), "low": (NOW - timedelta(days=40), True)}
    order = to_quote(cards, cache, {"low"}, NOW, WEEK, MONTH)
    assert order == ["popular", "never", "unsold_old", "older", "old"]  # sans vente : recotée après 30 jours


def test_low_quotes_are_frozen_only_when_every_owned_rarity_is_low():
    assert frozen_value({"C": Quote(12, 0), "PC": Quote(30, 0)}, {"C", "PC"}, 50) == {"C": 12, "PC": 30}
    assert frozen_value({"C": Quote(12, 0), "L": Quote(300, 0)}, {"C", "L"}, 50) is None
    assert frozen_value({"C": Quote(12, 0)}, {"C", "PC"}, 50) is None   # une rareté sans cote
    assert frozen_value({"C": Quote(None, 0)}, {"C"}, 50) is None       # aucune vente : pas de prix trouvé
    assert frozen_value({"C": Quote(12, 0)}, {"C"}, 0) is None          # VALUE_SCAN_FREEZE_BELOW=0


@pytest.mark.asyncio
async def test_scan_quotes_every_card_silently_and_freezes_cheap_ones(tmp_path):
    collection = [{"id": "o1", "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"}},
                  {"id": "o2", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Banal", "rarity": "PC",
                                                          "pageviews": 500}},
                  {"id": "o3", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Banal", "rarity": "PC"}}]
    summaries = {"c1": {"C": {"average": 240}}, "c2": {"PC": {"average": 8}}}
    calls = {"sales": [], "limited": 1}
    messages = []

    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": collection})
        if path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if path.endswith("/sales"):
            card = path.split("/")[-2]
            calls["sales"].append(card)
            if calls["limited"]:  # 1re cote : route limitée, la cote est redemandée plus tard
                calls["limited"] -= 1
                return httpx.Response(403, headers={"Retry-After": "0"}, json={"error": "Too many requests"})
            return httpx.Response(200, json={"summary": summaries[card]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, value_scan_lookup_seconds=3, backoff_rate_limit_base_seconds=60,
                                   sell_min_value=5)
    notifier.send = lambda level, title, description="", **kw: messages.append((title, description))
    ctx = await make_context(tmp_path, client, notifier)
    worker = ValueScan(ctx)
    await worker.setup()
    assert await worker.step() > 0 and calls["sales"] == []  # pas tout de suite après le démarrage
    worker._next_run = datetime.now(UTC)
    await worker.step()  # lecture de la collection
    assert worker._queue == ["c2", "c1"]  # la plus vue sur Wikipédia d'abord
    delay = await worker.step()  # 1re cote : limitée, elle attend et le rythme ralentit
    assert worker._queue == ["c2", "c1"] and worker._spacing == 6 and delay > 60
    client._route_pause.clear()
    while worker._queue:
        await worker.step()
    await worker.step()  # fin du passage, sans aucun message ni ligne de récap
    assert calls["sales"] == ["c2", "c2", "c1"]
    assert not messages and not worker.recap()
    assert worker._next_run - datetime.now(UTC) > timedelta(hours=11)
    assert await ctx.db.frozen_values() == {"c2": {"PC": 8.0}}  # sous 50 WB : figée

    # Un mois plus tard (après un redémarrage) : la cote de Mème est redemandée, jamais celle de Banal, figée.
    await ctx.db.db.execute("UPDATE price_cache SET fetched_at = ?", ((datetime.now(UTC) - MONTH).isoformat(),))
    await ctx.db.db.commit()
    again = ValueScan(ctx)
    await again.setup()
    assert again._next_run == worker._next_run
    again._next_run = datetime.now(UTC)
    await again.step()
    assert again._queue == ["c1"]
    await again.step()
    await again.step()
    assert calls["sales"] == ["c2", "c2", "c1", "c1"] and not messages

    # La vente des doublons reprend la cote figée, sans la redemander.
    from src.workers.inventory_manager import InventoryManager
    _, _, quotes, _ = await InventoryManager(ctx).snapshot()
    assert quotes["c2"] == {"PC": Quote(8.0, 0)} and calls["sales"].count("c2") == 2
    await ctx.db.close()


@pytest.mark.asyncio
async def test_background_requests_wait_for_the_others(tmp_path):
    """Une cote de fond ne part que si personne n'attend : le sniper ne patiente jamais derrière l'analyse."""
    order = []

    def handler(request):
        order.append(request.url.path)
        return httpx.Response(200, json={"balance": 1, "summary": {}})

    client, _ = make_client(tmp_path, handler, http_min_spacing_seconds=0.05)
    await client.get("balance")
    background = asyncio.create_task(client.get("sales_summary", card_id="x", background=True))
    await asyncio.sleep(0)
    first, second = (asyncio.create_task(client.get("market_page")) for _ in range(2))
    await asyncio.gather(background, first, second)
    assert order == ["/api/wikibidous", "/api/marketplace", "/api/marketplace", "/api/marketplace/cards/x/sales"]


@pytest.mark.asyncio
async def test_refused_quote_falls_back_on_the_old_site_average(tmp_path):
    """Route des cotes refusée : l'ancienne moyenne du site plafonne encore l'estimation du sniper."""
    from tests.test_integration import seed_observed

    def handler(request):
        return httpx.Response(403, headers={"Retry-After": "60"}, json={"error": "Too many requests"})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    await seed_observed(ctx.db, "c1", [400, 410, 390], rarity="L")
    await ctx.db.price_put("c1", {"L": (300.0, None)})
    await ctx.db.db.execute("UPDATE price_cache SET fetched_at = ?", ((datetime.now(UTC) - timedelta(days=2)).isoformat(),))
    await ctx.db.db.commit()
    assert (await ctx.prices.quote("c1", "L", wait=False)).average == 300
    await ctx.db.close()


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert (s.enable_value_scan, s.value_scan_hours, s.manual_resale_tag) == (True, 12.0, "revente")
    assert (s.value_scan_lookups, s.value_scan_lookup_seconds, s.value_scan_freeze_below) == (0, 3.0, 50)



@pytest.mark.asyncio
async def test_pace_slows_on_refusal_and_speeds_up_gently(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}), value_scan_lookup_seconds=3)
    worker = ValueScan(await make_context(tmp_path, client, notifier))
    await worker.setup()
    worker._slow_down()
    worker._slow_down()
    assert worker._spacing == 12
    for _ in range(20):
        worker._accepted()
    assert worker._spacing == 9
    for _ in range(100):
        worker._accepted()
    assert worker._spacing == 3  # jamais sous VALUE_SCAN_LOOKUP_SECONDS
    await worker.db.close()


@pytest.mark.asyncio
async def test_server_errors_pause_the_scan_without_slowing_it(tmp_path):
    """18/10 au soir : des 500 en série (site instable) faisaient tomber l'analyse à une cote toutes les 182 s."""
    def handler(request):
        if request.url.path.endswith("/sales"):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"collection": []})

    client, notifier = make_client(tmp_path, handler, value_scan_lookup_seconds=3, backoff_base_seconds=30)
    ctx = await make_context(tmp_path, client, notifier)
    worker = ValueScan(ctx)
    await worker.setup()
    await worker.start_cycle(datetime.now(UTC))
    worker._queue = ["c1"]
    delay = await worker.lookup()
    assert worker._queue == ["c1"] and worker._spacing == 3 and delay > 30  # attend la fin de la pause
    assert await worker.lookup() > 25  # toujours en pause : aucune requête, pas de ralentissement
    assert worker._spacing == 3
    await ctx.db.close()
