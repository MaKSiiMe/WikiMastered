"""Valeur du compte en WB (/valeur sur Discord) : solde + cartes possédées + cartes en vente + mises en tête.

Lire toute la collection (≈ 5 600 exemplaires, 110 pages, le 27/10) prend plusieurs minutes : sa valeur est gardée
en base (kv « collection_value ») à chaque lecture complète faite par un worker (cotes, inventaire, et le worker
« valeur » qui la relit toutes les ACCOUNT_VALUE_HOURS si personne d'autre ne l'a fait), et relue à la demande
seulement si elle date (COLLECTION_VALUE_MAX_AGE) ou sur le bouton « Recompter ». Rien n'est coté sur le
site pour cela : chaque exemplaire vaut l'estimation du bot sans requête (ventes observées, cote du site en cache),
sinon sa cote figée ; sans aucune des deux, il compte 0 et est compté à part.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from aiosqlite import Row

from src.config import RARITY_RANK
from src.core.models import OwnedCard
from src.core.pricing import combine, trusted
from src.core.timeutils import now_local, utcnow
from src.roi import wb
from src.workers.base import Context

KV_KEY = "collection_value"
HISTORY_KEY = "account_value_history"  # total du compte par jour (Paris), pour l'évolution
COLLECTION_VALUE_MAX_AGE = timedelta(hours=12)
WAITING_LISTINGS = ("scheduled", "waiting")  # l'exemplaire est encore dans la collection
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
        value = combine(*(sales or ([], 0)), average, site_margin=ctx.prices.site_margin).average
        out.append((row, value if value is not None else frozen.get(row.catalog_id, {}).get(rarity)))
    return out


async def real_listings(ctx: Context) -> list[Row]:
    """Ventes réelles comptées à part de la collection : en cours ou qui attendent une place."""
    return [r for r in await ctx.db.open_listings() if r["status"] != "simulated"]


def not_on_sale(rows: list[OwnedCard], listings: list[Row]) -> list[OwnedCard]:
    """Exemplaires hors ventes. Une invendue revient souvent sous un autre identifiant, relevé seulement à sa publication
    (02/11 : George V comptée deux fois) : une vente en attente dont l'identifiant a disparu retire un exemplaire de la
    même carte et de la même rareté."""
    on_sale = {r["owned_card_id"] for r in listings}
    present = {r.id for r in rows}
    returned = Counter((r["card_id"], r["rarity"]) for r in listings
                       if r["status"] in WAITING_LISTINGS and r["owned_card_id"] not in present)
    out = []
    for row in rows:
        if row.id in on_sale:
            continue
        if returned[(row.catalog_id, row.rarity)] > 0:
            returned[(row.catalog_id, row.rarity)] -= 1
            continue
        out.append(row)
    return out


async def store_collection(ctx: Context, rows: list[OwnedCard]) -> dict:
    """Valeur de la collection lue (hors exemplaires en vente, comptés à part), gardée en base."""
    values = await copy_values(ctx, not_on_sale(rows, await real_listings(ctx)))
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
    """Solde, collection, cartes en vente ou qui attendent une place (au prix de départ) et mises en tête (WB bloqués),
    puis le total, gardé par jour pour l'évolution. `balance` : déjà lu par l'appelant (bilan du soir), sinon lu sur le
    site."""
    if balance is None:
        balance = await ctx.api.balance()
    listings = await real_listings(ctx)
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
    return {**parts, "total": total, "listings": len(listings),
            "waiting": sum(r["status"] in WAITING_LISTINGS for r in listings), "leading": len(leading),
            "previous": previous[-1] if previous else None}


async def bilan_text(ctx: Context, balance: int | None) -> tuple[str, list[tuple[str, str]]] | None:
    """Haut du bilan du soir : total du compte et évolution, puis solde, cartes, ventes (et mises en tête) côte à côte,
    d'après la dernière collection comptée (jamais relue pour le bilan : None si elle a plus de 24 h ou n'existe
    pas)."""
    snapshot = await ctx.db.kv_get(KV_KEY)
    if not snapshot or utcnow() - datetime.fromisoformat(snapshot["at"]) > timedelta(hours=24):
        return None
    value = await account_value(ctx, snapshot, balance)
    change = ""
    if value["previous"]:
        day, before = value["previous"]
        change = f" ({wb(value['total'] - before, True)} WB depuis le {day[8:10]}/{day[5:7]})"
    columns = [("Solde", f"{wb(value['balance'])} WB"), ("Cartes", f"{wb(value['cards'])} WB"),
               ("En vente", f"{wb(value['selling'])} WB")]
    if value["bids"]:
        columns.append(("Mises en tête", f"{wb(value['bids'])} WB"))
    return f"Compte : **{wb(value['total'])} WB**{change}", columns
