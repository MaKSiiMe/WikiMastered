"""Indépendance des workers : une panne d'un worker (ou de sa route) ne touche jamais les autres."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from src import main
from src.core.api import WikiMastersAPI
from src.core.client import RateLimited, UncertainMutation
from src.core.timeutils import PARIS
from src.workers.base import Worker
from tests.test_integration import make_client, make_context


@pytest.mark.asyncio
async def test_broken_write_route_does_not_pause_other_routes(tmp_path):
    """La vérification des succès renvoie 500 : seule sa route attend, le sniper (lectures, mises) continue."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/achievements/check":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"balance": 3})

    client, _ = make_client(tmp_path, handler, dry_run=False, backoff_base_seconds=60)
    api = WikiMastersAPI(client)
    with pytest.raises(UncertainMutation):
        await api.achievements_check()
    assert client._failures == 0 and client.route_paused_for("achievements_check") > 50
    assert await asyncio.wait_for(api.balance(), timeout=1) == 3  # pas de pause générale
    calls.clear()
    with pytest.raises(RateLimited):
        await api.achievements_check()  # route en pause : rien n'est envoyé
    assert calls == []


@pytest.mark.asyncio
async def test_sniper_bid_jumps_the_request_queue(tmp_path):
    """Le 15/10 au soir, la relecture et la mise du sniper attendaient derrière les requêtes des autres workers
    et partaient à 0-8 s de la fin : une requête urgente prend le prochain créneau, l'espacement est gardé."""
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"balance": 3})

    client, _ = make_client(tmp_path, handler, http_min_spacing_seconds=0.05)
    background = [asyncio.create_task(client.get("balance")) for _ in range(4)]
    await asyncio.sleep(0.01)  # la 1re est partie, les 3 autres attendent leur créneau
    await client.get("auction_detail", urgent=True, auction_id="a1")
    await asyncio.gather(*background)
    assert sent.index("/api/marketplace/a1") == 1  # juste après la requête déjà partie
    assert sent.count("/api/wikibidous") == 4 and client._no_urgent.is_set()


class Flaky(Worker):
    name = "flaky"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.setups = self.steps = 0

    async def setup(self):
        self.setups += 1
        if self.setups == 1:
            raise httpx.ConnectError("réseau absent au démarrage")

    async def step(self):
        self.steps += 1
        return 0.01


@pytest.mark.asyncio
async def test_worker_setup_failure_is_retried(tmp_path, monkeypatch):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    worker = Flaky(ctx)
    monkeypatch.setattr(worker, "_on_error", lambda exc: 0.01)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    task.cancel()
    assert worker.setups == 2 and worker.steps > 0  # réessayé dans la boucle, puis le worker tourne
    await db.close()


@pytest.mark.asyncio
async def test_crashed_task_is_restarted_while_others_keep_running(monkeypatch):
    monkeypatch.setattr(main, "SUPERVISE_BASE_SECONDS", 0.01)
    alerts, runs, ticks = [], {"n": 0}, {"n": 0}

    class Notifier:
        def critical(self, title, description=""):
            alerts.append(title)

    async def crashing():
        runs["n"] += 1
        raise RuntimeError("bogue")

    async def healthy():
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(main.supervise("packs", crashing, Notifier())),
             asyncio.create_task(main.supervise("sniper", healthy, Notifier()))]
    await asyncio.sleep(0.3)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert runs["n"] >= 3 and ticks["n"] > 10  # packs relancé, le sniper n'a jamais été interrompu
    assert alerts[0] == "packs : tâche arrêtée, relancée" and len(alerts) <= 2


def test_peak_spacing_falls_back_after_site_limits(tmp_path):
    """Pic de ventes : une requête par seconde ; le site limite le bot, retour à 1,5 s pendant le délai réglé."""
    now = datetime.now(PARIS)
    around = {"http_peak_start": (now - timedelta(hours=1)).time(), "http_peak_end": (now + timedelta(hours=1)).time()}
    client, _ = make_client(tmp_path, lambda r: httpx.Response(200, json={}), http_min_spacing_seconds=1.5,
                            http_peak_spacing_seconds=1.0, **around)
    assert client.spacing() == 1.0
    client._route_backoff("sales_summary", 0)  # refus isolé des cotes : le pic continue
    assert client.spacing() == 1.0
    client._route_backoff("sales_summary", 0)
    assert client.spacing() == 1.5
    client._peak_off_until = float("-inf")
    client._backoff("market_page: ReadTimeout", limited=False)
    assert client.spacing() == 1.5
    later = {"http_peak_start": (now + timedelta(hours=1)).time(), "http_peak_end": (now + timedelta(hours=2)).time()}
    off, _ = make_client(tmp_path, lambda r: httpx.Response(200, json={}), http_min_spacing_seconds=1.5,
                         http_peak_spacing_seconds=1.0, **later)
    assert off.spacing() == 1.5  # hors du pic
