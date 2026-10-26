"""Réglages changés sur Discord (/plafond-collection…) : gardés en base (kv « overrides ») et appliqués au démarrage
par-dessus les variables d'environnement, sans redéployer. Seuls les réglages de ALLOWED peuvent l'être."""

from __future__ import annotations

from typing import Any

from src.workers.base import Context

KV_KEY = "overrides"
ALLOWED = {"collection_max_price"}


async def load(ctx: Context) -> dict[str, Any]:
    """Applique les réglages enregistrés ; renvoie ceux appliqués."""
    saved = {k: v for k, v in (await ctx.db.kv_get(KV_KEY, {})).items() if k in ALLOWED}
    for name, value in saved.items():
        setattr(ctx.settings, name, value)
    return saved


async def set_value(ctx: Context, name: str, value: Any) -> Any:
    """Change un réglage tout de suite et pour les démarrages suivants ; renvoie l'ancienne valeur."""
    if name not in ALLOWED:
        raise ValueError(f"réglage {name} non modifiable")
    old = getattr(ctx.settings, name)
    setattr(ctx.settings, name, value)
    saved = await ctx.db.kv_get(KV_KEY, {})
    saved[name] = value
    await ctx.db.kv_set(KV_KEY, saved)
    return old
