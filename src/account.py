"""Valeur du compte en WB (/valeur sur Discord) : solde + cartes possédées + cartes en vente + mises en tête.

Lire toute la collection (≈ 5 600 exemplaires, 110 pages, le 27/10) prend plusieurs minutes : sa valeur est gardée
en base (kv « collection_value ») à chaque lecture complète faite par un worker (cotes, inventaire, et le worker
« valeur » qui la relit toutes les ACCOUNT_VALUE_HOURS si personne d'autre ne l'a fait), et relue à la demande
seulement si elle date (COLLECTION_VALUE_MAX_AGE) ou sur le bouton « Recompter ». Rien n'est coté sur le
site pour cela : chaque exemplaire vaut l'estimation du bot sans requête (ventes observées, cote du site en cache),
sinon sa cote figée ; sans aucune des deux, il compte 0 et est compté à part.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.config import RARITY_RANK
from src.core.models import OwnedCard
from src.core.pricing import combine, trusted
from src.core.timeutils import now_local, utcnow
from src.roi import wb
from src.workers.base import Context

KV_KEY = "collection_value"
HISTORY_KEY = "account_value_history"  # total du compte par jour (Paris), pour l'évolution
COLLECTION_VALUE_MAX_AGE = timedelta(hours=12)
TOP_SHOWN = 5
HISTORY_DAYS = 60


async def copy_values(ctx: Context, rows: list[OwnedCard]) -> list[tuple[OwnedCard, float | None]]:
    """Valeur de chaque exemplaire, sans requête au site : l'estimation du bot (médiane des dernières ventes
    observées, plafonnée par la cote du site en cache ; la cote seule sans vente ; un shiny jamais vu vendu vaut la
    carte ordinaire), sinon la cote figée. None : rien de connu. Requêtes groupées : ≈ 1 s pour 5 600 exemplaires."""
    ids = {r.catalog_id for r in rows}
    observed = await ctx.db.observed_for_cards(ids, ctx.prices.recent_n)
    site = await ctx.db.site_averages(ids, utcnow() - ctx.prices.default_ttl)  # comme l'estimation du bot
    frozen = await ctx.db.frozen_values()
    out = []
    for row in rows:
        rarity = row.rarity or ""
        sales = trusted(observed.get((row.catalog_id, row.is_shiny), {})).get(rarity)
        if row.is_shiny and not sales:
            sales = trusted(observed.get((row.catalog_id, False), {})).get(rarity)
        average = site.get(row.catalog_id, {}).get(rarity)
        value = combine(*(sales or ([], 0)), average).average
        out.append((row, value if value is not None else frozen.get(row.catalog_id, {}).get(rarity)))
    return out


async def store_collection(ctx: Context, rows: list[OwnedCard]) -> dict:
    """Valeur de la collection lue (hors exemplaires en vente, comptés à part), gardée en base."""
    selling = await ctx.db.scheduled_owned_ids()
    values = [(r, v) for r, v in await copy_values(ctx, rows) if r.id not in selling]
    by_rarity: dict[str, list[float]] = {}
    for r, v in values:
        entry = by_rarity.setdefault(r.rarity or "?", [0, 0.0])
        entry[0] += 1
        entry[1] += v or 0
    top = sorted(((v, r.title, r.rarity or "?", r.is_shiny) for r, v in values if v), reverse=True)[:TOP_SHOWN]
    snapshot = {
        "at": utcnow().isoformat(), "copies": len(values), "value": round(sum(v or 0 for _, v in values)),
        "unknown": sum(1 for _, v in values if v is None),
        "by_rarity": {k: [n, round(v)] for k, (n, v) in sorted(by_rarity.items(),
                                                               key=lambda kv: -RARITY_RANK.get(kv[0], -1))},
        "top": [[round(v), title, rarity, shiny] for v, title, rarity, shiny in top],
    }
    await ctx.db.kv_set(KV_KEY, snapshot)
    return snapshot


async def collection_snapshot(ctx: Context, *, refresh: bool = False) -> dict:
    """Dernière valeur de la collection ; relue sur le site (en requêtes de fond) si absente, trop vieille ou
    demandée."""
    snapshot = await ctx.db.kv_get(KV_KEY)
    if refresh or not snapshot or utcnow() - datetime.fromisoformat(snapshot["at"]) > COLLECTION_VALUE_MAX_AGE:
        snapshot = await store_collection(ctx, await ctx.api.load_collection(background=True))
    return snapshot


async def account_value(ctx: Context, snapshot: dict, balance: int | None = None) -> dict:
    """Solde, collection, cartes en vente (au prix de départ) et mises en tête (WB bloqués), puis le total, gardé
    par jour pour l'évolution. `balance` : déjà lu par l'appelant (bilan du soir), sinon lu sur le site."""
    if balance is None:
        balance = await ctx.api.balance()
    listings = [r for r in await ctx.db.open_listings() if r["status"] in ("posted", "uncertain")]
    me = (ctx.client.user_id or "").lower()
    leading = [a for a in await ctx.api.my_bids()
               if a.status == "active" and me and (a.current_bidder_id or "").lower() == me]
    parts = {"balance": balance, "cards": snapshot["value"],
             "selling": sum(r["base_amount"] for r in listings), "bids": sum(a.current_bid or 0 for a in leading)}
    total = sum(parts.values())
    history = await ctx.db.kv_get(HISTORY_KEY, {})
    day = now_local().date().isoformat()  # un jour (Paris) = une valeur, la dernière calculée
    previous = [(d, v) for d, v in sorted(history.items()) if d < day]
    history[day] = total
    await ctx.db.kv_set(HISTORY_KEY, dict(sorted(history.items())[-HISTORY_DAYS:]))
    return {**parts, "total": total, "listings": len(listings), "leading": len(leading),
            "previous": previous[-1] if previous else None}


async def bilan_text(ctx: Context, balance: int | None) -> str | None:
    """Bloc « Compte » du bilan du soir : total et évolution, d'après la dernière collection comptée (jamais relue
    pour le bilan : None si elle a plus de 24 h ou n'existe pas)."""
    snapshot = await ctx.db.kv_get(KV_KEY)
    if not snapshot or utcnow() - datetime.fromisoformat(snapshot["at"]) > timedelta(hours=24):
        return None
    value = await account_value(ctx, snapshot, balance)
    change = ""
    if value["previous"]:
        day, before = value["previous"]
        change = f" ({wb(value['total'] - before, True)} WB depuis le {day[8:10]}/{day[5:7]})"
    return (f"**{wb(value['total'])} WB**{change}\nSolde {wb(value['balance'])} · cartes {wb(value['cards'])} · "
            f"en vente {wb(value['selling'])} · mises en tête {wb(value['bids'])}")
