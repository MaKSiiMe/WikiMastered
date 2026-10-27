"""Socle commun des workers : contexte partagé et boucle résistante aux erreurs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from src.config import Settings
from src.core import timeutils
from src.core.api import WikiMastersAPI
from src.core.client import ApiError, RateLimited, SessionExpired, UncertainMutation, WikiMastersClient
from src.core.database import Database
from src.core.notifier import DiscordNotifier, Topic
from src.core.pricing import PriceService
from src.core.timeutils import utcnow


@dataclass
class Context:
    settings: Settings
    client: WikiMastersClient
    api: WikiMastersAPI
    db: Database
    notifier: DiscordNotifier
    prices: PriceService
    # Workers en pause (/pause sur Discord) : nom -> fin, ou None jusqu'à /reprise. Voir src/pauses.py.
    pauses: dict[str, datetime | None] = field(default_factory=dict)
    last_backup: dict = field(default_factory=dict)  # dernière sauvegarde de la base (src/backup.py), pour /etat


class Worker:
    name = "worker"
    topic = Topic.SYSTEM  # catégorie affichée sur ses messages Discord
    # Après N erreurs consécutives, une alerte Discord est envoyée (une seule fois).
    alert_after_errors = 5

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.settings = ctx.settings
        self.api = ctx.api
        self.db = ctx.db
        self.notifier = ctx.notifier.for_topic(self.topic)
        self.log = logging.getLogger(f"worker.{self.name}")
        self._errors = 0
        # Santé, pour le récap : dernier passage réussi, prochain prévu, erreurs depuis le dernier récap.
        self.last_ok: datetime | None = None
        self.next_due: datetime | None = None
        self.recent_errors = 0
        self.last_error = ""

    async def setup(self) -> None:
        """Initialisation (lecture d'état en base...)."""

    async def step(self) -> float:
        """Une itération ; renvoie le délai avant la suivante, en secondes."""
        raise NotImplementedError

    async def run(self) -> None:
        ready = False
        while True:
            await self.ctx.client.wait_session()
            if wait := self._paused_for():
                self.next_due = utcnow() + timedelta(seconds=wait)  # en pause, pas « arrêté » pour le récap
                await asyncio.sleep(wait)
                continue
            try:
                if not ready:
                    # Initialisation dans la boucle : un échec au démarrage (réseau…) est réessayé ici,
                    # sans arrêter ce worker ni les autres.
                    await self.setup()
                    ready = True
                    self.log.info("démarré")
                delay = await self.step()
                self._errors = 0
                self.last_ok = utcnow()
            except asyncio.CancelledError:
                raise
            except (SessionExpired, RateLimited):
                delay = 5  # le client gère déjà la pause et l'alerte
            except UncertainMutation as exc:
                if self._errors == 0:  # une alerte par série : une route en panne ne spamme pas Discord
                    self.notifier.critical(f"{self.name} : résultat incertain", f"{exc}\nRien n'a été rejoué. Le bot "
                                           "relira le résultat sur le site et enverra « … vérifiée ».")
                self._errors += 1
                self.recent_errors += 1
                self.last_error = str(exc)[:200]
                delay = 60
            except (ApiError, RuntimeError, ValueError, httpx.HTTPError) as exc:
                delay = self._on_error(exc)
            except Exception as exc:  # noqa: BLE001 - un worker ne doit jamais tuer le bot
                self.log.exception("erreur inattendue")
                delay = self._on_error(exc)
            self.next_due = utcnow() + timedelta(seconds=max(0.0, delay))
            await asyncio.sleep(max(0.0, delay))

    def _paused_for(self) -> float:
        """Secondes à attendre avant de revérifier la pause (0 : pas en pause). Une pause finie est retirée."""
        if self.name not in self.ctx.pauses:
            return 0.0
        until = self.ctx.pauses[self.name]
        if until is not None and until <= utcnow():
            del self.ctx.pauses[self.name]
            self.log.info("fin de la pause, reprise")
            return 0.0
        left = (until - utcnow()).total_seconds() if until else 30.0
        return min(1.0, max(0.1, left))  # /reprise est vue dans la seconde

    @property
    def errors_in_a_row(self) -> int:
        return self._errors

    @property
    def paused(self) -> str | None:
        """« en pause jusqu'à 14:30 » (ou jusqu'à /reprise), None hors pause."""
        if self.name not in self.ctx.pauses:
            return None
        until = self.ctx.pauses[self.name]
        if until is None:
            return "en pause jusqu'à /reprise"
        return f"en pause jusqu'à {until.astimezone(timeutils.PARIS):%H:%M}" if until > utcnow() else None

    @property
    def blocked(self) -> str | None:
        """Raison pour laquelle le worker attend une action de ta part (affichée par le récap)."""
        return None

    def recap(self) -> list[str]:
        """Lignes d'activité propres au worker pour le récap (compteurs remis à zéro ensuite)."""
        return []

    def _on_error(self, exc: Exception) -> float:
        self._errors += 1
        self.recent_errors += 1
        self.last_error = str(exc)[:200]
        self.log.warning("erreur #%d : %s", self._errors, exc)
        if self._errors == self.alert_after_errors:
            self.notifier.warning(f"{self.name} : erreurs répétées", str(exc)[:500])
        return min(600.0, 15.0 * 2 ** min(self._errors - 1, 5))
