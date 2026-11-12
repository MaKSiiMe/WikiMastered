"""Liste de souhaits du site : le site annonce lui-même la mise en vente d'une carte manquante.

Avec COLLECTION_WISHLIST, les cartes manquantes sont mises en liste de souhaits (table Supabase `wishlist_items`).
Le site envoie alors une notification `marketplace_wishlist_listed` à chaque mise en vente (moins de 10 s, testé à la
main le 04/11/2026). Le sniper lit ces notifications au lieu de chercher ces cartes une par une par leur titre : à
~30 titres par heure, 1 500 cartes demandaient plus de deux jours par tour.

Seules les cartes ajoutées par le bot sont retirées ensuite : celles que le joueur a mises lui-même restent.
Les notifications de ces cartes sont marquées comme lues, pour ne pas noyer les autres dans la cloche du site.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from src.core.client import ApiError, RateLimited, UncertainMutation
from src.workers.base import Context
from src.core.timeutils import utcnow
from src.workers.collection import collection_key, targets

log = logging.getLogger(__name__)

KV_KEY = "collection_wishlist"
CURSOR_KEY = "collection_wishlist_cursor"  # à part : le sniper l'écrit pendant que le worker collection écrit KV_KEY
WRITE = "wishlist_write"
BACKLOG_KEY = "collection_wishlist_backlog"  # notifications déjà accumulées : marquées une seule fois
LISTED = "marketplace_wishlist_listed"
COLLECTION_TYPES = f"in.({LISTED},marketplace_outbid,marketplace_auction_won)"
LOOKUP_BATCH = 50    # titres par recherche : la liste passe dans l'adresse
WRITE_BATCH = 200
PAGE = 1000          # PostgREST ne renvoie pas plus par défaut
NOTIFICATIONS_PER_READ = 100
BACKLOG_MAX_PAGES = 20


def _quoted(values: Iterable[str]) -> str:
    """Liste `in.(…)` de PostgREST : chaque valeur entre guillemets, à cause des virgules et parenthèses des titres."""
    return "(" + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values) + ")"


def _chunks(items: list, size: int) -> Iterable[list]:
    return (items[i:i + size] for i in range(0, len(items), size))


async def _resolve(ctx: Context, titles: dict[str, str], ids: dict[str, str | None]) -> None:
    """Complète `ids` (clé de titre -> id de carte). Sans carte à ce titre : None, pour ne pas rechercher à chaque fois."""
    unknown = [k for k in titles if k not in ids]
    for keys in _chunks(unknown, LOOKUP_BATCH):
        status, rows = await ctx.client.supabase(
            "GET", "cards", background=True,
            params={"select": "id,wikipedia_title", "wikipedia_title": "in." + _quoted(titles[k] for k in keys)})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, "supabase:cards")
        found = {collection_key(str(r.get("wikipedia_title"))): r.get("id") for r in rows}
        ids.update({k: found.get(k) for k in keys})


async def current(ctx: Context) -> set[str]:
    """Cartes déjà dans la liste de souhaits du joueur."""
    out: set[str] = set()
    while True:
        status, rows = await ctx.client.supabase("GET", "wishlist_items", background=True, params={
            "select": "card_id", "user_id": f"eq.{ctx.client.user_id}", "order": "card_id",
            "limit": str(PAGE), "offset": str(len(out))})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, "supabase:wishlist_items")
        out.update(str(r["card_id"]) for r in rows)
        if len(rows) < PAGE:
            return out


async def sync(ctx: Context, missing: list[str], titles: dict[str, str]) -> dict[str, Any]:
    """Aligne la liste de souhaits sur les cartes manquantes. Renvoie ce qui a changé et, si besoin, l'erreur du site."""
    state = await ctx.db.kv_get(KV_KEY) or {}
    ids: dict[str, str | None] = state.get("ids") or {}
    added: dict[str, str] = state.get("added") or {}  # clé -> id : ajoutées par le bot
    result: dict[str, Any] = {"added": 0, "removed": 0, "error": ""}
    live = ctx.settings.is_live(WRITE)
    wanted = {k: titles.get(k, k) for k in missing}
    try:
        await _resolve(ctx, wanted, ids)
        have = await current(ctx)
        targets = {k: ids[k] for k in wanted if ids.get(k)}
        for rows in _chunks([(k, c) for k, c in targets.items() if c not in have], WRITE_BATCH):
            if not live:
                log.info("[DRY RUN] %d carte(s) en liste de souhaits", len(rows))
                continue
            status, data = await ctx.client.supabase(
                "POST", "wishlist_items", params={"on_conflict": "user_id,card_id"},
                prefer="resolution=ignore-duplicates,return=minimal",
                json_body=[{"user_id": ctx.client.user_id, "card_id": c} for _, c in rows])
            if status not in (200, 201, 204):
                result["error"] = f"ajout refusé (HTTP {status}) : {str(data)[:200]}"
                break
            added.update(rows)
            have.update(c for _, c in rows)
            result["added"] += len(rows)
        # Plus manquantes : retirées, sinon le site continue de les annoncer pour rien.
        stale = [k for k in added if k not in targets]
        for keys in _chunks(stale, LOOKUP_BATCH):
            if not live or result["error"]:
                break
            status, data = await ctx.client.supabase(
                "DELETE", "wishlist_items", prefer="return=minimal",
                params={"user_id": f"eq.{ctx.client.user_id}", "card_id": "in." + _quoted(added[k] for k in keys)})
            if status not in (200, 204):
                result["error"] = f"retrait refusé (HTTP {status}) : {str(data)[:200]}"
                break
            for k in keys:
                have.discard(added.pop(k))
            result["removed"] += len(keys)
    except ApiError as exc:
        result["error"] = f"liste de souhaits illisible : {exc}"
        have = set()
        targets = {}
    # Couvertes : le site les annoncera, inutile de les chercher par leur titre.
    covered = sorted(k for k, c in targets.items() if c in have) if live else []
    result["covered"] = len(covered)
    result["unknown"] = sum(1 for k in wanted if k in ids and not ids[k])
    await ctx.db.kv_set(KV_KEY, {**state, "ids": ids, "added": added, "covered": covered})
    return result


def _of_collection(ctx: Context, rows: list[dict], by_id: dict[str, str]) -> list[str]:
    """Notifications des cartes suivies par les collections : celles des autres cartes restent non lues."""
    keys = set(targets(ctx))
    out = []
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        key = by_id.get(str(data.get("card_id"))) or collection_key(str(data.get("card_title") or ""))
        if key in keys and row.get("id"):
            out.append(str(row["id"]))
    return out


async def _mark_read(ctx: Context, ids: list[str]) -> bool:
    """Échec sans conséquence : la cloche garde juste quelques notifications de plus."""
    if not ids:
        return True
    try:
        result = await ctx.api.mark_notifications_read(ids)
    except (ApiError, RateLimited, UncertainMutation) as exc:
        log.warning("notifications de collection non marquées comme lues : %s", exc)
        return False
    return result.dry_run or result.ok


async def clear_backlog(ctx: Context) -> int:
    """Une seule fois : marque comme lues les notifications de collection déjà accumulées. Renvoie leur nombre."""
    if await ctx.db.kv_get(BACKLOG_KEY) or not ctx.settings.is_live("mark_notifications_read"):
        return 0
    by_id = {c: k for k, c in ((await ctx.db.kv_get(KV_KEY) or {}).get("ids") or {}).items() if c}
    marked = kept = 0
    for _ in range(BACKLOG_MAX_PAGES):
        # Les marquées sortent du filtre : on saute seulement celles laissées non lues.
        status, rows = await ctx.client.supabase("GET", "notifications", background=True, params={
            "select": "id,type,data", "user_id": f"eq.{ctx.client.user_id}", "type": COLLECTION_TYPES,
            "read": "eq.false", "order": "created_at.desc,id", "limit": str(NOTIFICATIONS_PER_READ),
            "offset": str(kept)})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, "supabase:notifications")
        ids = _of_collection(ctx, rows, by_id)
        if not await _mark_read(ctx, ids):
            return marked  # réessayé au prochain passage
        marked += len(ids)
        kept += len(rows) - len(ids)
        if len(rows) < NOTIFICATIONS_PER_READ:
            await ctx.db.kv_set(BACKLOG_KEY, True)
            return marked
    return marked


async def covered(ctx: Context) -> set[str]:
    return set((await ctx.db.kv_get(KV_KEY) or {}).get("covered") or [])


async def new_listings(ctx: Context, wanted: set[str], now: datetime | None = None,
                       extra: dict[str, str] | None = None) -> list[str]:
    """Ventes annoncées depuis la dernière lecture, pour les cartes de `wanted`. Le curseur est gardé en base : un
    redémarrage ne perd pas les annonces arrivées entre-temps. `extra` : id de carte -> clé de titre des cartes
    souhaitées (src/workers/wishes.py), que l'état des collections ne connaît pas."""
    known = await ctx.db.kv_get(CURSOR_KEY)
    cursor = known or (now or utcnow()).isoformat()
    status, rows = await ctx.client.supabase("GET", "notifications", params={
        "select": "id,type,created_at,data", "user_id": f"eq.{ctx.client.user_id}", "type": COLLECTION_TYPES,
        "created_at": f"gt.{cursor}", "order": "created_at.asc", "limit": str(NOTIFICATIONS_PER_READ)})
    if status != 200 or not isinstance(rows, list):
        raise ApiError(status, rows, "supabase:notifications")
    by_id = {c: k for k, c in ((await ctx.db.kv_get(KV_KEY) or {}).get("ids") or {}).items() if c}
    by_id.update(extra or {})
    auctions: list[str] = []
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        key = by_id.get(str(data.get("card_id"))) or collection_key(str(data.get("card_title") or ""))
        if row.get("type") == LISTED and key in wanted and data.get("auction_id"):
            auctions.append(str(data["auction_id"]))
    await _mark_read(ctx, _of_collection(ctx, rows, by_id))
    if rows:
        cursor = str(rows[-1]["created_at"])
    if cursor != known:
        await ctx.db.kv_set(CURSOR_KEY, cursor)
    return list(dict.fromkeys(auctions))
