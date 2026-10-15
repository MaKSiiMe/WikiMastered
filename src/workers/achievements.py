"""Module H : réclame les récompenses des succès débloqués (wikibidous gratuits).

Toutes les ACHIEVEMENTS_INTERVAL_MINUTES (~3 h) : demande au site de recalculer les succès
(POST /api/achievements/check), lit ceux qui sont débloqués et pas encore réclamés (tables
Supabase `user_achievements` et `achievements`), puis les réclame un par un, espacés de
quelques secondes (POST /api/achievements/claim). Routes relevées dans des projets publics,
pas encore vérifiées : les premières réponses sont gardées dans data/inspect/writes/.
Nécessite SUPABASE_ANON_KEY (comme les étiquettes).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from src.core.client import ApiError
from src.core.notifier import Topic
from src.workers.base import Worker


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    reward: int | None


class AchievementsWorker(Worker):
    name = "succes"
    topic = Topic.ACHIEVEMENTS

    async def setup(self) -> None:
        self._claimed = 0   # depuis le dernier récap
        self._earned = 0

    async def step(self) -> float:
        live = self.settings.is_live("claim_achievement")
        check = await self.api.achievements_check()
        if not check.dry_run and not check.ok:
            self.log.warning("recalcul des succès refusé (HTTP %s) : %s", check.status, str(check.payload)[:200])
        pending = await self.unclaimed()
        claimed: list[tuple[Achievement, int]] = []
        for achievement in pending:
            result = await self.api.claim_achievement(achievement.id)
            if result.dry_run:
                self.log.info("[DRY RUN] succès à réclamer : %s (%s WB)", achievement.title, achievement.reward or "?")
                continue
            payload = result.payload if isinstance(result.payload, dict) else {}
            if result.ok and not payload.get("already_claimed"):
                amount = payload.get("amount") if isinstance(payload.get("amount"), int) else achievement.reward or 0
                claimed.append((achievement, amount))
            elif not result.ok:
                self.log.warning("succès « %s » refusé (HTTP %s) : %s", achievement.title, result.status,
                                 str(payload or result.payload)[:200])
            await asyncio.sleep(random.uniform(2, 5))
        if claimed:
            total = sum(amount for _, amount in claimed)
            self._claimed += len(claimed)
            self._earned += total
            self.log.info("%d succès réclamé(s), +%d WB", len(claimed), total)
            self.notifier.success("Succès réclamés", "\n".join(f"• {a.title} : +{n} WB" for a, n in claimed),
                                  live=live, wikibidous=f"+{total}")
        elif pending and live:
            self.log.info("%d succès à réclamer, aucun accepté", len(pending))
        minutes = self.settings.achievements_interval_minutes
        return random.uniform(0.8, 1.2) * minutes * 60

    async def unclaimed(self) -> list[Achievement]:
        """Succès débloqués et pas encore réclamés, avec leur titre et leur récompense."""
        client = self.ctx.client
        status, rows = await client.supabase("GET", "user_achievements", params={
            "select": "achievement_id", "user_id": f"eq.{self.api.user_id}",
            "unlocked_at": "not.is.null", "claimed_at": "is.null"})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, "supabase:user_achievements")
        ids = [str(r["achievement_id"]) for r in rows if isinstance(r, dict) and r.get("achievement_id")]
        if not ids:
            return []
        status, info = await client.supabase("GET", "achievements", params={
            "select": "id,title,wikibidous_reward", "id": f"in.({','.join(ids)})"})
        details = {str(r.get("id")): r for r in info if isinstance(r, dict)} if status == 200 and \
            isinstance(info, list) else {}
        return [Achievement(i, str(details.get(i, {}).get("title") or i),
                            details.get(i, {}).get("wikibidous_reward")) for i in ids]

    def recap(self) -> list[str]:
        claimed, earned, self._claimed, self._earned = self._claimed, self._earned, 0, 0
        return [f"{claimed} succès réclamé(s), +{earned} WB" if claimed else "Aucun succès à réclamer"]
