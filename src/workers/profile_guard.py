"""Garde : lit le profil du joueur sur le site et arrête tout le bot au premier signe de sanction.

Le profil (RPC Supabase `get_my_profile`, relevé du 16/10/2026) contient `cheat_strikes`,
`activity_blocked_until`, `last_sanction_at` / `last_sanction_type`, `leaderboard_excluded` et
`deleted_at` : le site surveille la triche et peut sanctionner. Une nouvelle sanction arrête le bot
(plus aucune requête, data/HALT écrit) au lieu de laisser les workers aggraver le cas.
Note aussi `pity_counter` à chaque lecture, pour comprendre ce qu'il compte.
Prévient aussi quand les paquets s'accumulent (PACKS_ALERT_AT) : le bot ne les ouvre jamais.
Nécessite SUPABASE_ANON_KEY (comme les étiquettes et les succès).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from src.core import timeutils
from src.core.notifier import Topic
from src.core.timeutils import utcnow
from src.workers.base import Worker

KV_KEY = "profile_guard"  # dernières valeurs vues : une sanction déjà connue n'arrête pas le bot deux fois
PACK_CAP = 10  # stock maximal et régénération, quand le profil ne les donne pas
PACK_REGEN_SECONDS = 600


def _when(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when and when.tzinfo is None else when


def packs_available(profile: dict, now: datetime) -> tuple[int, int] | None:
    """(paquets disponibles, stock maximal). `packs_remaining` n'est recalculé par le site qu'à l'ouverture de sa
    page des paquets : il reste figé entre deux visites, alors on ajoute ce qui s'est régénéré depuis
    `packs_last_regen_at`."""
    remaining = profile.get("packs_remaining")
    if not isinstance(remaining, int):
        return None
    cap = profile.get("pack_cap")
    cap = cap if isinstance(cap, int) and cap > 0 else PACK_CAP
    regen_ms = profile.get("pack_regen_ms")
    regen = regen_ms / 1000 if isinstance(regen_ms, int) and regen_ms > 0 else PACK_REGEN_SECONDS
    since = _when(profile.get("packs_last_regen_at"))
    gained = int((now - since).total_seconds() // regen) if since else 0
    return min(cap, remaining + max(0, gained)), cap


def sanctions(profile: dict, seen: dict, now: datetime) -> list[str]:
    """Signes de sanction nouveaux par rapport à `seen` (valeurs de la lecture précédente)."""
    found = []
    strikes = int(profile.get("cheat_strikes") or 0)
    if strikes > int(seen.get("cheat_strikes") or 0):
        found.append(f"avertissements de triche (cheat_strikes) : {strikes}")
    blocked = _when(profile.get("activity_blocked_until"))
    if blocked and blocked > now:
        found.append(f"activité bloquée jusqu'au {blocked.astimezone(timeutils.PARIS):%d/%m %H:%M}")
    sanction = profile.get("last_sanction_at")
    if sanction and sanction != seen.get("last_sanction_at"):
        at = _when(sanction)
        found.append(f"sanction « {profile.get('last_sanction_type') or '?'} »"
                     + (f" le {at.astimezone(timeutils.PARIS):%d/%m %H:%M}" if at else ""))
    if profile.get("leaderboard_excluded") and not seen.get("leaderboard_excluded"):
        found.append("compte exclu du classement")
    if profile.get("deleted_at"):
        found.append("compte supprimé")
    return found


class ProfileGuard(Worker):
    name = "garde"
    topic = Topic.SYSTEM

    async def setup(self) -> None:
        self._pity: list[int] = []  # valeurs depuis le dernier récap

    async def step(self) -> float:
        status, profile = await self.ctx.client.supabase("POST", "rpc/get_my_profile", json_body={})
        if status != 200 or not isinstance(profile, dict):
            raise RuntimeError(f"profil illisible (HTTP {status})")
        seen = await self.db.kv_get(KV_KEY) or {}
        now = utcnow()
        found = sanctions(profile, seen, now)
        packs = packs_available(profile, now)
        alerted = self._alert_packs(packs, bool(seen.get("packs_alerted")))
        await self.db.kv_set(KV_KEY, {**{k: profile.get(k) for k in
                                         ("cheat_strikes", "last_sanction_at", "leaderboard_excluded")},
                                      "packs_alerted": alerted})
        if found:
            self.ctx.client.halt(" ; ".join(found))
            return 3600.0
        pity = profile.get("pity_counter")
        if isinstance(pity, int):
            self._pity.append(pity)
        self.log.info("profil : pity %s, paquets %s, solde %s", pity, packs[0] if packs else "?",
                      profile.get("wikibidous_balance"))
        minutes = self.settings.profile_check_minutes
        return random.uniform(0.8, 1.2) * minutes * 60

    def _alert_packs(self, packs: tuple[int, int] | None, alerted: bool) -> bool:
        """Un message quand le seuil est atteint, un seul jusqu'à ce que le stock redescende. Renvoie l'état à garder."""
        threshold = self.settings.packs_alert_at
        if not packs or threshold <= 0:
            return alerted
        stock, cap = packs
        reached = stock >= min(threshold, cap)
        if reached and not alerted:
            full = stock >= cap
            self.notifier.info("Paquets pleins" if full else f"{stock} paquets à ouvrir",
                               f"{stock}/{cap} disponibles sur le site"
                               + (" : plus rien ne se régénère tant qu'ils ne sont pas ouverts." if full else "."))
        return reached

    def recap(self) -> list[str]:
        pity, self._pity = self._pity, []
        return [f"Aucune sanction · pity relevé : {', '.join(map(str, pity)) or '—'}"]
