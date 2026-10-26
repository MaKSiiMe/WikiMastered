"""Pauses demandées sur Discord (/pause, /reprise) : un worker en pause ne fait plus aucun passage.

Gardées en base (kv « pauses » : nom du worker -> fin, ou None jusqu'à /reprise) pour survivre à un
redémarrage, et en mémoire dans le contexte (lues à chaque passage, sans requête). La garde des sanctions
n'est jamais mise en pause.
"""

from __future__ import annotations

from datetime import datetime

from src.core.timeutils import utcnow
from src.workers.base import Context

KV_KEY = "pauses"
NEVER_PAUSED = ("garde",)


async def load(ctx: Context) -> None:
    saved = await ctx.db.kv_get(KV_KEY, {})
    ctx.pauses.clear()
    now = utcnow()
    for name, until in saved.items():
        end = datetime.fromisoformat(until) if until else None
        if end is None or end > now:  # une pause finie pendant l'arrêt du bot est oubliée
            ctx.pauses[name] = end


async def _save(ctx: Context) -> None:
    await ctx.db.kv_set(KV_KEY, {name: until.isoformat() if until else None for name, until in ctx.pauses.items()})


async def pause(ctx: Context, names: list[str], until: datetime | None) -> list[str]:
    """Met ces workers en pause jusqu'à `until` (None : jusqu'à /reprise) ; renvoie ceux mis en pause."""
    names = [n for n in names if n not in NEVER_PAUSED]
    ctx.pauses.update(dict.fromkeys(names, until))
    await _save(ctx)
    return names


async def resume(ctx: Context, names: list[str] | None = None) -> list[str]:
    """Reprend ces workers (tous si None) ; renvoie ceux qui étaient en pause."""
    resumed = [n for n in list(ctx.pauses) if names is None or n in names]
    for name in resumed:
        del ctx.pauses[name]
    await _save(ctx)
    return resumed


