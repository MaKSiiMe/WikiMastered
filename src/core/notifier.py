"""Notifications Discord par webhook.

Les messages passent par une file : un worker ne bloque jamais sur Discord, et la
limitation de Discord (429 + retry_after) est respectée. Sans webhook configuré,
les messages ne sont que loggés.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

log = logging.getLogger(__name__)


class Level(Enum):
    INFO = 0x3B82F6
    SUCCESS = 0x22C55E
    WARNING = 0xF59E0B
    CRITICAL = 0xEF4444


@dataclass
class _Message:
    level: Level
    title: str
    description: str
    fields: dict[str, Any] = field(default_factory=dict)


class DiscordNotifier:
    def __init__(self, webhook_url: str, alert_webhook_url: str = "", mention: str = "", dry_run: bool = True):
        self._webhook = webhook_url
        self._alert_webhook = alert_webhook_url or webhook_url
        self._mention = mention
        self._dry_run = dry_run
        self._queue: asyncio.Queue[_Message] = asyncio.Queue(maxsize=500)
        self._http = httpx.AsyncClient(timeout=15)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="discord-notifier")

    async def close(self) -> None:
        if self._task:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=10)
            except asyncio.TimeoutError:
                log.warning("Discord : %d message(s) non envoyés à l'arrêt", self._queue.qsize())
            self._task.cancel()
        await self._http.aclose()

    def send(self, level: Level, title: str, description: str = "", **fields: Any) -> None:
        log_fn = log.warning if level in (Level.WARNING, Level.CRITICAL) else log.info
        log_fn("[%s] %s %s", level.name, title, description.replace("\n", " | "))
        if not self._webhook:
            return
        try:
            self._queue.put_nowait(_Message(level, title, description, fields))
        except asyncio.QueueFull:
            log.error("File Discord pleine, message perdu : %s", title)

    def info(self, title: str, description: str = "", **fields: Any) -> None:
        self.send(Level.INFO, title, description, **fields)

    def success(self, title: str, description: str = "", **fields: Any) -> None:
        self.send(Level.SUCCESS, title, description, **fields)

    def warning(self, title: str, description: str = "", **fields: Any) -> None:
        self.send(Level.WARNING, title, description, **fields)

    def critical(self, title: str, description: str = "", **fields: Any) -> None:
        self.send(Level.CRITICAL, title, description, **fields)

    async def _run(self) -> None:
        while True:
            msg = await self._queue.get()
            try:
                await self._deliver(msg)
            except Exception:
                log.exception("Échec d'envoi Discord : %s", msg.title)
            finally:
                self._queue.task_done()

    async def _deliver(self, msg: _Message) -> None:
        critical = msg.level is Level.CRITICAL
        url = self._alert_webhook if critical else self._webhook
        title = f"[DRY RUN] {msg.title}" if self._dry_run else msg.title
        embed = {
            "title": title[:256],
            "description": msg.description[:4000],
            "color": msg.level.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fields": [
                {"name": str(k)[:256], "value": str(v)[:1024] or "-", "inline": True}
                for k, v in list(msg.fields.items())[:25]
            ],
        }
        payload: dict[str, Any] = {"username": "WikiMastered", "embeds": [embed]}
        if critical and self._mention:
            payload["content"] = self._mention
        for _ in range(5):
            resp = await self._http.post(url, json=payload)
            if resp.status_code == 429:
                retry = float((resp.json() or {}).get("retry_after", 2))
                await asyncio.sleep(min(retry, 60))
                continue
            if resp.status_code >= 400:
                log.error("Discord HTTP %s : %s", resp.status_code, resp.text[:200])
            return
