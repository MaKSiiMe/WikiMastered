"""Temps réel du site (ENABLE_REALTIME) : les événements que le site pousse à ses pages, reçus par le bot.

Relevé du 21/11 dans le code du site (client Supabase Realtime, canaux privés) :
- `auction:<id>` : « BID » à chaque mise (payload {bid: {amount, bidder_id, bidder}, end_at}) et « UPDATE » ;
- `notifications:<user_id>` : « INSERT » à chaque notification (payload {record: {...}}) ;
- `profile:<user_id>` : « UPDATE » (solde, paquets…).
Il n'y a pas de canal des nouvelles ventes du marché : le repérage par pages reste nécessaire.

Protocole Phoenix (vsn 1.0.0, messages JSON {topic, event, payload, ref, join_ref}) sur
wss://<projet>.supabase.co/realtime/v1/websocket?apikey=<clé publique>&vsn=1.0.0, avec le jeton de la session du bot
(celui de ses requêtes, renvoyé quand il est renouvelé) ; battement toutes les 25 s comme le site ; reconnexion avec un
délai croissant. Le temps réel ne fait qu'avancer des lectures que le bot fait déjà : s'il tombe, rien ne manque.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.core.client import WikiMastersClient

log = logging.getLogger("temps-reel")

HEARTBEAT_SECONDS = 25.0  # comme le site (heartbeatIntervalMs)
TOKEN_CHECK_SECONDS = 30.0
RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)
REFUSED_LIMIT = 3  # abonnements refusés d'affilée : temps réel coupé (le site ne l'accepte pas pour ce compte)

Handler = Callable[[str, dict[str, Any]], None]  # (événement, payload) ; appelé dans la boucle, doit être rapide
Connect = Callable[[str], Awaitable[Any]]  # ouvre la connexion (tests : une fausse)


def topic(name: str) -> str:
    return f"realtime:{name}"


class Realtime:
    def __init__(self, client: WikiMastersClient, connect: Connect | None = None):
        self.client = client
        self.settings = client.settings
        self._connect = connect or _websocket
        self._handlers: dict[str, Handler] = {}  # canal (sans « realtime: ») -> fonction
        self._joined: set[str] = set()
        self._ws: Any = None
        self._ref = 0
        self._token = ""
        self._refused = 0
        self.disabled = ""  # raison de l'arrêt du temps réel ("" : actif)
        self.events = 0  # événements reçus (relevés)
        self.connected = False

    # ------------------------------------------------------------ abonnements
    def subscribe(self, name: str, handler: Handler) -> None:
        """Écoute ce canal (sans effet s'il l'est déjà) ; abonné tout de suite si la connexion est ouverte."""
        if name in self._handlers or self.disabled:
            return
        self._handlers[name] = handler
        if self._ws is not None:
            self._spawn(self._join(name))

    def unsubscribe(self, name: str) -> None:
        if self._handlers.pop(name, None) is not None and self._ws is not None and name in self._joined:
            self._joined.discard(name)
            self._spawn(self._send(topic(name), "phx_leave", {}))

    def watching(self, name: str) -> bool:
        return name in self._handlers

    # ------------------------------------------------------------ connexion
    def _url(self) -> str | None:
        session = self.client.auth_session()
        key = self.settings.supabase_anon_key
        if session is None or not key:
            return None
        base = (self.settings.supabase_url or f"https://{session.ref}.supabase.co").rstrip("/")
        return f"{base.replace('https://', 'wss://').replace('http://', 'ws://')}/realtime/v1/websocket?apikey={key}" \
               "&vsn=1.0.0"

    def _access_token(self) -> str:
        session = self.client.auth_session()
        return str(session.data.get("access_token") or "") if session else ""

    async def run(self) -> None:
        """Connexion tenue tant que le bot tourne ; refermée et rouverte après une coupure."""
        failures = 0
        while not self.disabled:
            url = self._url()
            if url is None:
                await asyncio.sleep(30)
                continue
            try:
                self._ws = await self._connect(url)
            except Exception as exc:  # noqa: BLE001 - réseau, refus : nouvel essai plus tard
                failures += 1
                delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS)) - 1]
                log.info("temps réel : connexion impossible (%s), nouvel essai dans %d s", type(exc).__name__, delay)
                await asyncio.sleep(delay)
                continue
            self.connected, self._joined, self._token = True, set(), self._access_token()
            if failures:
                log.info("temps réel : reconnecté")
            failures = 0
            beats = asyncio.create_task(self._heartbeat())
            try:
                for name in list(self._handlers):
                    await self._join(name)
                await self._receive()
            except Exception as exc:  # noqa: BLE001 - connexion perdue : rouverte
                log.info("temps réel : connexion perdue (%s)", type(exc).__name__)
            finally:
                beats.cancel()
                self.connected, self._ws = False, None
                await asyncio.sleep(RECONNECT_DELAYS[0])
        log.warning("temps réel coupé : %s", self.disabled)

    async def _receive(self) -> None:
        while True:
            message = json.loads(await self._ws.recv())
            if isinstance(message, dict):
                self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        event, payload = message.get("event"), message.get("payload") or {}
        name = str(message.get("topic") or "").removeprefix("realtime:")
        if event == "phx_reply" and name in self._handlers and isinstance(payload, dict):
            if payload.get("status") == "ok":
                self._joined.add(name)
                self._refused = 0
            else:
                self._refused += 1
                log.info("temps réel : abonnement à %s refusé (%s)", name, json.dumps(payload.get("response"))[:200])
                if self._refused >= REFUSED_LIMIT:
                    self.disabled = "abonnements refusés par le site"
            return
        if event in ("phx_error", "phx_close") and name in self._handlers:
            self._joined.discard(name)  # canal fermé par le serveur : réabonné un peu plus tard
            self._spawn(self._rejoin(name))
            return
        if event == "broadcast" and isinstance(payload, dict) and (handler := self._handlers.get(name)):
            self.events += 1
            try:
                handler(str(payload.get("event") or ""), payload.get("payload") or {})
            except Exception:  # une fonction en erreur ne coupe pas la connexion
                log.exception("temps réel : événement %s de %s mal traité", payload.get("event"), name)

    async def _heartbeat(self) -> None:
        """Battement (sinon le serveur ferme la connexion) ; jeton de la session renvoyé quand il change."""
        waited = 0.0
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._send("phoenix", "heartbeat", {})
            waited += HEARTBEAT_SECONDS
            if waited >= TOKEN_CHECK_SECONDS:
                waited = 0.0
                token = self._access_token()
                if token and token != self._token:
                    self._token = token
                    for name in list(self._joined):
                        await self._send(topic(name), "access_token", {"access_token": token})

    async def _rejoin(self, name: str) -> None:
        await asyncio.sleep(RECONNECT_DELAYS[2])
        if name in self._handlers and name not in self._joined:
            await self._join(name)

    async def _join(self, name: str) -> None:
        config = {"broadcast": {"ack": False, "self": False}, "presence": {"key": "", "enabled": False},
                  "postgres_changes": [], "private": True}
        await self._send(topic(name), "phx_join", {"config": config, "access_token": self._access_token()},
                         join=True)

    async def _send(self, to: str, event: str, payload: dict[str, Any], *, join: bool = False) -> None:
        if self._ws is None:
            return
        self._ref += 1
        ref = str(self._ref)
        message = {"topic": to, "event": event, "payload": payload, "ref": ref}
        if join:
            message["join_ref"] = ref
        with contextlib.suppress(Exception):  # connexion en train de tomber : _receive la rouvrira
            await self._ws.send(json.dumps(message))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


async def _websocket(url: str) -> Any:
    from websockets.asyncio.client import connect  # chargé seulement avec ENABLE_REALTIME

    return await connect(url, open_timeout=20, ping_interval=None, max_size=2 ** 20,
                         user_agent_header="Mozilla/5.0")
