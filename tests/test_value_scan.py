"""Cartes de valeur de l'inventaire : derniers exemplaires compris, cotes espacées, signalées une fois."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config import Settings
from src.core.models import OwnedCard
from src.core.pricing import Quote
from src.workers.value_scan import ValueScan, find_valuable, remembered, stale_first, to_report
from tests.test_integration import make_client, make_context

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


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
    reported = {"a|C": 190.0, "b|C": 100.0, "gone|C": 500.0}
    assert [v.title for v in to_report(found, reported)] == ["C", "B"]  # nouvelle ; cote +60 %
    assert remembered(found, reported) == {"a|C": 200.0, "b|C": 160.0, "c|C": 300.0}  # vendue : oubliée


def test_stale_cards_are_quoted_first():
    fetched = {"fresh": NOW - timedelta(days=1), "old": NOW - timedelta(days=20), "older": NOW - timedelta(days=30)}
    order = stale_first(["fresh", "old", "never", "older"], fetched, NOW, timedelta(days=7))
    assert order == ["never", "older", "old"]


@pytest.mark.asyncio
async def test_scan_reports_valuable_cards_once(tmp_path):
    collection = [{"id": "o1", "card_id": "c1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"}},
                  {"id": "o2", "card_id": "c2", "card": {"id": "c2", "wikipedia_title": "Banal", "rarity": "PC"}}]
    summaries = {"c1": {"C": {"average": 240}}, "c2": {"PC": {"average": 8}}}
    end = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    rival = {"id": "a1", "card": {"id": "c1", "wikipedia_title": "Mème", "rarity": "C"}, "status": "active",
             "seller_id": "22222222-2222-2222-2222-222222222222", "end_at": end, "base_amount": 180,
             "current_bid": None, "current_bidder_id": None}
    calls = {"sales": 0, "limited": 1}
    messages = []

    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": collection})
        if path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if path.endswith("/sales"):
            calls["sales"] += 1
            if calls["limited"]:  # 1re cote : route limitée, la cote est redemandée plus tard
                calls["limited"] -= 1
                return httpx.Response(403, headers={"Retry-After": "0"}, json={"error": "Too many requests"})
            return httpx.Response(200, json={"summary": summaries[path.split("/")[-2]]})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [rival]})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, value_scan_lookup_seconds=30,
                                   backoff_rate_limit_base_seconds=60)
    notifier.send = lambda level, title, description="", **kw: messages.append((title, description))
    ctx = await make_context(tmp_path, client, notifier)
    worker = ValueScan(ctx)
    await worker.setup()
    assert await worker.step() > 0 and calls["sales"] == 0  # pas tout de suite après le démarrage
    worker._next_run = datetime.now(UTC)
    await worker.step()  # lecture de la collection
    assert worker._queue == ["c1", "c2"]
    delay = await worker.step()  # 1re cote : limitée, elle attend
    assert worker._queue == ["c1", "c2"] and worker._spacing == 60 and delay == 60
    client._route_pause.clear()
    while worker._queue:
        await worker.step()
    await worker.step()  # analyse et message
    assert calls["sales"] == 3
    assert [t for t, _ in messages] == ["1 carte(s) de valeur dans ton inventaire"]
    assert "Mème" in messages[0][1] and "~240 WB (moyenne du site) · dernier exemplaire" in messages[0][1]
    assert "1 en vente, dès 180 WB" in messages[0][1]
    assert worker.recap()[0].endswith("2/2 cartes cotées, 1 à 100 WB ou plus (1 nouvelle(s))")
    assert worker._next_run - datetime.now(UTC) > timedelta(hours=11)

    # Passage suivant (après un redémarrage) : cotes encore récentes, rien de nouveau à signaler.
    again = ValueScan(ctx)
    await again.setup()
    assert again._next_run == worker._next_run
    again._next_run = datetime.now(UTC)
    await again.step()
    assert again._queue == []
    await again.step()
    assert calls["sales"] == 3 and len(messages) == 1
    await ctx.db.close()


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert (s.enable_value_scan, s.value_scan_hours, s.value_scan_min_value) == (True, 12.0, 100)
