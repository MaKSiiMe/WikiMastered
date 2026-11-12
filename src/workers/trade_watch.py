"""Échanges proposés : chacun est évalué une fois (src/trades.py) et annoncé sur Discord, avec la contre-offre à
envoyer s'il est défavorable.

Toutes les TRADE_CHECK_MINUTES min, relit les échanges actifs (/api/trades?active=1, requête de fond). Un échange en
attente dont tu es le destinataire (on te le propose, contre-offres comprises) est annoncé une seule fois, dans le salon
principal. Le bot ne répond jamais à ta place : il n'existe pas de route connue pour accepter, refuser ou contrer.

Chaque échange en attente, proposé par toi ou reçu, est aussi gardé avec la valeur de chaque côté (kv « trades_log »),
puis son statut final quand il n'est plus en cours : c'est la base du rendement de tes échanges.
"""

from __future__ import annotations

import random
from datetime import datetime

from src import trades
from src.core.client import ApiError, RateLimited
from src.core.models import TradesList
from src.core.timeutils import utcnow
from src.core.notifier import Topic
from src.workers.base import Worker

KV_KEY = "trades_seen"  # échanges déjà annoncés (seulement ceux encore actifs)
LOG_KEY = "trades_log"  # tes échanges, proposés ou reçus, avec la valeur de chaque côté : id -> src.trades.snapshot
LOG_MAX = 300
BACKFILL_KEY = "trades_backfilled"  # anciens échanges repris une fois
BACKFILL_MAX = 40  # échanges terminés repris au plus (chaque carte coûte une ou deux requêtes de fond)


class TradeWatch(Worker):
    name = "echanges"
    topic = Topic.TRADES

    async def setup(self) -> None:
        self._shape_logged = False
        self._ended_logged = False

    async def step(self) -> float:
        raw = await self.ctx.client.get("active_trades", background=True)
        listed = TradesList.model_validate(raw)
        # Champs réels d'une carte d'échange, à relire dans les logs : le 30/10, le 1er échange n'en avait aucune
        # (« d'une carte : [] ») et celles de jarry0 restaient illisibles (« ? · ? »).
        items = [i for t in (raw.get("trades") or [] if isinstance(raw, dict) else []) if isinstance(t, dict)
                 for i in t.get("items") or []]
        if items and not self._shape_logged:
            self.log.info("carte d'un échange : %s", str(items[0])[:1500])
            self._shape_logged = True
        me = (self.ctx.client.user_id or "").lower()
        seen = set(await self.db.kv_get(KV_KEY) or [])
        history: dict[str, dict] = await self.db.kv_get(LOG_KEY) or {}
        active = {t.id for t in listed.trades if t.id}
        for trade in listed.trades:
            if not trade.id or trade.status != "pending" or not me:
                continue
            received = (trade.recipient_id or "").lower() == me
            involved = received or (trade.initiator_id or "").lower() == me
            announce = received and trade.id not in seen  # proposé par toi : gardé, rien à annoncer
            if not announce and (trade.id in history or not involved):
                continue
            e = await trades.evaluate_trade(self.ctx, trade, me, offers=announce)
            if involved:
                history.setdefault(trade.id, trades.snapshot(trade, e, me, utcnow().isoformat()))
            if not announce:
                continue
            seen.add(trade.id)
            who = (trade.initiator or {}).get("username") or "un joueur"
            self.notifier.send(trades.LEVELS[e.verdict], f"Échange proposé par {who}", e.headline,
                               columns=e.columns, sections=[("\u200b", e.notes)] if e.notes else [],
                               url=f"{self.ctx.client.endpoints.base_url}/trades", important=True,
                               alert=True)  # à accepter ou refuser sur le site
        await self._close_ended(history, active)
        if me and not await self.db.kv_get(BACKFILL_KEY):
            await self._backfill(history, me)
        await self.db.kv_set(KV_KEY, sorted(seen & active))
        await self.db.kv_set(LOG_KEY, dict(sorted(history.items(), key=lambda kv: kv[1]["seen"])[-LOG_MAX:]))
        minutes = self.settings.trade_check_minutes
        return random.uniform(0.8, 1.2) * minutes * 60

    async def _close_ended(self, history: dict[str, dict], active: set[str]) -> None:
        """Échanges qui ne sont plus en cours : statut final relu sur le site (accepté, refusé, annulé…), sinon
        « ended ». Le premier est écrit en entier dans les logs : ses champs réels restent à relever."""
        ended = [t for tid, t in history.items() if t["status"] == "pending" and tid not in active]
        if not ended:
            return
        final: dict[str, dict] = {}
        try:
            raw = await self.ctx.client.get("trades", background=True)
            final = {str(t.get("id")): t for t in (raw.get("trades") or [] if isinstance(raw, dict) else [])
                     if isinstance(t, dict)}
        except (ApiError, RateLimited) as exc:
            self.log.info("échanges terminés illisibles (%s) : statut final inconnu", exc)
        for t in ended:
            found = final.get(t["id"])
            status = str((found or {}).get("status") or "")
            t["status"] = status if status and status != "pending" else "ended"
            t["ended"] = utcnow().isoformat()
            self.log.info("échange avec %s terminé (%s) : donné ~%d WB, reçu ~%d WB", t["with"], t["status"],
                          t["given_total"], t["received_total"])
            if found and not self._ended_logged:
                self._ended_logged = True
                self.log.info("échange terminé, champs : %s", str({k: v for k, v in found.items()
                                                                  if k != "items"})[:1500])

    async def _backfill(self, history: dict[str, dict], me: str) -> None:
        """Une seule fois : tes échanges terminés avant le suivi (12/11), si le site les donne encore. Chaque carte
        est comptée à sa valeur de la date de l'échange quand ses ventes de l'époque sont encore en base, sinon à
        l'estimation d'aujourd'hui ; la rareté d'une carte qui a quitté ta collection sans passer par le bot reste
        inconnue. Route illisible : nouvel essai au passage suivant."""
        try:
            raw = await self.ctx.client.get("trades", background=True)
        except (ApiError, RateLimited) as exc:
            self.log.info("anciens échanges illisibles (%s) : nouvel essai au prochain passage", exc)
            return
        rows = [t for t in (raw.get("trades") or [] if isinstance(raw, dict) else []) if isinstance(t, dict)]
        old = [t for t in rows if t.get("id") and t["id"] not in history and str(t.get("status") or "") != "pending"
               and me in (str(t.get("initiator_id") or "").lower(), str(t.get("recipient_id") or "").lower())]
        old.sort(key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""), reverse=True)
        done = 0
        for row in old[:BACKFILL_MAX]:
            when = str(row.get("updated_at") or row.get("created_at") or "")
            try:
                at = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            trade = TradesList.model_validate({"trades": [row]}).trades[0]
            e = await trades.evaluate_trade(self.ctx, trade, me, offers=False, at=at)
            kept = trades.snapshot(trade, e, me, at.isoformat(), status=str(row.get("status") or "ended"))
            history[trade.id] = {**kept, "ended": at.isoformat(), "retro": True}
            done += 1
        await self.db.kv_set(BACKFILL_KEY, True)
        statuses = sorted({str(t.get("status")) for t in rows})
        self.log.info("anciens échanges : %d repris sur %d donnés par le site (statuts vus : %s)", done, len(rows),
                      ", ".join(statuses) or "aucun")

