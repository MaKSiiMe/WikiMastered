"""Garde : lit le profil du joueur sur le site et arrête tout le bot au premier signe de sanction.

Le profil (RPC Supabase `get_my_profile`, relevé du 16/10/2026) contient `cheat_strikes`,
`activity_blocked_until`, `last_sanction_at` / `last_sanction_type`, `leaderboard_excluded` et
`deleted_at` : le site surveille la triche et peut sanctionner. Une nouvelle sanction arrête le bot
(plus aucune requête, data/HALT écrit) au lieu de laisser les workers aggraver le cas.
Note aussi `pity_counter` à chaque lecture, pour comprendre ce qu'il compte.
Nécessite SUPABASE_ANON_KEY (comme les étiquettes et les succès).
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from src.core import timeutils
from src.core.database import utcnow
from src.core.notifier import Topic
from src.workers.base import Worker

KV_KEY = "profile_guard"  # dernières valeurs vues : une sanction déjà connue n'arrête pas le bot deux fois


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


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
        found = sanctions(profile, seen, utcnow())
        await self.db.kv_set(KV_KEY, {k: profile.get(k) for k in
                                      ("cheat_strikes", "last_sanction_at", "leaderboard_excluded")})
        if found:
            self.ctx.client.halt(" ; ".join(found))
            return 3600.0
        pity = profile.get("pity_counter")
        if isinstance(pity, int):
            self._pity.append(pity)
        self.log.info("profil : pity %s, paquets %s, solde %s", pity, profile.get("packs_remaining"),
                      profile.get("wikibidous_balance"))
        minutes = self.settings.profile_check_minutes
        return random.uniform(0.8, 1.2) * minutes * 60

    def recap(self) -> list[str]:
        pity, self._pity = self._pity, []
        return [f"Aucune sanction · pity relevé : {', '.join(map(str, pity)) or '—'}"]
