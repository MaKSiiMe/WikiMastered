"""Notifications Discord par webhook.

Les messages passent par une file : un worker ne bloque jamais sur Discord, et la
limitation de Discord (429 + retry_after) est respectée. Sans webhook configuré,
les messages ne sont que loggés.

Chaque message porte la catégorie de ce qui l'envoie (sniper, ventes, paquets…), affichée
au-dessus du titre pour repérer d'un coup d'œil d'où il vient.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from src.core.timeutils import utcnow

log = logging.getLogger(__name__)


class Topic(Enum):
    """Catégorie d'un message ; la valeur est l'étiquette affichée au-dessus du titre."""

    SYSTEM = "⚙️ Système"        # démarrage, session, serveurs
    SNIPER = "🎯 Sniper"          # enchères, positions achetées
    COLLECTION = "📅 Collection"  # cartes de collection obtenues, étiquettes
    SALES = "💰 Ventes"           # mises en vente, reventes, défausses
    PACKS = "🎴 Paquets"          # ouvertures de boosters
    TRENDS = "📈 Tendances"       # signaux Wikipédia et Google Trends
    ACHIEVEMENTS = "🏆 Succès"    # récompenses de succès réclamées
    REPORT = "📊 Bilan"           # bilan du soir
    RECAP = "🩺 Récap"            # récap périodique des workers


# Limites des embeds Discord
_TOTAL_MAX = 5800  # 6000 au plus pour l'ensemble d'un embed, avec de la marge
_FIELD_MAX = 1024


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
    live: bool = False  # action réelle malgré DRY_RUN (ex. paquets via LIVE_WRITES) : pas de préfixe
    topic: Topic = Topic.SYSTEM
    sections: list[tuple[str, str]] = field(default_factory=list)  # blocs pleine largeur, dans l'ordre
    thumbnail: str | None = None
    url: str | None = None
    important: bool | None = None  # True : salon principal ; False : activité ; None : selon le niveau

    @property
    def main_channel(self) -> bool:
        """Salon principal (le seul qui sonne) : ce qui touche à ton argent (achats, mises en vente, ventes,
        marqués `important`), les alertes (avertissements, critiques) et le bilan. Tout le reste, réussites
        comprises (cartes de collection gagnées, tirages, succès), va dans le salon d'activité, consulté de temps
        en temps. Un avertissement qui se règle seul est marqué `important=False`."""
        if self.important is not None:
            return self.important
        if self.topic is Topic.REPORT:
            return True
        return self.level in (Level.WARNING, Level.CRITICAL) and self.topic is not Topic.RECAP


# DISCORD_ACTIVITY_WEBHOOK_URL=off : les messages d'activité ne vont que dans les logs.
_OFF = {"off", "none", "aucun", "0", "false"}


class DiscordNotifier:
    def __init__(self, webhook_url: str, alert_webhook_url: str = "", mention: str = "", dry_run: bool = True, *,
                 footer: str = "", activity_webhook_url: str = ""):
        self._webhook = webhook_url
        self._alert_webhook = alert_webhook_url or webhook_url
        # Salon d'activité : vide = salon principal (comme avant) ; « off » = logs seulement.
        activity = activity_webhook_url.strip()
        self._activity_webhook = "" if activity.casefold() in _OFF else (activity or webhook_url)
        self._mention = mention
        self._dry_run = dry_run
        self._footer = footer
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
            except TimeoutError:
                log.warning("Discord : %d message(s) non envoyés à l'arrêt", self._queue.qsize())
            self._task.cancel()
        await self._http.aclose()

    def send(self, level: Level, title: str, description: str = "", *, live: bool = False,
             topic: Topic = Topic.SYSTEM, sections: list[tuple[str, str]] | None = None,
             thumbnail: str | None = None, url: str | None = None, important: bool | None = None,
             **fields: Any) -> None:
        log_fn = log.warning if level in (Level.WARNING, Level.CRITICAL) else log.info
        # Les logs reprennent tout le message (champs et blocs), pas seulement le titre.
        parts = [description] if description else []
        parts += [", ".join(f"{k} : {v}" for k, v in fields.items() if v is not None)] if fields else []
        parts += [f"{name} : {body}" for name, body in sections or []]
        log_fn("[%s] %s %s", level.name, title, " | ".join(p for p in parts if p).replace("\n", " | "))
        msg = _Message(level, title, description, fields, live, topic, sections or [], thumbnail, url, important)
        if not self._webhook_for(msg):
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.error("File Discord pleine, message perdu : %s", title)

    def for_topic(self, topic: Topic) -> TopicNotifier:
        """Le même notifier, avec `topic` comme catégorie par défaut."""
        return TopicNotifier(self, topic)

    def info(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.INFO, title, description, **kwargs)

    def success(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.SUCCESS, title, description, **kwargs)

    def warning(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.WARNING, title, description, **kwargs)

    def critical(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.CRITICAL, title, description, **kwargs)

    async def _run(self) -> None:
        while True:
            msg = await self._queue.get()
            try:
                await self._deliver(msg)
            except Exception:
                log.exception("Échec d'envoi Discord : %s", msg.title)
            finally:
                self._queue.task_done()

    def embed(self, msg: _Message) -> dict[str, Any]:
        title = f"[DRY RUN] {msg.title}" if self._dry_run and not msg.live else msg.title
        fields = [{"name": str(name)[:256], "value": str(value)[:_FIELD_MAX] or "-", "inline": False}
                  for name, value in msg.sections]
        fields += [{"name": str(k)[:256], "value": str(v)[:_FIELD_MAX] or "-", "inline": True}
                   for k, v in msg.fields.items()]
        kept, used = [], len(msg.topic.value) + len(title[:256]) + len(self._footer)
        for f in fields[:25]:  # au-delà de la limite totale, les derniers blocs sont coupés
            room = _TOTAL_MAX - used - len(f["name"])
            if room < 20:
                break
            if len(f["value"]) > room:
                f["value"] = f["value"][:room - 1] + "…"
            kept.append(f)
            used += len(f["name"]) + len(f["value"])
        fields = kept
        embed: dict[str, Any] = {
            "author": {"name": msg.topic.value},
            "title": title[:256],
            "color": msg.level.value,
            "timestamp": utcnow().isoformat(),
            "fields": fields,
        }
        if self._footer:
            embed["footer"] = {"text": self._footer[:2048]}
        if msg.thumbnail and msg.thumbnail.startswith(("https://", "http://")):
            embed["thumbnail"] = {"url": msg.thumbnail}
        if msg.url:
            embed["url"] = msg.url
        used = len(msg.topic.value) + len(embed["title"]) + len(self._footer) + sum(len(f["name"]) + len(f["value"]) for f in fields)
        room = max(0, min(4000, _TOTAL_MAX - used))
        if msg.description:
            embed["description"] = (msg.description if len(msg.description) <= room
                                    else msg.description[:max(0, room - 1)] + "…")
        return embed

    def _webhook_for(self, msg: _Message) -> str:
        if not self._webhook:
            return ""
        if msg.level is Level.CRITICAL:
            return self._alert_webhook
        return self._webhook if msg.main_channel else self._activity_webhook

    async def _deliver(self, msg: _Message) -> None:
        url = self._webhook_for(msg)
        payload: dict[str, Any] = {"username": "WikiMastered", "embeds": [self.embed(msg)]}
        if msg.level is Level.CRITICAL and self._mention:
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


class TopicNotifier:
    """Vue d'un notifier avec une catégorie par défaut (celle d'un worker) ; `topic=` la remplace."""

    def __init__(self, parent: DiscordNotifier, topic: Topic):
        self._parent = parent
        self.topic = topic

    def send(self, level: Level, title: str, description: str = "", **kwargs: Any) -> None:
        kwargs.setdefault("topic", self.topic)
        self._parent.send(level, title, description, **kwargs)

    def info(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.INFO, title, description, **kwargs)

    def success(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.SUCCESS, title, description, **kwargs)

    def warning(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.WARNING, title, description, **kwargs)

    def critical(self, title: str, description: str = "", **kwargs: Any) -> None:
        self.send(Level.CRITICAL, title, description, **kwargs)
