"""Cache de prix partagé avec d'autres joueurs : le serveur de l'extension WikiMastered (dépôt
MaKSiiMe/WikiMastered-extension, dossier server/, un Cloudflare Worker).

Le quota du site sur /api/marketplace/cards/{id}/price-history (~90-100 lectures par 10 min, sans doute par compte)
est partagé entre ce bot et l'extension du même compte. Avec SHARED_PRICES_URL et SHARED_PRICES_TOKEN, le bot demande
d'abord au serveur commun si un participant (extension, autre bot) a lu la carte depuis moins de QUOTE_CACHE_MINUTES,
et y dépose chaque historique qu'il lit lui-même. Seuls l'identifiant de la carte et la réponse du site
(`computed_at`, `by_rarity`) partent : ni cookie, ni pseudo, ni collection.

Le serveur ne doit jamais gêner le bot : une erreur ou un délai dépassé fait lire le site comme avant, et le serveur
n'est plus essayé pendant OFF_AFTER_ERROR.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
OFF_AFTER_ERROR = timedelta(minutes=5)
PUSH_DELAY_SECONDS = 5.0   # lectures déposées par lots
PUSH_BATCH = 50            # … ou dès qu'il y en a autant (le serveur en prend 100 au plus)


class SharedPrices:
    def __init__(self, url: str, token: str, max_age: timedelta, http: httpx.AsyncClient | None = None):
        self.enabled = bool(url and token)
        self.max_age = max_age
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http
        if self.enabled and self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS))
        self._off_until = 0.0
        self._pending: list[dict[str, Any]] = []
        self._flush_task: asyncio.Task | None = None
        self.served = 0   # historiques reçus du serveur (lectures du site évitées)
        self.missed = 0   # cartes absentes du cache partagé
        self.stored = 0   # historiques déposés

    def _available(self) -> bool:
        return self.enabled and time.monotonic() >= self._off_until

    def _failed(self, what: str, exc: Exception) -> None:
        self._off_until = time.monotonic() + OFF_AFTER_ERROR.total_seconds()
        log.info("prix partagés : %s impossible (%s), serveur ignoré %d min", what, exc,
                 OFF_AFTER_ERROR.seconds // 60)

    async def get(self, card_id: str) -> dict[str, Any] | None:
        """Historique de la carte lu par un participant depuis moins de `max_age`, ou None (absent, serveur coupé)."""
        if not self._available():
            return None
        try:
            resp = await self._http.get(f"{self._url}/v1/history",
                                        params={"ids": card_id, "max_age": int(self.max_age.total_seconds())},
                                        headers=self._headers)
            resp.raise_for_status()
            item = (resp.json().get("items") or {}).get(card_id)
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            self._failed("lecture", exc)
            return None
        history = item.get("history") if isinstance(item, dict) else None
        if isinstance(history, dict) and isinstance(history.get("by_rarity"), dict):
            self.served += 1
            log.debug("prix partagés : %s lu par %s", card_id, item.get("read_by"))
            return history
        self.missed += 1
        return None

    def put(self, card_id: str, data: dict[str, Any]) -> None:
        """Dépose un historique lu sur le site (envoyé par lot, en arrière-plan)."""
        if not self.enabled or not isinstance(data, dict) or not isinstance(data.get("by_rarity"), dict):
            return
        self._pending.append({"id": card_id, "read_at": int(time.time() * 1000),
                              "history": {"computed_at": data.get("computed_at"), "by_rarity": data["by_rarity"]}})
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        if len(self._pending) < PUSH_BATCH:
            await asyncio.sleep(PUSH_DELAY_SECONDS)
        await self.flush()

    async def flush(self) -> None:
        while self._pending:
            batch, self._pending = self._pending[:100], self._pending[100:]
            if not self._available():
                return  # serveur coupé : ces lectures restent seulement chez nous
            try:
                resp = await self._http.post(f"{self._url}/v1/history", json={"items": batch}, headers=self._headers)
                resp.raise_for_status()
                self.stored += int(resp.json().get("stored") or 0)
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                self._failed("dépôt", exc)
                return

    async def close(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        if self.enabled:
            await self.flush()
            await self._http.aclose()
