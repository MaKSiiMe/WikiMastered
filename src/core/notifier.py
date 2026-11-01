"""Notifications Discord par webhook.

Les messages passent par une file : un worker ne bloque jamais sur Discord, et la
limitation de Discord (429 + retry_after) est respectée. Sans webhook configuré,
les messages ne sont que loggés.

Chaque message porte la catégorie de ce qui l'envoie (sniper, ventes, paquets…), affichée
au-dessus du titre pour repérer d'un coup d'œil d'où il vient. Une catégorie peut avoir son propre salon
(DISCORD_WEBHOOK_SNIPER, _VENTES…) ; sans lui, le salon principal ou d'activité, selon le message.

Une position (carte achetée pour la revente) n'a qu'un message, modifié à chaque étape (`Thread`) : achetée, en
vente, remise en vente… Discord ne signale pas une modification : la vente (ou un problème) republie le message en
bas du salon.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from src.core import colors
from src.core.journal import plain
from src.core.timeutils import now_local, utcnow

log = logging.getLogger(__name__)


class Topic(Enum):
    """Catégorie d'un message ; la valeur est l'étiquette affichée au-dessus du titre."""

    SYSTEM = "⚙️ Système"        # démarrage, session, serveurs
    SNIPER = "🎯 Sniper"          # enchères, positions achetées
    COLLECTION = "📅 Collection"  # cartes de collection obtenues, étiquettes
    SALES = "💰 Ventes"           # mises en vente, reventes, défausses
    ACHIEVEMENTS = "🏆 Succès"    # récompenses de succès réclamées
    TRADES = "🤝 Échanges"        # échanges proposés, évalués
    REPORT = "📊 Bilan"           # bilan du soir
    RECAP = "🩺 Récap"            # récap périodique des workers


# Limites des embeds Discord
_TOTAL_MAX = 5800  # 6000 au plus pour l'ensemble d'un embed, avec de la marge
_FIELD_MAX = 1024


# Salon propre à chaque catégorie (réglages DISCORD_WEBHOOK_<SALON>) : nom du salon par catégorie.
TOPIC_CHANNELS = {
    Topic.SNIPER: "sniper",
    Topic.SALES: "ventes",
    Topic.TRADES: "ventes",
    Topic.COLLECTION: "collection",
    Topic.ACHIEVEMENTS: "collection",
    Topic.REPORT: "bilan",
    Topic.RECAP: "bilan",
    Topic.SYSTEM: "systeme",
}

THREAD_LINES = 15  # étapes gardées dans le message d'une position (les plus récentes)


@dataclass
class Thread:
    """Étape d'une position, ajoutée à son message unique (`key` : « position:12 ») au lieu d'un nouveau message."""

    key: str
    card: str                # titre du message : la carte
    line: str                # étape ajoutée à l'historique, précédée de l'heure
    head: str | None = None  # ligne sous le titre (remplace la précédente) : achat, revente visée…
    ring: bool = False       # message republié en bas du salon, pour être signalé (vente, problème)


def position_thread(bid: Any, line: str, *, head: str | None = None, ring: bool = False) -> Thread:
    """Étape du message unique d'une position ; `bid` : sa ligne de la table bids (id, titre, rareté, shiny)."""
    shiny = " ✨" if bid["is_shiny"] else ""
    return Thread(f"position:{bid['id']}", f"{bid['title']} ({bid['rarity'] or '?'}{shiny})", line, head, ring)


class Level(Enum):
    INFO = colors.INFO
    SUCCESS = colors.OK
    WARNING = colors.WARN
    CRITICAL = colors.ALERT


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
    # Boutons proposés par le bot Discord (DISCORD_BOT_TOKEN) : {"kind": "relist", "listing": 12}… ; sans bot, le
    # message part par le webhook, sans boutons. Jamais de message privé : tout va dans les salons.
    actions: dict[str, Any] | None = None
    thread: Thread | None = None
    columns: list[tuple[str, str]] = field(default_factory=list)  # chiffres clés côte à côte, avant les blocs

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
                 footer: str = "", activity_webhook_url: str = "", channel_webhooks: dict[str, str] | None = None):
        # Salon principal ; à défaut, le premier salon de catégorie configuré (repli des autres catégories).
        channels = {name: url.strip() for name, url in (channel_webhooks or {}).items() if url.strip()}
        webhook_url = webhook_url or next(iter(channels.values()), "")
        self._channels = {topic: channels[name] for topic, name in TOPIC_CHANNELS.items() if name in channels}
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
        # Branchés par le bot Discord quand il est connecté (src/discord_bot.py) :
        # with_buttons(actions, embed, url du webhook, mention) -> identifiant du message envoyé par le bot dans le
        # salon du webhook, ou None ; bot_edit / bot_delete(url, identifiant…) -> fait ?
        self.with_buttons: Callable[[dict[str, Any] | None, dict[str, Any], str, str], Awaitable[str | None]] | None = None
        self.bot_edit: Callable[[str, str, dict[str, Any], dict[str, Any] | None], Awaitable[bool]] | None = None
        self.bot_delete: Callable[[str, str], Awaitable[bool]] | None = None
        # Base (kv) où sont gardés les messages des positions, branchée par build_context (src/main.py).
        self.store: Any = None

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
             actions: dict[str, Any] | None = None, thread: Thread | None = None,
             columns: list[tuple[str, str]] | None = None, **fields: Any) -> None:
        log_fn = log.warning if level in (Level.WARNING, Level.CRITICAL) else log.info
        # Les logs reprennent tout le message (champs et blocs), sans sa mise en forme : la console le coupe s'il
        # est long, le journal le garde entier.
        parts = [description] if description else []
        parts += [", ".join(f"{k} : {v}" for k, v in [*(columns or []), *fields.items()] if v is not None)] \
            if fields or columns else []
        parts += [f"{name} : {body}" for name, body in sections or []]
        text = plain(" | ".join(p for p in parts if p).replace("\n", " | "))
        log_fn("%s%s · %s%s", "CRITIQUE · " if level is Level.CRITICAL else "", topic.value.split(" ", 1)[-1],
               plain(title), f" — {text}" if text else "", extra={"data": {"salon": topic.name.lower()}})
        msg = _Message(level, title, description, fields, live, topic, sections or [], thumbnail, url, important,
                       actions, thread, columns or [])
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
        fields = [{"name": str(name)[:256], "value": str(value)[:_FIELD_MAX] or "-", "inline": True}
                  for name, value in msg.columns]
        fields += [{"name": str(name)[:256], "value": str(value)[:_FIELD_MAX] or "-", "inline": False}
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
        if channel := self._channels.get(msg.topic):
            return channel
        if msg.thread is not None:
            return self._webhook  # l'argent : le salon principal
        return self._webhook if msg.main_channel else self._activity_webhook

    async def _deliver(self, msg: _Message) -> None:
        url = self._webhook_for(msg)
        if msg.thread is not None and self.store is not None:
            await self._deliver_thread(msg, url)
            return
        mention = self._mention if msg.level is Level.CRITICAL else ""
        await self._post(url, self.embed(msg), msg.actions, mention, title=msg.title)

    async def _post(self, url: str, embed: dict[str, Any], actions: dict[str, Any] | None, mention: str = "", *,
                    title: str = "", keep: bool = False) -> dict[str, str] | None:
        """Envoie un message ; avec des boutons (ou `keep`, pour le modifier ensuite), par le bot s'il est connecté,
        sinon par le webhook. Renvoie de quoi le retrouver ({"via", "url", "id"}), si Discord l'a dit."""
        if (actions or keep) and self.with_buttons:
            try:
                if message_id := await self.with_buttons(actions, embed, url, mention):
                    return {"via": "bot", "url": url, "id": str(message_id)}
            except Exception:  # noqa: BLE001 - repli : le webhook, sans boutons
                log.exception("Message par le bot impossible : %s", title or embed.get("title"))
        payload: dict[str, Any] = {"username": "WikiMastered", "embeds": [embed]}
        if mention:
            payload["content"] = mention
        resp = await self._webhook_call("POST", url, payload, params={"wait": "true"} if keep else None)
        if keep and resp is not None and resp.status_code < 300:
            return {"via": "webhook", "url": url, "id": str((resp.json() or {}).get("id", ""))}
        return None

    async def _webhook_call(self, method: str, url: str, payload: dict[str, Any] | None = None,
                            params: dict[str, str] | None = None) -> httpx.Response | None:
        """Appel au webhook, en respectant la limitation de Discord (429 + retry_after)."""
        for _ in range(5):
            resp = await self._http.request(method, url, json=payload, params=params)
            if resp.status_code == 429:
                retry = float((resp.json() or {}).get("retry_after", 2))
                await asyncio.sleep(min(retry, 60))
                continue
            if resp.status_code >= 400:
                log.error("Discord HTTP %s : %s", resp.status_code, resp.text[:200])
            return resp
        return None

    def thread_embed(self, msg: _Message, state: dict[str, Any]) -> dict[str, Any]:
        """Message d'une position : la carte en titre, la ligne d'en-tête, puis l'historique de ses étapes."""
        title = f"[DRY RUN] {state['card']}" if self._dry_run and not msg.live else state["card"]
        lines = "\n".join(state["lines"])
        description = f"{state['head']}\n\n{lines}" if state.get("head") else lines
        embed: dict[str, Any] = {
            "author": {"name": msg.topic.value},
            "title": title[:256],
            "description": description[:4000],
            "color": msg.level.value,
            "timestamp": utcnow().isoformat(),
        }
        if self._footer:
            embed["footer"] = {"text": self._footer[:2048]}
        if state.get("thumbnail"):
            embed["thumbnail"] = {"url": state["thumbnail"]}
        if state.get("link"):
            embed["url"] = state["link"]
        return embed

    async def _deliver_thread(self, msg: _Message, url: str) -> None:
        """Ajoute l'étape au message de la position : modifié en place, ou republié en bas du salon (`ring`, ou
        message introuvable). L'état (carte, en-tête, étapes, message Discord) est gardé en base."""
        thread = msg.thread
        assert thread is not None
        key = f"discord:{thread.key}"
        state = await self.store.kv_get(key) or {"lines": []}
        state.setdefault("card", thread.card)  # celui de l'achat : une vente ne sait pas si la carte est shiny
        if thread.head:
            state["head"] = thread.head
        if msg.url:
            state["link"] = msg.url
        if msg.thumbnail and msg.thumbnail.startswith(("https://", "http://")):
            state["thumbnail"] = msg.thumbnail
        now = now_local()
        stamp = f"{now:%H:%M}" if state.get("day", f"{now:%d/%m}") == f"{now:%d/%m}" else f"{now:%d/%m %H:%M}"
        state["day"] = f"{now:%d/%m}"
        state["lines"] = (state["lines"] + [f"`{stamp}` {thread.line}"])[-THREAD_LINES:]
        embed = self.thread_embed(msg, state)
        ref = state.get("message")
        if ref and ref.get("url") == url and not thread.ring and await self._edit(ref, embed, msg.actions):
            await self.store.kv_set(key, state)
            return
        if ref:
            await self._remove(ref)
        state["message"] = await self._post(url, embed, msg.actions, title=msg.title, keep=True)
        await self.store.kv_set(key, state)

    async def _edit(self, ref: dict[str, str], embed: dict[str, Any], actions: dict[str, Any] | None) -> bool:
        try:
            if ref["via"] == "bot":
                return bool(self.bot_edit and await self.bot_edit(ref["url"], ref["id"], embed, actions))
            resp = await self._webhook_call("PATCH", f"{ref['url']}/messages/{ref['id']}", {"embeds": [embed]})
            return resp is not None and resp.status_code < 300
        except Exception:  # noqa: BLE001 - repli : un nouveau message
            log.exception("Message de position non modifié")
            return False

    async def _remove(self, ref: dict[str, str]) -> None:
        try:
            if ref["via"] == "bot":
                if self.bot_delete:
                    await self.bot_delete(ref["url"], ref["id"])
            else:
                await self._webhook_call("DELETE", f"{ref['url']}/messages/{ref['id']}")
        except Exception:  # noqa: BLE001 - l'ancien message reste, le nouveau part quand même
            log.exception("Ancien message de position non supprimé")


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
