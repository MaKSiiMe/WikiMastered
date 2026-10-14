"""Module B : ouverture automatique des paquets gratuits.

Le stock (1 paquet / 10 min, 10 max) est déduit des champs `packs_remaining` et
`packs_last_regen_at` renvoyés par POST /api/packs/open [WR pack-cards.js]. Aucune route
de statut REST n'est documentée : si tu en déclares une (`pack_status` dans
config/endpoints.yaml), elle est utilisée pour éviter les ouvertures à vide.

Rythme : une session d'ouverture toutes les 15 à 90 min, tirées au hasard, et des paquets
espacés de 5 à 15 s. Le site semble demander une vérification humaine quand les actions
s'enchaînent trop vite : le bot imite un rythme humain.

Vérification humaine : quand le site renvoie `human_verification_required`, le bot
s'arrête et te prévient. Il ne tente jamais de la contourner ; fais-la dans ton navigateur.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from src.config import RARITY_RANK
from src.core.database import utcnow
from src.core.models import Card, PackOpenResult
from src.core import timeutils
from src.core.notifier import Topic
from src.core.timeutils import next_local
from src.workers.base import Worker

STATE_KEY = "packs"


@dataclass
class PackState:
    remaining: int | None = None
    last_regen_at: datetime | None = None
    verification_required: bool = False
    verification_since: datetime | None = None
    paused_until: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> PackState:
        data = data or {}
        parse = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return cls(
            remaining=data.get("remaining"),
            last_regen_at=parse(data.get("last_regen_at")),
            verification_required=bool(data.get("verification_required")),
            verification_since=parse(data.get("verification_since")),
            paused_until=parse(data.get("paused_until")),
        )


def seconds_until_stock(state: PackState, target: int, period: timedelta, now: datetime) -> float:
    """Délai avant d'avoir `target` paquets, d'après le dernier état connu (0 si inconnu)."""
    if state.remaining is None or state.last_regen_at is None:
        return 0.0
    if state.remaining >= target:
        return 0.0
    ready_at = state.last_regen_at + (target - state.remaining) * period
    return max(0.0, (ready_at - now).total_seconds())


class PackOpener(Worker):
    name = "packs"
    topic = Topic.PACKS

    async def setup(self) -> None:
        s = self.settings
        self.period = timedelta(minutes=s.pack_regen_minutes)
        self.target = max(1, min(s.pack_open_min_stock, s.pack_max_stock))
        self.live = s.is_live("open_pack")  # DRY_RUN, sauf LIVE_WRITES=open_pack
        self.state = PackState.from_json(await self.db.kv_get(STATE_KEY)) if self.live else PackState()
        # Première ouverture quelques minutes après le démarrage, puis toutes les 15 à 90 min.
        self._next_open = utcnow() + timedelta(minutes=random.uniform(1, 10))
        self._retry_soon = False  # la dernière tentative a échoué : réessayer dans 5 à 10 min
        self._started_at = utcnow()  # démarrage du bot, noté avec chaque vérification humaine

    def _schedule_next(self) -> None:
        s = self.settings
        minutes = random.uniform(s.pack_interval_min_minutes, max(s.pack_interval_min_minutes,
                                                                  s.pack_interval_max_minutes))
        self._next_open = utcnow() + timedelta(minutes=minutes)
        self.log.info("prochaine ouverture de paquets vers %s", f"{self._next_open.astimezone():%H:%M}")

    def _schedule_retry(self) -> None:
        s = self.settings
        minutes = random.uniform(s.pack_retry_min_minutes, max(s.pack_retry_min_minutes, s.pack_retry_max_minutes))
        self._next_open = utcnow() + timedelta(minutes=minutes)
        self.log.info("ouverture échouée : nouvel essai vers %s", f"{self._next_open.astimezone():%H:%M}")

    async def _save(self) -> None:
        if self.live:
            await self.db.kv_set(STATE_KEY, self.state.to_json())

    async def step(self) -> float:
        now = utcnow()
        if self.state.paused_until and now < self.state.paused_until:
            return min(600.0, (self.state.paused_until - now).total_seconds())

        if now < self._next_open:
            return min(600.0, (self._next_open - now).total_seconds())
        await self._refresh_status()
        wait = seconds_until_stock(self.state, self.target, self.period, now)
        if wait > 0:
            return min(wait, 600.0)
        self._retry_soon = False
        await self._open_batch()
        self._schedule_retry() if self._retry_soon else self._schedule_next()
        return 1.0

    async def _refresh_status(self) -> None:
        if not self.ctx.client.endpoints.has("pack_status"):
            return
        data = await self.ctx.client.get("pack_status")
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return
        if isinstance(data.get("packs_remaining"), int):
            self.state.remaining = data["packs_remaining"]
        if data.get("packs_last_regen_at"):
            self.state.last_regen_at = datetime.fromisoformat(str(data["packs_last_regen_at"]).replace("Z", "+00:00"))

    async def _open_batch(self) -> None:
        opened = 0
        cards_pulled = 0
        highlights: list[tuple[Card, bool]] = []
        rate_retries = 0
        fresh_regen = False
        while opened <= self.settings.pack_max_stock:
            result = await self.api.open_pack()
            if result.dry_run:
                self.log.info("[DRY RUN] ouverture de paquet simulée")
                self.state.remaining, self.state.last_regen_at = 0, utcnow()
                return

            payload = result.payload if isinstance(result.payload, dict) else {}
            pack = PackOpenResult.model_validate(payload)
            if pack.packs_remaining is not None:
                self.state.remaining = pack.packs_remaining
            fresh_regen = pack.packs_last_regen_at is not None
            if fresh_regen:
                self.state.last_regen_at = pack.packs_last_regen_at

            if pack.human_verification_required or "human_verification_required" in str(payload):
                await self._pause_for_verification()
                return
            if self.state.verification_required and result.ok:
                self.notifier.success("Paquets : vérification levée", "Les ouvertures reprennent.", live=self.live)
                await self.db.clear_pack_check()
            self.state.verification_required, self.state.verification_since = False, None

            if pack.rate_limit_daily:
                resume = next_local(time(0, 0)) + timedelta(minutes=random.uniform(5, 30))
                self.state.paused_until = resume.astimezone(timezone.utc)
                self.notifier.warning("Paquets : limite quotidienne atteinte", f"Reprise vers {resume:%d/%m %H:%M}.",
                                      live=self.live)
                await self._save()
                return

            pulled = pack.pulled()
            if not pulled and pack.rate_limited and pack.retry_after:
                wait = (pack.retry_after - utcnow()).total_seconds() + random.uniform(1, 5)
                rate_retries += 1
                if rate_retries > 5 or wait > 300:
                    self.log.warning("ouverture limitée par le serveur, nouvel essai dans quelques minutes")
                    self._retry_soon = True
                    break
                await asyncio.sleep(max(0.5, wait))
                continue

            if not result.ok or not pulled:
                if not (result.status == 400 and pack.packs_remaining == 0):
                    self._retry_soon = True  # refus inconnu : nouvel essai dans 5 à 10 min
                    self.log.warning("ouverture refusée (HTTP %s) : %s", result.status,
                                     pack.error or pack.message or str(payload)[:200])
                break

            opened += 1
            cards_pulled += len(pulled)
            highlights += await self._record(pulled)
            if not pack.packs_remaining:
                break
            await asyncio.sleep(random.uniform(self.settings.pack_between_opens_min_seconds,
                                               self.settings.pack_between_opens_max_seconds))

        if opened == 0 and self.state.remaining == 0 and not fresh_regen:
            # Stock vide sans date de régénération fraîche : notre estimation était périmée,
            # on attend une période complète plutôt que de réessayer en boucle.
            self.state.last_regen_at = utcnow()
        if opened:
            self.log.info("%d paquet(s) ouvert(s), stock restant : %s", opened, self.state.remaining)
            counts = {"boosters": opened, "cartes": cards_pulled, "stock restant": self.state.remaining}
            if highlights:
                best, _ = max(highlights, key=lambda h: (RARITY_RANK.get(h[0].rarity or "", -1), h[1]))
                self.notifier.success("Boosters ouverts : tirage remarquable !",
                                      "\n".join(highlight_line(card, shiny) for card, shiny in highlights),
                                      live=self.live, thumbnail=best.image_url, url=best.page_url, **counts)
            else:
                self.notifier.info("Boosters ouverts", live=self.live, **counts)
        await self._save()

    async def _pause_for_verification(self) -> None:
        self._retry_soon = True
        if not self.state.verification_required:
            self.notifier.critical(
                "Paquets : vérification humaine demandée",
                "WikiMasters demande une vérification. Ouvre la page Paquets dans ton navigateur et "
                "fais-la toi-même ; aucun paquet n'a été consommé. Le bot réessaie toutes les 5 à 10 min "
                "(le stock plafonne à 10 paquets).",
                live=self.live,
            )
            await self.db.record_pack_check(self._started_at)
        self.state.verification_required = True
        self.state.verification_since = self.state.verification_since or utcnow()
        await self._save()

    @property
    def blocked(self) -> str | None:
        if not self.state.verification_required:
            return None
        since = (f" depuis {self.state.verification_since.astimezone(timeutils.PARIS):%d/%m %H:%M}"
                 if self.state.verification_since else "")
        return f"vérification humaine en attente{since} : fais-la sur la page Paquets"

    async def _record(self, pulled: list) -> list[tuple[Card, bool]]:
        """Enregistre les cartes d'un paquet ; renvoie les tirages remarquables (UR, L, Shiny)."""
        notify_rarities = set(self.settings.pack_notify_rarities)
        highlights = []
        await self.db.record_pack_open(len(pulled))
        for card, copy in pulled:
            shiny = bool(copy and copy.is_shiny) or card.is_shiny
            await self.db.record_pull(
                card_id=card.id, owned_card_id=copy.id if copy else None, title=card.wikipedia_title,
                rarity=card.rarity, is_shiny=shiny, pageviews=card.pageviews,
            )
            if card.rarity in notify_rarities or (shiny and self.settings.pack_notify_shiny):
                highlights.append((card, shiny))
        self.log.info("paquet ouvert : %s", ", ".join(f"{c.rarity}:{c.wikipedia_title}" for c, _ in pulled))
        return highlights


# Une vérification demandée peu après un démarrage du bot compte comme « après un démarrage ».
AFTER_START = timedelta(minutes=15)


async def checks_report(db) -> list[str]:
    """Vérifications humaines : quand, combien de boosters et de temps depuis la précédente, démarrage proche."""
    rows = await db.pack_checks()
    if not rows:
        return ["Aucune vérification humaine enregistrée."]
    parse = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
    lines, packs, gaps, after_start = [], [], [], 0
    previous = None
    for r in rows:
        asked, cleared, started = parse(r["requested_at"]), parse(r["cleared_at"]), parse(r["bot_started_at"])
        since_start = asked - started if started else None
        fresh = since_start is not None and since_start <= AFTER_START
        after_start += fresh
        detail = []
        if previous is not None and previous["cleared_at"]:
            # Boosters ouverts entre la levée de la précédente et celle-ci.
            n = await db.packs_opened_between(previous["cleared_at"], r["requested_at"])
            gap = asked - parse(previous["cleared_at"])
            packs.append(n)
            gaps.append(gap)
            detail.append(f"{n} booster(s) et {_duration(gap)} depuis la précédente")
        if since_start is not None:
            detail.append(f"{_duration(since_start)} après le démarrage du bot")
        detail.append(f"faite en {_duration(cleared - asked)}" if cleared else "en attente")
        lines.append(f"  {asked.astimezone(timeutils.PARIS):%d/%m %H:%M}  " + ", ".join(detail))
        previous = r
    head = [f"{len(rows)} vérification(s), dont {after_start} dans les {AFTER_START.seconds // 60} min "
            "après un démarrage du bot"]
    if packs:
        head.append(f"Entre deux vérifications : {_spread(packs)} booster(s), "
                    f"{_duration(sorted(gaps)[len(gaps) // 2])} (médiane)")
    return head + lines[-20:]


def _spread(values: list[int]) -> str:
    ordered = sorted(values)
    return f"médiane {ordered[len(ordered) // 2]} (de {ordered[0]} à {ordered[-1]})"


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


def highlight_line(card: Card, shiny: bool) -> str:
    title = f"[{card.wikipedia_title}]({card.page_url})" if card.page_url else card.wikipedia_title
    return f"**{card.rarity}**{' ✨ Shiny' if shiny else ''} — {title}"
