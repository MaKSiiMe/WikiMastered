"""Succès : recalcul, lecture des succès débloqués non réclamés, réclamation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.workers.achievements import AchievementsWorker
from src.workers.base import Context
from tests.test_integration import make_client
from tests.test_session import cookie_for


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_unlocked_achievements_are_claimed(tmp_path, monkeypatch, dry_run):
    posts = []

    def handler(request):
        if request.url.host.endswith("supabase.co"):
            table = request.url.path.rsplit("/", 1)[1]
            if table == "user_achievements":
                assert request.url.params["claimed_at"] == "is.null"
                assert request.url.params["unlocked_at"] == "not.is.null"
                return httpx.Response(200, json=[{"achievement_id": "s1"}, {"achievement_id": "s2"}])
            assert request.url.params["id"] == "in.(s1,s2)"
            return httpx.Response(200, json=[{"id": "s1", "title": "Vitrine pleine", "wikibidous_reward": 25},
                                             {"id": "s2", "title": "Première enchère", "wikibidous_reward": 50}])
        body = json.loads(request.content)
        posts.append((request.url.path, body))
        if request.url.path == "/api/achievements/check":
            return httpx.Response(200, json={})
        if body["achievement_id"] == "s1":
            return httpx.Response(200, json={"claimed_at": "2026-10-15T08:00:00Z", "amount": 25})
        return httpx.Response(200, json={"already_claimed": True})

    client, notifier = make_client(tmp_path, handler, dry_run=dry_run, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    worker = AchievementsWorker(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    monkeypatch.setattr("src.workers.achievements.asyncio.sleep", lambda _s: _noop())
    await worker.setup()
    assert await worker.step() > 0 and posts == []  # pas avant ACHIEVEMENTS_TIME
    worker._next_run = datetime.now(UTC)
    await worker.step()
    assert timedelta(0) < worker._next_run - datetime.now(UTC) <= timedelta(days=1, minutes=5)
    if dry_run:
        assert posts == [] and worker.recap() == ["Aucun succès à réclamer"]
    else:
        assert posts == [("/api/achievements/check", {"event": "achievements_sync"}),
                         ("/api/achievements/claim", {"achievement_id": "s1"}),
                         ("/api/achievements/claim", {"achievement_id": "s2"})]
        assert ("SUCCESS", "Succès réclamés") in notifier.sent
        assert worker.recap() == ["1 succès réclamé(s), +25 WB"]
    await db.close()


async def _noop():
    return None
