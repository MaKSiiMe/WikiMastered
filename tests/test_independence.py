"""Indépendance des workers : une panne d'un worker (ou de sa route) ne touche jamais les autres."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src import main
from src.core.api import WikiMastersAPI
from src.core.client import RateLimited, UncertainMutation
from src.core.database import Database
from src.core.pricing import PriceService
from src.workers.base import Context, Worker
from tests.test_integration import make_client


@pytest.mark.asyncio
async def test_broken_pack_route_does_not_pause_other_routes(tmp_path):
    """L'ouverture de paquets renvoie 500 : seule sa route attend, le sniper (lectures, mises) continue."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/packs/open":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"balance": 3})

    client, _ = make_client(tmp_path, handler, dry_run=False, backoff_base_seconds=60)
    api = WikiMastersAPI(client)
    with pytest.raises(UncertainMutation):
        await api.open_pack()
    assert client._failures == 0 and client.route_paused_for("open_pack") > 50
    assert await asyncio.wait_for(api.balance(), timeout=1) == 3  # pas de pause générale
    calls.clear()
    with pytest.raises(RateLimited):
        await api.open_pack()  # route en pause : rien n'est envoyé
    assert calls == []


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
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    worker = Flaky(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
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
