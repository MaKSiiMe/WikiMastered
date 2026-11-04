"""Garde : une sanction lue dans le profil arrête tout le bot, même après un redémarrage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.client import Halted, WikiMastersClient
from src.core.endpoints import Endpoints
from src.workers.profile_guard import ProfileGuard, packs_available, sanctions
from tests.test_integration import ROOT, make_client, make_context
from tests.test_session import cookie_for

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
# Relevé du 16/10 : profil sans sanction.
CLEAN = {"cheat_strikes": 0, "activity_blocked_until": None, "last_sanction_at": None, "last_sanction_type": None,
         "leaderboard_excluded": False, "deleted_at": None, "pity_counter": 0, "packs_remaining": 2,
         "wikibidous_balance": 2199}


def test_only_new_sanctions_count():
    assert sanctions(CLEAN, {}, NOW) == []
    assert sanctions({**CLEAN, "cheat_strikes": 1}, {}, NOW) == ["avertissements de triche (cheat_strikes) : 1"]
    assert sanctions({**CLEAN, "cheat_strikes": 1}, {"cheat_strikes": 1}, NOW) == []  # déjà connu
    later = (NOW + timedelta(hours=2)).isoformat()
    assert sanctions({**CLEAN, "activity_blocked_until": later}, {}, NOW) == ["activité bloquée jusqu'au 16/10 19:00"]
    assert sanctions({**CLEAN, "activity_blocked_until": (NOW - timedelta(hours=1)).isoformat()}, {}, NOW) == []
    sanction = {**CLEAN, "last_sanction_at": NOW.isoformat(), "last_sanction_type": "warn"}
    assert sanctions(sanction, {}, NOW) == ["sanction « warn » le 16/10 17:00"]
    assert sanctions(sanction, {"last_sanction_at": NOW.isoformat()}, NOW) == []
    assert sanctions({**CLEAN, "leaderboard_excluded": True}, {}, NOW) == ["compte exclu du classement"]


@pytest.mark.asyncio
async def test_a_strike_halts_every_request_even_after_a_restart(tmp_path):
    profiles = [CLEAN, {**CLEAN, "cheat_strikes": 1}]

    def handler(request):
        if request.url.path == "/rest/v1/rpc/get_my_profile":
            return httpx.Response(200, json=profiles.pop(0))
        return httpx.Response(200, json={"balance": 42})

    client, notifier = make_client(tmp_path, handler, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    db, api = ctx.db, ctx.api
    guard = ProfileGuard(ctx)
    await guard.setup()

    assert await guard.step() > 60 and client.halted is None
    assert "pity relevé : 0" in guard.recap()[0]
    await guard.step()
    assert client.halted == "avertissements de triche (cheat_strikes) : 1"
    assert ("CRITICAL", "Bot arrêté : sanction du site") in notifier.sent
    with pytest.raises(Halted):
        await api.balance()  # plus aucune requête

    # Redémarrage : data/HALT garde le bot à l'arrêt.
    again = WikiMastersClient(client.settings, Endpoints.load(ROOT / "config" / "endpoints.yaml"), notifier)
    assert again.halted == "avertissements de triche (cheat_strikes) : 1"
    with pytest.raises(Halted):
        await again.supabase("POST", "rpc/get_my_profile", json_body={})
    await db.close()


def test_packs_count_what_regenerated_since_the_last_read():
    """`packs_remaining` reste figé tant que la page des paquets n'est pas ouverte (0 pendant 1 h le 04/11)."""
    profile = {"packs_remaining": 0, "packs_last_regen_at": (NOW - timedelta(minutes=35)).isoformat()}
    assert packs_available(profile, NOW) == (3, 10)
    assert packs_available({**profile, "packs_last_regen_at": (NOW - timedelta(days=1)).isoformat()}, NOW) == (10, 10)
    assert packs_available({**profile, "pack_cap": 20, "pack_regen_ms": 60_000}, NOW) == (20, 20)
    assert packs_available({"packs_remaining": 4}, NOW) == (4, 10)  # sans date : la valeur telle quelle
    assert packs_available({"packs_remaining": 4, "packs_last_regen_at": "2026-10-16T15:00:00"}, NOW) == (4, 10)
    assert packs_available({}, NOW) is None


@pytest.mark.asyncio
async def test_full_packs_are_announced_once_until_they_are_opened(tmp_path):
    stocks = [9, 10, 10, 4, 10]

    def handler(request):
        return httpx.Response(200, json={**CLEAN, "packs_remaining": stocks.pop(0)})

    client, notifier = make_client(tmp_path, handler, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    guard = ProfileGuard(ctx)
    await guard.setup()
    announced = []
    for _ in range(5):
        await guard.step()
        announced.append(sum(title == "Paquets pleins" for _, title in notifier.sent))
    assert announced == [0, 1, 1, 1, 2]

    # Redémarrage alors que le stock est encore plein : pas de second message.
    stocks.append(10)
    again = ProfileGuard(ctx)
    await again.setup()
    await again.step()
    assert sum(title == "Paquets pleins" for _, title in notifier.sent) == 2
    assert client.halted is None
    await client.close()
    await ctx.db.close()


@pytest.mark.asyncio
async def test_packs_alert_can_be_turned_off_or_lowered(tmp_path):
    def handler(request):
        return httpx.Response(200, json={**CLEAN, "packs_remaining": 6})

    for threshold, titles in ((0, []), (5, ["6 paquets à ouvrir"])):
        (tmp_path / str(threshold)).mkdir()
        client, notifier = make_client(tmp_path / str(threshold), handler, supabase_anon_key="anon-key",
                                       wm_cookie=cookie_for("a1", "r1", 3600), packs_alert_at=threshold)
        ctx = await make_context(tmp_path / str(threshold), client, notifier)
        guard = ProfileGuard(ctx)
        await guard.setup()
        await guard.step()
        assert [title for _, title in notifier.sent] == titles
        await client.close()
        await ctx.db.close()
