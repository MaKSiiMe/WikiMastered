"""Outil à usage unique : les lignes affichées, relues comme des variables, doivent redonner les valeurs actuelles."""

from __future__ import annotations

from src.config import Settings
from src.tools.figer_reglages import FIELDS, lines


def test_every_field_is_a_setting():
    assert {name.lower() for name in FIELDS} <= set(Settings.model_fields)


def test_a_variable_already_set_is_left_alone():
    shown = lines({"SNIPER_MIN_CONTESTED_SALES": "0", "HTTP_PEAK_SPACING_SECONDS": "0.4"})
    assert not any(line.startswith(("SNIPER_MIN_CONTESTED_SALES=", "HTTP_PEAK_SPACING_SECONDS=")) for line in shown)
    assert len(shown) == len(FIELDS) - 2


def test_printed_lines_give_back_the_current_values(monkeypatch):
    for line in lines({}):
        name, value = line.split("=", 1)
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    for name in FIELDS:
        field = name.lower()
        assert getattr(settings, field) == Settings.model_fields[field].get_default(call_default_factory=True), name
