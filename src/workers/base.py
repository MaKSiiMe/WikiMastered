"""Socle commun des workers : contexte partagé et boucle résistante aux erreurs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from src.config import Settings
from src.core.api import WikiMastersAPI
from src.core.client import ApiError, RateLimited, SessionExpired, UncertainMutation, WikiMastersClient
from src.core.database import Database
from src.core.notifier import DiscordNotifier
from src.core.pricing import PriceService


@dataclass
class Context:
    settings: Settings
    client: WikiMastersClient
    api: WikiMastersAPI
    db: Database
    notifier: DiscordNotifier
    prices: PriceService


class Worker:
    name = "worker"
    # Après N erreurs consécutives, une alerte Discord est envoyée (une seule fois).
    alert_after_errors = 5

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.settings = ctx.settings
        self.api = ctx.api
        self.db = ctx.db
        self.notifier = ctx.notifier
        self.log = logging.getLogger(f"worker.{self.name}")
        self._errors = 0

    async def setup(self) -> None:
        """Initialisation (lecture d'état en base...)."""

    async def step(self) -> float:
        """Une itération ; renvoie le délai avant la suivante, en secondes."""
        raise NotImplementedError

    async def run(self) -> None:
        await self.setup()
        self.log.info("démarré")
        while True:
            await self.ctx.client.wait_session()
            try:
                delay = await self.step()
                self._errors = 0
            except asyncio.CancelledError:
                raise
            except (SessionExpired, RateLimited):
                delay = 5  # le client gère déjà la pause et l'alerte
            except UncertainMutation as exc:
                self.notifier.critical(f"{self.name} : résultat incertain", f"{exc}\nVérifie sur le site, rien n'a été rejoué.")
                delay = 60
            except (ApiError, RuntimeError, ValueError, httpx.HTTPError) as exc:
                delay = self._on_error(exc)
            except Exception as exc:  # noqa: BLE001 - un worker ne doit jamais tuer le bot
                self.log.exception("erreur inattendue")
                delay = self._on_error(exc)
            await asyncio.sleep(max(0.0, delay))

    def _on_error(self, exc: Exception) -> float:
        self._errors += 1
        self.log.warning("erreur #%d : %s", self._errors, exc)
        if self._errors == self.alert_after_errors:
            self.notifier.warning(f"{self.name} : erreurs répétées", str(exc)[:500])
        return min(600.0, 15.0 * 2 ** min(self._errors - 1, 5))
