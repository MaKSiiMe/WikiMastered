"""Réglages renommés le 22/11/2026 : chaque ancien nom reste lu, avec la même valeur, jusqu'au retrait des alias."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import RENAMED, Settings

CANDIDATES = ("2", "3", "7", "1.5", "0.3", "true", "false", "03:00", "C", "1,2", "unique", "Vitrine")


def value_for(field: str, monkeypatch) -> str:
    """Une valeur que le nouveau nom accepte et qui change le réglage (sinon le test ne prouverait rien)."""
    default = getattr(Settings(_env_file=None), field)
    for raw in CANDIDATES:
        monkeypatch.setenv(field.upper(), raw)
        try:
            value = getattr(Settings(_env_file=None), field)
        except ValidationError:
            continue
        finally:
            monkeypatch.delenv(field.upper())
        if value != default:
            return raw
    raise AssertionError(f"aucune valeur de test pour {field}")


def test_every_old_name_still_gives_the_same_value(monkeypatch):
    assert len(RENAMED) == 114 and len(set(RENAMED.values())) == 114
    for old, new in RENAMED.items():
        assert new in Settings.model_fields and old.lower() not in Settings.model_fields, old
        raw = value_for(new, monkeypatch)
        monkeypatch.setenv(new.upper(), raw)
        expected = getattr(Settings(_env_file=None), new)
        monkeypatch.delenv(new.upper())
        monkeypatch.setenv(old, raw)
        s = Settings(_env_file=None)
        monkeypatch.delenv(old)
        assert getattr(s, new) == expected, f"{old} -> {new.upper()}"
        assert s.legacy_names == [(old, new.upper())]


def test_new_name_wins_and_nothing_is_reported_without_old_names(monkeypatch):
    monkeypatch.setenv("SNIPER_MAX_BID", "700")
    monkeypatch.setenv("PORTFOLIO_SLOTS", "3")
    monkeypatch.setenv("RESALE_SLOTS", "4")
    s = Settings(_env_file=None)
    assert s.resale_slots == 4 and s.legacy_names == [("PORTFOLIO_SLOTS", "RESALE_SLOTS")]
    monkeypatch.delenv("PORTFOLIO_SLOTS")
    assert Settings(_env_file=None).legacy_names == []


def test_old_names_are_read_from_the_env_file_too(tmp_path, monkeypatch):
    for name in ("AUTO_LIST", "DUPLICATES_SELL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("AUTO_LIST=true\nPROTECTED_TAGS=Jour,GIF\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.duplicates_sell is True and s.tag_protected == ["Jour", "GIF"]
    assert dict(s.legacy_names) == {"AUTO_LIST": "DUPLICATES_SELL", "PROTECTED_TAGS": "TAG_PROTECTED"}


@pytest.mark.asyncio
async def test_settings_changed_on_discord_keep_their_value_under_the_new_name(tmp_path):
    import httpx

    from src import overrides
    from tests.test_integration import make_client, make_context

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404))
    ctx = await make_context(tmp_path, client, notifier)
    await ctx.db.kv_set(overrides.KV_KEY, {"auto_list": True, "auto_discard": False, "collection_max_price": 40})
    assert await overrides.load(ctx) == {"duplicates_sell": True, "discard_enabled": False, "collection_max_price": 40}
    assert ctx.settings.duplicates_sell is True and ctx.settings.discard_enabled is False
    assert await ctx.db.kv_get(overrides.KV_KEY) == {"duplicates_sell": True, "discard_enabled": False,
                                                     "collection_max_price": 40}
    await ctx.db.close()


def test_old_worker_and_group_names_in_values_are_replaced_and_reported(monkeypatch):
    monkeypatch.setenv("SIMULATE", "sniper,souhaits")
    monkeypatch.setenv("LIVE_WRITES", "ventes")
    monkeypatch.setenv("HTTP_WORKER_SPACING_SECONDS", "cotes=5,themes=3")
    s = Settings(_env_file=None)
    assert s.simulate == ["sniper", "wishes"] and s.live_writes == ["resale"]
    assert s.http_worker_spacing_seconds == {"value_scan": 5.0, "themes": 3.0}
    assert s.legacy_names == [("LIVE_WRITES : ventes", "resale"), ("SIMULATE : souhaits", "wishes"),
                              ("HTTP_WORKER_SPACING_SECONDS : cotes", "value_scan")]


@pytest.mark.asyncio
async def test_pauses_saved_under_an_old_worker_name_are_kept(tmp_path):
    import httpx

    from src import pauses
    from tests.test_integration import make_client, make_context

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404))
    ctx = await make_context(tmp_path, client, notifier)
    await ctx.db.kv_set(pauses.KV_KEY, {"cotes": None, "sniper": None})  # défausse coupée : worker des cotes en pause
    await pauses.load(ctx)
    assert set(ctx.pauses) == {"value_scan", "sniper"}
    assert await ctx.db.kv_get(pauses.KV_KEY) == {"value_scan": None, "sniper": None}
    await ctx.db.close()


def test_every_worker_has_an_english_name():
    names = {m for path in Path("src/workers").glob("*.py")
             for m in re.findall(r'^    name = "([a-z_]+)"', path.read_text(encoding="utf-8"), re.MULTILINE)} - {"worker"}
    assert names == {"sniper", "inventory", "collection", "themes", "achievements", "wishes", "trades", "value_scan",
                     "account_value", "guard"}


def test_unused_and_default_variables_are_reported(tmp_path, monkeypatch):
    """22/11 : « plein de variables ne servent plus à rien ». Le bot ignorait sans rien dire une variable qu'il ne lit
    pas (réglage retiré, faute de frappe) ; les variables du système et de l'image ne sont jamais signalées."""
    env = tmp_path / ".env"
    env.write_text("VALUE_SELL_ENABLED=true\nRESALE_SLOTS=4\n", encoding="utf-8")
    monkeypatch.setenv("SNIPER_MAX_BIDD", "700")  # faute de frappe
    monkeypatch.setenv("PORTFOLIO_SLOTS", "3")  # ancien nom : signalé à part (legacy_names)
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv("GPG_KEY", "x")
    s = Settings(_env_file=env)
    assert {"SNIPER_MAX_BIDD", "VALUE_SELL_ENABLED"} <= set(s.unused_names)
    assert not {"PORTFOLIO_SLOTS", "RESALE_SLOTS", "PYTHONUNBUFFERED", "GPG_KEY", "PATH"} & set(s.unused_names)

    default = Settings.model_fields["resale_slots"].default
    monkeypatch.setenv("RESALE_SLOTS", str(default))
    monkeypatch.setenv("LIVE_WRITES", "")
    s = Settings(_env_file=None)
    assert {"RESALE_SLOTS", "LIVE_WRITES"} <= set(s.default_names) and "PORTFOLIO_SLOTS" not in s.default_names
