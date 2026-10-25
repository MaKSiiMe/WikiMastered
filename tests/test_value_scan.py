"""Cartes de valeur de l'inventaire : derniers exemplaires compris, cotes de fond, cotes basses figées."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config import Settings
from src.core.models import OwnedCard
from src.core.pricing import Quote
from src.workers.value_scan import (ValueScan, find_valuable, frozen_value, plan_value_tags, remembered, tier_of,
                                   to_quote, to_report)
from tests.test_integration import make_client, make_context

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
WEEK, MONTH = timedelta(days=7), timedelta(days=30)


def owned(oid: str, card: str, rarity: str = "C", **kw) -> OwnedCard:
    return OwnedCard.model_validate({"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": card.title(),
                                                                         "rarity": rarity}, **kw})


def groups_of(*rows: OwnedCard) -> dict[str, list[OwnedCard]]:
    from src.workers.inventory_manager import group_by_card
    return group_by_card(list(rows))


def test_last_copy_of_a_common_card_is_found():
    """La vente auto ne vend que des doublons : une C à 150 WB en un seul exemplaire n'était jamais vue."""
    groups = groups_of(owned("o1", "meme"), owned("o2", "pomme"), owned("o3", "pomme"), owned("o4", "banal"))
    quotes = {"meme": {"C": Quote(150, 0)}, "pomme": {"C": Quote(120, 3)}, "banal": {"C": Quote(40, 0)}}
    found = find_valuable(groups, quotes, set(), min_value=100, keep=1)
    assert [(v.title, v.spare, v.copies, v.observed) for v in found] == [("Meme", False, 1, 0), ("Pomme", True, 2, 3)]


def test_protected_and_favorite_copies_are_not_reported():
    groups = groups_of(owned("o1", "fav", starred=True), owned("o2", "date"), owned("o3", "shiny", is_shiny=True))
    quotes = {cid: {"C": Quote(500, 0)} for cid in ("fav", "date", "shiny")}
    found = find_valuable(groups, quotes, {"o2"}, min_value=100, keep=1)
    assert [(v.title, v.shiny) for v in found] == [("Shiny", True)]  # favori et étiquette protégée : ignorés


def test_quote_of_another_rarity_is_not_used():
    groups = groups_of(owned("o1", "meme", "C"))
    assert find_valuable(groups, {"meme": {"L": Quote(900, 5)}}, set(), min_value=100, keep=1) == []


def test_report_only_new_cards_or_big_rises():
    groups = groups_of(owned("o1", "a"), owned("o2", "b"), owned("o3", "c"))
    quotes = {"a": {"C": Quote(200, 0)}, "b": {"C": Quote(160, 0)}, "c": {"C": Quote(300, 0)}}
    found = find_valuable(groups, quotes, set(), min_value=100, keep=1)
    reported = {"a|C": 190.0, "b|C": 100.0, "gone|C": 500.0, "later|C": 400.0}
    assert [v.title for v in to_report(found, reported)] == ["C", "B"]  # nouvelle ; cote +60 %
    # Vendue ou retombée : oubliée ; en attente de sa nouvelle cote : gardée.
    assert remembered(found, reported, {"later"}) == {"a|C": 200.0, "b|C": 160.0, "c|C": 300.0, "later|C": 400.0}


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
async def test_scan_reports_valuable_cards_and_freezes_cheap_ones(tmp_path):
    collection = [{"id": "o1", "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"}},
                  {"id": "o2", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Banal", "rarity": "PC",
                                                          "pageviews": 500}},
                  {"id": "o3", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Banal", "rarity": "PC"}}]
    summaries = {"c1": {"C": {"average": 240}}, "c2": {"PC": {"average": 8}}}
    end = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    rival = {"id": "a1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"}, "status": "active",
             "seller_id": "22222222-2222-2222-2222-222222222222", "end_at": end, "base_amount": 180,
             "current_bid": None, "current_bidder_id": None}
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
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [rival]})
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
    await worker.step()  # analyse et message
    assert calls["sales"] == ["c2", "c2", "c1"]
    assert [t for t, _ in messages] == ["1 carte(s) de valeur dans ton inventaire"]
    assert "Mème" in messages[0][1] and "~240 WB (moyenne du site) · dernier exemplaire" in messages[0][1]
    assert "1 en vente, dès 180 WB" in messages[0][1]
    assert worker.recap()[0].endswith("2/2 cartes cotées, 1 à 100 WB ou plus (1 nouvelle(s))")
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
    assert calls["sales"] == ["c2", "c2", "c1", "c1"] and len(messages) == 1  # rien de nouveau à signaler

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
    assert (s.enable_value_scan, s.value_scan_hours, s.value_scan_min_value) == (True, 12.0, 100)
    assert (s.value_scan_lookups, s.value_scan_lookup_seconds, s.value_scan_freeze_below) == (0, 3.0, 50)
    assert s.value_tiers() == {}  # pas d'étiquettes de palier sans VALUE_TAG_TIERS
    s = Settings(_env_file=None, value_tag_tiers="100,1000,500")
    assert s.value_tiers() == {1000: "💎 1000+", 500: "💎 500+", 100: "💎 100+"}


TIERS = {1000: "💎 1000+", 500: "💎 500+", 100: "💎 100+"}


def tagged(oid: str, card: str, *tags: str, **kw) -> OwnedCard:
    return owned(oid, card, tags=[{"id": 1, "name": t} for t in tags], **kw)


def test_tier_of_takes_the_highest_tier_reached():
    assert [tier_of(v, TIERS) for v in (99, 100, 499, 500, 10972)] == [None, "💎 100+", "💎 100+", "💎 500+",
                                                                       "💎 1000+"]


def test_value_tags_follow_the_quote():
    groups = groups_of(
        owned("n1", "neuve", is_shiny=True), owned("n2", "neuve"),       # nouvelle : le non-shiny est étiqueté
        tagged("m1", "montee", "💎 100+"),                                # passée à 600 : palier changé, même exemplaire
        tagged("d1", "double", "💎 500+"), tagged("d2", "double", "💎 500+"),  # un seul exemplaire garde l'étiquette
        tagged("t1", "tombee", "💎 100+"),                                # retombée sous 100 : étiquette retirée
        tagged("a1", "attente", "💎 1000+"),                              # en attente de sa cote : gardée
        owned("b1", "achetee", "L"),                                      # achetée par le bot pour la revente
        tagged("v1", "vendue", "💎 100+"),                                # en vente (exclue) : pas touchée
        tagged("f1", "favori", "💎 100+", starred=True))                   # favori : pas touché
    quotes = {"neuve": {"C": Quote(1200, 0)}, "montee": {"C": Quote(600, 0)}, "double": {"C": Quote(700, 0)},
              "tombee": {"C": Quote(60, 0)}, "attente": {"C": Quote(None, 0)}, "achetee": {"L": Quote(2000, 4)},
              "vendue": {"C": Quote(20, 0)}, "favori": {"C": Quote(20, 0)}}
    found = find_valuable(groups, quotes, {"v1"}, min_value=100, keep=1)
    adds, removes = plan_value_tags(groups, found, {"v1"}, {"attente"}, {"achetee"}, TIERS)
    assert adds == [("n2", "💎 1000+"), ("m1", "💎 500+")]
    assert removes == [("m1", "💎 100+"), ("d2", "💎 500+"), ("t1", "💎 100+")]
    # Une fois posées, rien ne bouge.
    groups = groups_of(tagged("n2", "neuve", "💎 1000+"), tagged("m1", "montee", "💎 500+"))
    found = find_valuable(groups, quotes, set(), min_value=100, keep=1)
    assert plan_value_tags(groups, found, set(), set(), set(), TIERS) == ([], [])


@pytest.mark.asyncio
async def test_scan_tags_valuable_cards_and_protects_them(tmp_path):
    import json as jsonlib

    from src.workers.inventory_manager import excluded_copies
    from tests.test_session import cookie_for

    def row(oid, card, title, tags=()):
        return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title, "rarity": "C"},
                "tags": [{"id": 9, "name": t} for t in tags]}
    collection = [row("o1", "c1", "Volley albanais"), row("o2", "c2", "Mème", ["💎 100+"]),
                  row("o3", "c3", "Banal", ["💎 100+"])]
    summaries = {"c1": {"C": {"average": 2682}}, "c2": {"C": {"average": 640}}, "c3": {"C": {"average": 30}}}
    supabase = []

    def handler(request):
        path = request.url.path
        if request.url.host.endswith("supabase.co"):
            body = jsonlib.loads(request.content) if request.content else None
            supabase.append((request.method, path, body, dict(request.url.params)))
            if path == "/rest/v1/tags" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 9, "name": "💎 100+"}, {"id": 3, "name": "date"}])
            if path == "/rest/v1/tags":  # étiquette absente : créée
                return httpx.Response(201, json=[{"id": 20 + len(supabase), "name": body["name"]}])
            return httpx.Response(204 if request.method == "DELETE" else 201)
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": collection})
        if path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if path.endswith("/sales"):
            return httpx.Response(200, json={"summary": summaries[path.split("/")[-2]]})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, value_tag_tiers=[1000, 500, 100],
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    worker = ValueScan(ctx)
    await worker.setup()
    worker._next_run = datetime.now(UTC)
    await worker.step()
    while worker._queue:
        await worker.step()
    await worker.step()  # analyse, message et étiquettes
    writes = [(m, p.rsplit("/", 1)[-1], b, q) for m, p, b, q in supabase if m != "GET"]
    created = {b["name"]: 20 + i + 1 for i, (m, p, b, q) in enumerate(supabase) if p == "/rest/v1/tags" and m == "POST"}
    assert set(created) == {"💎 1000+", "💎 500+"}  # « 💎 100+ » existait déjà
    assert [(m, t) for m, t, _, _ in writes if t == "user_card_tags"] == [("DELETE", "user_card_tags")] * 2 + [
        ("POST", "user_card_tags")] * 2
    assert [q for m, _, _, q in writes if m == "DELETE"] == [{"user_card_id": "eq.o2", "tag_id": "eq.9"},
                                                             {"user_card_id": "eq.o3", "tag_id": "eq.9"}]
    assert [b for m, t, b, _ in writes if m == "POST" and t == "user_card_tags"] == [
        {"user_card_id": "o1", "tag_id": created["💎 1000+"]}, {"user_card_id": "o2", "tag_id": created["💎 500+"]}]
    # L'exemplaire étiqueté n'est jamais vendu ni défaussé ; l'analyse, elle, le voit toujours.
    rows = await ctx.api.load_collection()
    assert await excluded_copies(ctx, rows) == {"o2", "o3"}
    assert await excluded_copies(ctx, rows, value_tags=False) == set()
    await ctx.db.close()


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


@pytest.mark.asyncio
async def test_valuable_cards_are_watched_on_the_market(tmp_path):
    """Une C cotée 10 972 WB sur une seule vente : la surveillance dit si elle passe vraiment sur le marché."""
    collection = [{"id": "o1", "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Volley albanais",
                                                          "rarity": "C"}},
                  {"id": "o2", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Mème", "rarity": "C"}}]
    from tests.test_integration import ME as me
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    def auction(aid, card, rarity="C", seller="22222222-2222-2222-2222-222222222222", end=soon, bid=None):
        return {"id": aid, "card": {"id": card, "wikipedia_title": "Volley albanais", "rarity": rarity},
                "status": "active", "seller_id": seller, "end_at": end, "base_amount": 300, "current_bid": bid,
                "current_bidder_id": None}
    market = [auction("a1", "c1"), auction("a2", "c1", "PC"), auction("a3", "autre"), auction("a4", "c1", seller=me),
              auction("a5", "c1", end=past)]
    searched = []

    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": collection})
        if path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if path.endswith("/sales"):
            card = path.split("/")[-2]
            return httpx.Response(200, json={"summary": {"C": {"average": 10972 if card == "c1" else 240}}})
        if path == "/api/marketplace":
            searched.append(request.url.params.get("q"))
            return httpx.Response(200, json={"auctions": market})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    worker = ValueScan(ctx)
    await worker.setup()
    worker._next_run = datetime.now(UTC)
    await worker.step()
    while worker._queue:
        await worker.step()
    await worker.step()  # analyse : seule la carte à 500 WB ou plus est surveillée
    assert worker._watch == {"c1|C": "Volley albanais"}
    assert (await ctx.db.kv_get("value_scan"))["watch"] == worker._watch
    searched.clear()
    worker._watch_due = datetime.now(UTC)
    assert await worker.step() > 0
    assert searched == ["Volley albanais"]
    rows = await ctx.db.sightings_since(datetime.now(UTC) - timedelta(days=1))
    assert [(r["auction_id"], r["rarity"], r["current_bid"]) for r in rows] == [("a1", "C", None), ("a2", "PC", None)]
    assert worker._watch_due - datetime.now(UTC) > timedelta(minutes=55)
    assert worker.recap()[-1] == "Marché des cartes à 500 WB ou plus (7 j) : 2 vente(s) repérée(s) pour 1/1 carte(s)"

    # Relevée plus tard : la mise est mise à jour, la vente n'est pas comptée deux fois.
    market[0]["current_bid"] = 450
    await worker.watch_market()
    rows = await ctx.db.sightings_since(datetime.now(UTC) - timedelta(days=1))
    assert [(r["auction_id"], r["current_bid"]) for r in rows] == [("a1", 450), ("a2", None)]
    await ctx.db.close()


def test_value_sale_queue_order_and_exclusions():
    from src.workers.inventory_manager import value_sale_price, value_sale_queue
    s = Settings(_env_file=None, value_sell_min=1000, value_sell_exclude=["manifestations du 17 octobre 2026"])
    watch = {"a|C": "Crouchen", "b|C": "Manifestations du 17 octobre 2026", "c|PC": "Sareee", "d|C": "Bondar",
             "e|R": "Saint-Amable", "f|L": "Achetée"}
    values = {"a|C": 23346, "b|C": 10972, "c|PC": 1228, "d|C": 1016, "e|R": 640, "f|L": 5000}
    queue = value_sale_queue(watch, values, {}, s, bought={"f"})
    assert [v.title for v in queue] == ["Crouchen", "Sareee", "Bondar"]  # exclue, sous 1000, achetée : jamais
    # Déjà mises en vente : après celles qui ne l'ont jamais été, la plus ancienne d'abord.
    listed = {"a|C": NOW - timedelta(hours=1), "c|PC": NOW - timedelta(days=2)}
    assert [v.title for v in value_sale_queue(watch, values, listed, s, bought={"f"})] == ["Bondar", "Sareee",
                                                                                          "Crouchen"]
    assert value_sale_price(23346, 0.6, None) == 14008
    assert value_sale_price(1228, 0.6, 900) == 900  # jamais sous la plus haute mise vue sur le marché


@pytest.mark.asyncio
async def test_value_cards_sold_one_at_a_time_on_a_reserved_slot(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    def copy(oid, card, title, rarity="C", **kw):
        return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title, "rarity": rarity}, **kw}
    collection = [copy("o1", "c1", "Crouchen", starred=True), copy("o2", "c1", "Crouchen", is_shiny=True),
                  copy("o3", "c1", "Crouchen", tags=[{"name": "💎 1000+"}]),
                  copy("o4", "c2", "Manifestations du 17 octobre 2026"), copy("o5", "c3", "Sareee", "PC")]
    slots = {"selling": 5}
    posted, auctions = [], {}

    def handler(request):
        path = request.url.path
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": slots["selling"], "maxConcurrentAuctions": 5})
        if path == "/api/my-collection":
            q = request.url.params.get("q", "")
            return httpx.Response(200, json={"collection": [c for c in collection
                                                            if q in c["card"]["wikipedia_title"]]})
        if path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if request.method == "POST" and path == "/api/marketplace":
            posted.append(request.read().decode())
            return httpx.Response(201, json={"auction_id": f"v{len(posted)}"})
        if path.startswith("/api/marketplace/v"):
            return httpx.Response(200, json={"auction": auctions[path.rsplit("/", 1)[-1]]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, value_tag_tiers=[1000],
                                   value_sell_exclude=["Manifestations du 17 octobre 2026"])
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set("value_scan", {"watch": {"c1|C": "Crouchen", "c2|C": "Manifestations du 17 octobre 2026",
                                             "c3|PC": "Sareee"}})
    await db.price_put("c1", {"C": (23346, None)})
    await db.price_put("c2", {"C": (10972, None)})
    await db.price_put("c3", {"PC": (1228, None)})
    now = datetime.now(UTC)
    await db.record_sightings([{"auction_id": "s1", "card_id": "c3", "title": "Sareee", "rarity": "PC",
                                "is_shiny": 0, "base_amount": 300, "current_bid": 900,
                                "end_at": (now + timedelta(hours=1)).isoformat(), "seen_at": now.isoformat()}])
    inv = InventoryManager(ctx)
    await inv.setup()

    # Ventes pleines : la carte attend, et la prochaine place libre lui est gardée.
    assert not await inv.sell_value_card() and inv._value_waiting
    slots["selling"] = 4
    assert await inv.free_slots() == 0  # ni doublon ni revente sur la place gardée
    for i in range(2):  # doublons déjà programmés, plus tôt
        await db.schedule_listing(owned_card_id=f"d{i}", card_id="d", title=f"Doublon {i}", rarity="C",
                                  base_amount=20, duration_minutes=360, post_at=now - timedelta(minutes=5),
                                  target_end=now + timedelta(hours=6), reason="cote 20")
    await inv.post_due_listings()
    assert not posted  # 4/5 : la place libre est celle de la carte de valeur, les doublons sont reportés
    rows = await db._all("SELECT post_at FROM listings WHERE reason = 'cote 20'")
    assert all(datetime.fromisoformat(r["post_at"]) > now + timedelta(hours=1) for r in rows)

    # La plus chère d'abord, sur l'exemplaire étiqueté (ni le favori ni le shiny), à 60 % de sa cote, pour 6 h.
    assert await inv.sell_value_card()
    await inv.post_due_listings()
    assert len(posted) == 1 and '"card_id":"o3"' in posted[0].replace(" ", "")
    row = await db._one("SELECT * FROM listings WHERE reason LIKE 'valeur%'")
    assert (row["status"], row["base_amount"], row["duration_minutes"]) == ("posted", 14008, 60)
    assert ("INFO", "Carte de valeur mise en vente") in notifier.sent
    slots["selling"] = 5

    # Une seule à la fois : rien tant que la première n'est pas réglée, et plus de place gardée.
    assert not await inv.sell_value_card() and not inv._value_waiting

    # Vendue : message, puis la suivante (jamais Manifestations), au prix de la mise vue sur le marché.
    auctions["v1"] = {"id": "v1", "card": {"id": "c1", "wikipedia_title": "Crouchen", "rarity": "C"},
                      "status": "settled_sold", "end_at": now.isoformat(), "base_amount": 14008,
                      "current_bid": 15000, "final_price": 15000}
    await db._exec("UPDATE listings SET target_end = ? WHERE id = ?",
                   ((now - timedelta(minutes=5)).isoformat(), row["id"]))
    await inv.settle_listings()
    assert ("SUCCESS", "Carte de valeur vendue") in notifier.sent
    collection[:] = [c for c in collection if c["id"] != "o3"]
    slots["selling"] = 4
    assert await inv.sell_value_card()
    nxt = await db._one("SELECT * FROM listings WHERE reason LIKE 'valeur%' AND status = 'scheduled'")
    assert (nxt["title"], nxt["base_amount"]) == ("Sareee", 900)
    await db.close()
