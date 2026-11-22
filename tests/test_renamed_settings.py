"""Réglages renommés le 22/11/2026 : chaque ancien nom reste lu, avec la même valeur, jusqu'au retrait des alias."""

from __future__ import annotations

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
