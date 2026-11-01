"""Logs : console lisible, journal détaillé en JSON, relevés des passages et des requêtes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import httpx
import pytest

from src.core import journal
from src.core.models import Auction
from src.core.pricing import Quote
from src.core.stats import NetStats, Timings, network_lines, workers_line
from tests.test_integration import make_client
from tests.test_sniper_outcomes import sale, start


@pytest.fixture
def logging_state():
    """setup() remplace les handlers : ceux de pytest sont remis après le test."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level, journal.EVENTS.handlers[:], journal.EVENTS.propagate
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    journal.EVENTS.handlers[:], journal.EVENTS.propagate = saved[2], saved[3]


def test_plain_drops_discord_markup():
    assert journal.plain("**[Albert Camus](https://fr.wikipedia.org/wiki/Albert_Camus)** | `x`") == "Albert Camus | x"


def test_parse_duration():
    assert journal.parse_duration("90m") == timedelta(minutes=90)
    assert journal.parse_duration("6h") == journal.parse_duration("6") == timedelta(hours=6)
    assert journal.parse_duration("2j") == journal.parse_duration("2d") == timedelta(days=2)
    with pytest.raises(ValueError):
        journal.parse_duration("hier")


def test_console_is_short_and_journal_keeps_everything(tmp_path, capsys, logging_state):
    journal.setup("INFO", tmp_path)
    logging.getLogger("worker.sniper").debug("positions pleines, X ignorée")
    logging.getLogger("worker.sniper").info("Enchère placée : " + "x" * 600)
    logging.getLogger("discord.client").warning("PyNaCl is not installed, voice will NOT be supported")
    journal.event("enchere", titre="Brésil", issue="misée")
    for handler in logging.getLogger().handlers:
        handler.flush()

    console = capsys.readouterr().out.splitlines()
    assert len(console) == 1  # ni DEBUG, ni événement, ni PyNaCl
    assert " sniper " in console[0] and len(console[0]) <= journal.CONSOLE_MAX + 40 and console[0].endswith("…")

    entries = [json.loads(line) for line in (tmp_path / journal.JOURNAL_FILE).read_text().splitlines()]
    assert [e.get("msg", e.get("ev"))[:16] for e in entries] == ["positions pleine", "Enchère placée :", "enchere"]
    assert entries[0]["lvl"] == "DEBUG" and entries[0]["src"] == "sniper"
    assert entries[1]["msg"].endswith("x" * 600)  # entier dans le journal
    assert entries[2]["data"] == {"titre": "Brésil", "issue": "misée"}


def test_read_filters_by_time_and_event(tmp_path):
    def line(at: datetime, **fields) -> str:
        return json.dumps({"t": at.isoformat(timespec="milliseconds"), "lvl": "INFO", "src": "x", **fields})

    now = datetime(2026, 11, 1, 12, 0)
    (tmp_path / "journal.jsonl.2026-10-30").write_text(line(now - timedelta(days=2), msg="vieux") + "\n")
    (tmp_path / "journal.jsonl.2026-10-31").write_text(line(now - timedelta(hours=20), msg="hier") + "\n")
    (tmp_path / journal.JOURNAL_FILE).write_text("\n".join([
        line(now - timedelta(hours=3), msg="avant"),
        line(now - timedelta(hours=1), ev="stats", data={}),
        line(now - timedelta(minutes=5), msg="après"),
        line(now - timedelta(minutes=1), ev="enchere", data={}),
    ]) + "\n")

    def read(since, *args, **kwargs):
        return [json.loads(x).get("msg") or json.loads(x)["ev"] for x in journal.read(tmp_path, since, *args, **kwargs)]

    assert read(now - timedelta(hours=2)) == ["stats", "après", "enchere"]
    assert read(now - timedelta(days=1)) == ["hier", "avant", "stats", "après", "enchere"]
    assert read(now - timedelta(hours=2), {"enchere"}) == ["enchere"]
    assert read(now - timedelta(hours=2), events_only=True) == ["stats", "enchere"]


def test_timings_and_summary_lines():
    t = Timings()
    for v in [0.1] * 18 + [2.0, 9.0]:
        t.add(v)
    s = t.summary()
    assert s["n"] == 20 and s["p50"] == 0.1 and s["p95"] == 2.0 and s["max"] == 9.0
    assert Timings().summary() == {"n": 0}

    line = workers_line({"sniper": {**s, "erreurs": 2}, "garde": {"n": 1, "moy": 0.4, "max": 0.4, "erreurs": 0},
                         "cotes": {"n": 0, "erreurs": 0, "pause": True}})
    assert line.startswith("sniper 20 passages, moy 0.6 s, p95 2.0 s, max 9.0 s · garde 1 passage(s), 0.4 s")
    assert "cotes en pause" in line and line.endswith("erreurs : sniper 2")

    net = NetStats()
    for _ in range(3):
        net.record("marketplace", "sniper", 0.2, 0.15, "200")
    net.record("sales_summary", "valeur", 4.0, 0.3, "429")
    net.record("place_bid", "écritures", 0.0, 0.5, "ReadTimeout")
    lines = network_lines(net.take(), 60)
    assert lines[0] == "5 requêtes (0.08/s) — échecs : sales_summary 429 ×1, place_bid ReadTimeout ×1"
    assert lines[1].startswith("attente d'un créneau : sniper moy 0.2 s")
    assert lines[2].startswith("routes : marketplace 3 (150 ms, p95 150 ms)")
    assert net.take() == {"routes": {}, "attente": {}}


@pytest.mark.asyncio
async def test_client_counts_each_request_by_route(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"balance": 42})

    client, _ = make_client(tmp_path, handler, backoff_rate_limit_base_seconds=0.01)
    await client.get("balance")
    routes = client.net.take()["routes"]
    assert routes["balance"]["n"] == 2 and routes["balance"]["codes"] == {"429": 1, "200": 1}
    await client.close()


@pytest.mark.asyncio
async def test_sniper_logs_each_new_outcome_once_with_its_estimate(tmp_path, caplog):
    sniper, db, _ = await start(tmp_path, {}, [])
    auction = Auction.model_validate(sale("a1", "Brésil", 30, base=900))
    caplog.set_level(logging.INFO, logger="evenement")
    quote = Quote(1119.4, 5, 3)
    sniper._outcome(auction, "bénéfice attendu trop faible", quote)
    sniper._outcome(auction, "bénéfice attendu trop faible", quote)
    sniper._outcome(auction, "misée", mise=900, plafond=950, estimation=1119.4)
    sniper._outcome(auction, "trop tard")  # une mise placée n'est plus remplacée
    events = [r.data for r in caplog.records if r.name == "evenement"]
    assert [e["issue"] for e in events] == ["bénéfice attendu trop faible", "misée"]
    assert events[0]["estimation"] == 1119 and events[0]["ventes"] == 5 and events[0]["titre"] == "Brésil"
    assert events[1]["plafond"] == 950
    await db.close()


@pytest.mark.asyncio
async def test_sniper_time_by_step_is_reported_then_reset(tmp_path):
    sniper, db, _ = await start(tmp_path, {}, [])

    async def step() -> float:
        started = sniper._phases[0][1]
        sniper._phases = [("début", started - timedelta(seconds=3)), ("relecture Brésil", started - timedelta(seconds=2)),
                          ("mises (2 vente(s))", started)]
        return 1.0

    sniper._step = step
    await sniper.step()
    phases = sniper.stats()["etapes"]
    assert set(phases) == {"début", "relecture", "mises"} and phases["relecture"] == 2.0
    assert sniper.stats() == {}
    await db.close()
