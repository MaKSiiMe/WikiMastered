"""Logs du bot : une console lisible, et un journal détaillé pour les analyses.

Console (logs Portainer, `ssh wm-logs 2h`) : une ligne courte par événement utile, au niveau LOG_LEVEL, avec un
nom de source court (sniper, reseau, discord…). Journal (`data/logs/journal.jsonl`, un fichier par jour,
JOURNAL_KEEP_DAYS gardés) : tous les logs, DEBUG compris, et les événements structurés (`event`) — décisions du
sniper, statistiques des workers et du réseau, messages Discord —, une ligne JSON chacun. Il se relit avec
`python -m src.main journal --since 6h`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

JOURNAL_FILE = "journal.jsonl"
CONSOLE_MAX = 500  # au-delà, la ligne de console est coupée (le journal garde tout)

# Événements structurés : dans le journal seulement, jamais en console.
EVENTS = logging.getLogger("evenement")

# Nom de logger -> source affichée en console et dans le journal.
SHORT_NAMES = {
    "wikimastered": "bot",
    "wikimastered.discord": "discord",
    "src.core.notifier": "discord",
    "src.core.client": "reseau",
    "src.core.api": "api",
    "src.core.models": "modeles",
    "src.core.pricing": "cotes",
    "src.core.database": "base",
    "src.core.wikidata": "wikidata",
    "src.recap": "recap",
    "src.backup": "sauvegarde",
    "src.workers.collection": "collection",
    "src.workers.themes": "etiquettes",
}

# Le bot Discord n'a pas de voix : cet avertissement revient à chaque démarrage pour rien.
_IGNORED = ("PyNaCl is not installed",)


def short_name(name: str) -> str:
    if name in SHORT_NAMES:
        return SHORT_NAMES[name]
    return name.removeprefix("worker.").removeprefix("src.").rsplit(".", 1)[-1]


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def plain(text: str) -> str:
    """Texte d'un message Discord sans sa mise en forme : `**[Titre](url)**` devient `Titre`."""
    return _LINK.sub(r"\1", text).replace("**", "").replace("`", "")


def event(kind: str, **data: Any) -> None:
    """Événement structuré (décision, statistiques…) : une ligne du journal, avec ses données."""
    EVENTS.info(kind, extra={"data": data})


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(source)-10s %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.source = short_name(record.name)
        line = super().format(record)
        head, sep, rest = line.partition("\n")  # la trace d'une exception reste entière
        if len(head) > CONSOLE_MAX:
            head = head[:CONSOLE_MAX - 1] + "…"
        return head + sep + rest


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "t": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "src": short_name(record.name),
        }
        data = getattr(record, "data", None)
        if record.name == EVENTS.name:
            entry["ev"] = record.getMessage()
        else:
            entry["msg"] = record.getMessage()
        if data:
            entry["data"] = data
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class _DropNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(text in str(record.msg) for text in _IGNORED)


def setup(level: str, logs_dir: Path | None = None, keep_days: int = 7) -> None:
    """Console au niveau `level` ; avec `logs_dir`, journal détaillé (DEBUG compris) dans ce dossier."""
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter())
    console.setLevel(level.upper())
    console.addFilter(_DropNoise())
    handlers: list[logging.Handler] = [console]
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        journal = TimedRotatingFileHandler(logs_dir / JOURNAL_FILE, when="midnight", backupCount=max(1, keep_days),
                                           encoding="utf-8")
        journal.setFormatter(JsonFormatter())
        journal.setLevel(logging.DEBUG)
        journal.addFilter(_DropNoise())
        handlers.append(journal)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(logging.DEBUG if logs_dir is not None else level.upper())
    EVENTS.handlers[:] = handlers[1:]
    EVENTS.propagate = False
    for noisy in ("httpx", "httpcore", "hpack", "aiosqlite", "discord", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_DURATION = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([mhj]|min|d)?\s*$")


def parse_duration(text: str) -> timedelta:
    """« 90m », « 6h », « 2j » (ou « 2d ») ; un nombre seul compte en heures."""
    match = _DURATION.match(text.lower())
    if not match:
        raise ValueError(f"durée illisible : {text!r} (exemples : 90m, 6h, 2j)")
    value = float(match[1].replace(",", "."))
    unit = match[2] or "h"
    return timedelta(minutes=value) if unit in ("m", "min") else \
        timedelta(days=value) if unit in ("j", "d") else timedelta(hours=value)


def read(logs_dir: Path, since: datetime, kinds: set[str] | None = None, *, events_only: bool = False
         ) -> Iterator[str]:
    """Lignes du journal depuis `since` (heure locale, comme le journal), des plus anciennes aux plus récentes.
    `kinds` : seulement ces événements (et aucun log) ; `events_only` : tous les événements, aucun log."""
    files = sorted(logs_dir.glob(JOURNAL_FILE + ".*")) + [logs_dir / JOURNAL_FILE]
    start = since.isoformat(timespec="milliseconds")
    for path in files:
        if not path.exists():
            continue
        if path.name != JOURNAL_FILE and path.name.rsplit(".", 1)[-1] < f"{since:%Y-%m-%d}":
            continue  # fichier d'un jour entièrement avant `since`
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line[7:30] < start:  # {"t": "2026-11-01T01:18:37.123" : comparaison sans décoder le JSON
                    continue
                if kinds is not None or events_only:
                    try:
                        ev = json.loads(line).get("ev")
                    except ValueError:
                        continue
                    if ev is None or (kinds is not None and ev not in kinds):
                        continue
                yield line.rstrip("\n")
