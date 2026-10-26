"""Module J : valeur du compte (/valeur), tenue à jour sans dépendre de la défausse ni de la vente des doublons.

Toutes les ACCOUNT_VALUE_HOURS heures, si aucun autre worker n'a relu toute la collection entre-temps (cotes,
inventaire : leur lecture compte), ce worker la relit en requêtes de fond (le sniper passe toujours avant), garde
sa valeur (src/account.py) et note la valeur du compte du jour, pour l'évolution. Aucun message.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src import account
from src.core.notifier import Topic
from src.core.timeutils import utcnow
from src.workers.base import Worker


class AccountValue(Worker):
    name = "valeur"
    topic = Topic.SALES

    async def step(self) -> float:
        every = timedelta(hours=self.settings.account_value_hours)
        snapshot = await self.db.kv_get(account.KV_KEY)
        age = utcnow() - datetime.fromisoformat(snapshot["at"]) if snapshot else None
        if age is None or age >= every:
            snapshot = await account.store_collection(self.ctx, await self.api.load_collection(background=True))
            value = await account.account_value(self.ctx, snapshot)
            self.log.info("collection : %d exemplaires, %d WB ; compte : %d WB", snapshot["copies"],
                          snapshot["value"], value["total"])
            age = timedelta(0)
        return max(60.0, (every - age).total_seconds())
