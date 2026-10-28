"""Échanges proposés : chacun est évalué une fois (src/trades.py) et annoncé sur Discord, avec la contre-offre à
envoyer s'il est défavorable.

Toutes les TRADE_CHECK_MINUTES min, relit les échanges actifs (/api/trades?active=1, requête de fond). Un échange en
attente dont tu es le destinataire (on te le propose, contre-offres comprises) est annoncé une seule fois, dans le salon
principal. Le bot ne répond jamais à ta place : il n'existe pas de route connue pour accepter, refuser ou contrer.
"""

from __future__ import annotations

import random

from src import trades
from src.core.models import TradesList
from src.core.notifier import Level, Topic
from src.workers.base import Worker

KV_KEY = "trades_seen"  # échanges déjà annoncés (seulement ceux encore actifs)
LEVELS = {"equitable": Level.INFO, "avantage": Level.SUCCESS, "defavorable": Level.WARNING}


class TradeWatch(Worker):
    name = "echanges"
    topic = Topic.TRADES

    async def setup(self) -> None:
        self._shape_logged = False

    async def step(self) -> float:
        raw = await self.ctx.client.get("active_trades", background=True)
        listed = TradesList.model_validate(raw)
        if listed.trades and not self._shape_logged:  # champs réels d'un échange, à relire dans les logs
            first = (raw.get("trades") or [{}])[0] if isinstance(raw, dict) else {}
            item = (first.get("items") or [{}])[0] if isinstance(first, dict) else {}
            self.log.info("champs d'un échange : %s ; d'une carte : %s", sorted(first), sorted(item))
            self._shape_logged = True
        me = (self.ctx.client.user_id or "").lower()
        seen = set(await self.db.kv_get(KV_KEY) or [])
        active = {t.id for t in listed.trades if t.id}
        for trade in listed.trades:
            if not trade.id or trade.id in seen or trade.status != "pending" or not me:
                continue
            if (trade.recipient_id or "").lower() != me:
                continue  # proposé par toi : rien à évaluer
            seen.add(trade.id)
            e = await trades.evaluate_trade(self.ctx, trade, me)
            who = (trade.initiator or {}).get("username") or "un joueur"
            self.notifier.send(LEVELS[e.verdict], f"Échange proposé par {who}", f"{e.headline}\n\n{e.text}",
                               url=f"{self.ctx.client.endpoints.base_url}/trades", important=True)
        await self.db.kv_set(KV_KEY, sorted(seen & active))
        minutes = self.settings.trade_check_minutes
        return random.uniform(0.8, 1.2) * minutes * 60
