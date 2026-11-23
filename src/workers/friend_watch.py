"""Worker « friends » : prévient sur Discord quand un ami possède une carte de ta liste de souhaits (src/friends.py).

Toutes les FRIENDS_CHECK_HOURS (0 = coupé), il relit le catalogue de ta liste de souhaits, qui donne les amis possédant
chaque carte et leurs raretés. Seules les cartes nouvelles chez un ami depuis le passage précédent sont annoncées ; le
premier passage annonce tout. Lecture seule : l'échange se propose à la main sur le site. Salon « ventes », comme les
échanges, sans message privé.
"""

from __future__ import annotations

import random

from src import friends
from src.core.notifier import Topic
from src.workers.base import Worker

NOTIFIED_KEY = "friends_wishlist_notified"


class FriendWatch(Worker):
    name = "friends"
    topic = Topic.TRADES

    async def step(self) -> float:
        state = await friends.overview(self.ctx, refresh=True)
        current = friends.pairs(state)
        new = current - set(await self.db.kv_get(NOTIFIED_KEY) or [])
        if new:
            self.log.info("%d carte(s) de la liste de souhaits nouvelles chez des amis", len(new))
            self.notifier.info("Cartes de ta liste chez tes amis", friends.news_text(state, new))
        # Une carte qu'un ami n'a plus sort de la liste : annoncée de nouveau s'il la récupère.
        await self.db.kv_set(NOTIFIED_KEY, sorted(current))
        return random.uniform(0.9, 1.1) * self.settings.friends_check_hours * 3600
