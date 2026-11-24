"""Prix partagés (core/shared_prices.py) : le serveur commun passe avant le site, reçoit nos lectures, et ne gêne
jamais le bot s'il est en panne."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.shared_prices import SharedPrices
from tests.test_integration import make_client

SITE = {"computed_at": "2026-11-24T05:00:00Z", "by_rarity": {"SR": {"weekly": [
    {"start": "2026-11-23", "avg": 1200, "min": 1000, "max": 1400, "count": 3}]}}}
SHARED = {"computed_at": "2026-11-24T04:00:00Z", "by_rarity": {"UR": {"weekly": []}}}


def site_handler(seen: list[str]):
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=SITE)
    return handler


def shared_server(calls: list[httpx.Request], items: dict | None = None, status: int = 200):
    def handler(request):
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        if request.method == "POST":
            return httpx.Response(200, json={"who": "bot", "stored": len(json.loads(request.content)["items"])})
        return httpx.Response(200, json={"who": "bot", "items": items or {}})
    return SharedPrices("https://share.example.workers.dev/", "s3cr3t", timedelta(minutes=180),
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_a_fresh_shared_reading_saves_the_site_request(tmp_path):
    site, calls = [], []
    client, _ = make_client(tmp_path, site_handler(site))
    shared = shared_server(calls, {"c1": {"read_at": 1, "read_by": "pote-ext", "history": SHARED}})
    assert await WikiMastersAPI(client, shared).price_history("c1") == SHARED
    assert site == []
    assert calls[0].url.params["ids"] == "c1" and calls[0].url.params["max_age"] == "10800"
    assert calls[0].headers["authorization"] == "Bearer s3cr3t"
    assert shared.served == 1
    await shared.close()


@pytest.mark.asyncio
async def test_a_site_reading_is_deposited_on_the_shared_server(tmp_path):
    site, calls = [], []
    client, _ = make_client(tmp_path, site_handler(site))
    shared = shared_server(calls)
    assert await WikiMastersAPI(client, shared).price_history("c2") == SITE
    assert site == ["/api/marketplace/cards/c2/price-history"]
    await shared.close()  # dépose ce qui attend
    posted = json.loads(calls[-1].content)["items"]
    assert calls[-1].method == "POST" and len(posted) == 1
    assert posted[0]["id"] == "c2" and posted[0]["history"] == SITE and posted[0]["read_at"] > 0
    assert shared.missed == 1 and shared.stored == 1


@pytest.mark.asyncio
async def test_a_broken_shared_server_is_skipped_for_a_while(tmp_path):
    site, calls = [], []
    client, _ = make_client(tmp_path, site_handler(site))
    shared = shared_server(calls, status=500)
    api = WikiMastersAPI(client, shared)
    assert await api.price_history("c3") == SITE
    assert await api.price_history("c4") == SITE
    assert len(site) == 2
    assert len(calls) == 1  # une seule tentative : le serveur est ignoré 5 min
    await shared.close()  # dépôt pas tenté non plus
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_without_settings_nothing_is_sent(tmp_path):
    site = []
    client, _ = make_client(tmp_path, site_handler(site))
    shared = SharedPrices("", "", timedelta(minutes=180))
    assert await WikiMastersAPI(client, shared).price_history("c5") == SITE
    assert not shared.enabled and shared.missed == 0
    await shared.close()
