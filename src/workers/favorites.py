"""Favoris à monter en rareté : une carte que tu mets en favori est rachetée dans une rareté plus haute, jusqu'à L.

Demandé le 22/11 : mettre en favori les cartes des vitrines et des collections, pour que le bot les monte en rareté
sans chercher des centaines de titres un par un (une recherche toutes les COLLECTION_SEARCH_SECONDS, dans la file du
sniper : chaque carte vue une fois par jour au mieux).

Toutes les FAVORITES_CHECK_MINUTES, le worker « favorites » lit tes favoris dans les tables Supabase (`user_cards`,
`cards` : une poignée de requêtes de fond, aucune page de collection) :
- Chaque carte en favori sans exemplaire shiny et pas encore en L devient une demande du sniper (kv « favorites ») :
  toute rareté au-dessus de ton meilleur exemplaire, au plus à sa cote dans la rareté en vente (jamais plus que le prix
  du marché ; sans cote : pas de mise), jamais une vente shiny.
- Repérage sans recherche : le bot met ces cartes dans ta liste de souhaits du site, qui annonce leurs ventes (une
  alerte par carte et par 24 h au plus) ; le sniper voit déjà toutes les ventes des raretés de revente
  (SNIPER_RESALE_RARITIES). Il les retire de ta liste une fois en L, ou sorties de tes favoris ; tes propres souhaits
  ne sont jamais touchés.
- Favori déplacé : quand ton meilleur exemplaire n'est pas en favori mais qu'un exemplaire plus bas l'est (carte
  montée par le sniper, un paquet ou un échange), le meilleur passe en favori et les plus bas en sortent : ils
  redeviennent vendables. Un exemplaire exposé dans une vitrine reste protégé par TAG_SHOWCASE (dans TAG_PROTECTED).
Écritures : groupe « favorites » (liste de souhaits et favoris).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from src.config import RARITY_RANK
from src.core.client import ApiError
from src.core.database import Database
from src.core.notifier import Topic
from src.core.timeutils import utcnow
from src.snipes import Snipe
from src.workers import wishlist
from src.workers.base import Context, Worker
from src.workers.collection import collection_key

log = logging.getLogger(__name__)

KV_KEY = "favorites"
WRITE = "favorite_write"
# Motif des mises d'un favori : « collection » en tête, la carte est gardée comme une carte de collection.
REASON = "collection : favori"
TOP = RARITY_RANK["L"]  # au-delà : shiny (L+), pas encore visé
PAGE = 1000  # PostgREST ne renvoie pas plus par défaut
LOOKUP_BATCH = 100  # identifiants par requête (la liste passe dans l'adresse)
WRITE_BATCH = 200
MOVES_PER_RUN = 50  # favoris déplacés par passage (deux écritures ou plus chacun)
WON_MEMORY = timedelta(hours=24)  # victoire du sniper comptée en attendant que l'exemplaire arrive


async def _state(db: Database) -> dict:
    state = await db.kv_get(KV_KEY) or {}
    state.setdefault("cards", {})  # id de carte -> {title, best}
    state.setdefault("added", {})  # id de carte -> titre : mises dans ta liste de souhaits par ce worker
    return state


async def load(db: Database) -> dict[str, Snipe]:
    """Favoris à monter, par clé de titre, comme des demandes du sniper."""
    out: dict[str, Snipe] = {}
    for card in (await _state(db))["cards"].values():
        best = int(card.get("best", -1))
        if card.get("title") and best < TOP:
            snipe = Snipe(card["title"], 0, favorite=True, above=best)
            out[snipe.key] = snipe
    return out


async def card_ids(db: Database) -> dict[str, str]:
    """Id de carte -> clé de titre, pour reconnaître les ventes annoncées par le site."""
    return {cid: collection_key(card["title"]) for cid, card in (await _state(db))["cards"].items()
            if card.get("title") and int(card.get("best", -1)) < TOP}


async def added_ids(db: Database) -> set[str]:
    """Cartes mises dans ta liste de souhaits par ce worker : pas des souhaits à toi."""
    return set((await _state(db))["added"])


async def record_win(db: Database, title: str, rarity: str | None) -> None:
    """Favori monté par le sniper : sa nouvelle rareté compte tout de suite, pour ne pas miser sur la même ; gardée
    WON_MEMORY, le temps que l'exemplaire arrive dans la collection."""
    state = await _state(db)
    key = collection_key(title)
    won = RARITY_RANK.get(rarity or "", -1)
    for card in state["cards"].values():
        if card.get("title") and collection_key(card["title"]) == key:
            card["best"] = max(int(card.get("best", -1)), won)
            card["won"], card["won_at"] = max(int(card.get("won", -1)), won), utcnow().isoformat()
    await db.kv_set(KV_KEY, state)


def _chunks(items: list, size: int) -> Iterable[list]:
    return (items[i:i + size] for i in range(0, len(items), size))


async def _select(ctx: Context, table: str, params: dict[str, str]) -> list[dict]:
    """Toutes les lignes d'une table Supabase (pages de PAGE lignes)."""
    out: list[dict] = []
    while True:
        status, rows = await ctx.client.supabase("GET", table, background=True, params={
            **params, "order": "id", "limit": str(PAGE), "offset": str(len(out))})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, f"supabase:{table}")
        out += [r for r in rows if isinstance(r, dict)]
        if len(rows) < PAGE:
            return out


async def read(ctx: Context) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """(exemplaires de chaque carte en favori, par id de carte ; fiche de chaque carte : titre et rareté)."""
    me = f"eq.{ctx.client.user_id}"
    starred = await _select(ctx, "user_cards", {"select": "card_id", "user_id": me, "starred": "eq.true"})
    ids = sorted({str(r["card_id"]) for r in starred if r.get("card_id")})
    copies: dict[str, list[dict]] = {}
    catalog: dict[str, dict] = {}
    for chunk in _chunks(ids, LOOKUP_BATCH):
        listed = f"in.({','.join(chunk)})"
        for row in await _select(ctx, "user_cards", {"select": "id,card_id,snapshot_rarity,is_shiny,starred",
                                                     "user_id": me, "card_id": listed}):
            copies.setdefault(str(row["card_id"]), []).append(row)
        catalog.update({str(r["id"]): r for r in await _select(ctx, "cards", {"select": "id,wikipedia_title,rarity",
                                                                                "id": listed})})
    return copies, catalog


def rank(copy: dict, card: dict) -> int:
    """Rareté d'un exemplaire : sa rareté figée, sinon celle de la carte (comme la fiche du site)."""
    return RARITY_RANK.get(copy.get("snapshot_rarity") or card.get("rarity") or "", -1)


def plan(copies: dict[str, list[dict]], catalog: dict[str, dict]) -> tuple[dict[str, dict], list[tuple[dict, list[dict], str]]]:
    """(favoris suivis : id de carte -> {title, best} ; favoris à déplacer : (meilleur exemplaire, exemplaires plus bas
    à sortir des favoris, titre)). Une carte avec un exemplaire shiny est déjà au plus haut : ni suivie ni touchée."""
    cards: dict[str, dict] = {}
    moves: list[tuple[dict, list[dict], str]] = []
    for cid, rows in copies.items():
        card = catalog.get(cid) or {}
        title = str(card.get("wikipedia_title") or "")
        if not title or any(r.get("is_shiny") for r in rows):
            continue
        best = max(rows, key=lambda r: rank(r, card))
        cards[cid] = {"title": title, "best": rank(best, card)}
        lower = [r for r in rows if r.get("starred") and rank(r, card) < rank(best, card)]
        if not best.get("starred") and lower:
            moves.append((best, lower, title))
    return cards, moves


async def _star(ctx: Context, copy_id: str, starred: bool) -> bool:
    status, _ = await ctx.client.supabase("PATCH", "user_cards", prefer="return=minimal", json_body={"starred": starred},
                                          params={"id": f"eq.{copy_id}", "user_id": f"eq.{ctx.client.user_id}"})
    return status in (200, 204)


async def sync(ctx: Context) -> dict[str, Any]:
    """Un passage : favoris lus, demandes du sniper à jour, liste de souhaits alignée, favoris déplacés.
    Renvoie {"tracked", "added", "removed", "moved", "error"}."""
    live = ctx.settings.is_live(WRITE)
    copies, catalog = await read(ctx)
    cards, moves = plan(copies, catalog)
    state = await _state(ctx.db)
    added: dict[str, str] = state["added"]
    result: dict[str, Any] = {"tracked": sum(1 for c in cards.values() if c["best"] < TOP), "added": 0,
                              "removed": 0, "moved": [], "error": ""}
    now = utcnow()
    for cid, card in cards.items():  # une victoire du sniper pas encore arrivée dans la collection reste comptée
        old = state["cards"].get(cid) or {}
        if old.get("won_at") and now - datetime.fromisoformat(old["won_at"]) < WON_MEMORY:
            card["best"] = max(card["best"], int(old.get("won", -1)))
            card["won"], card["won_at"] = old["won"], old["won_at"]
    state["cards"] = cards
    result["tracked"] = sum(1 for c in cards.values() if c["best"] < TOP)
    targets = {cid for cid, card in cards.items() if card["best"] < TOP}
    try:
        have = await wishlist.current(ctx)
        fresh = sorted(targets - have)
        for chunk in _chunks(fresh, WRITE_BATCH):
            if not live:
                log.info("[SIMULÉ] %d favori(s) en liste de souhaits", len(chunk))
                break
            status, data = await ctx.client.supabase(
                "POST", "wishlist_items", params={"on_conflict": "user_id,card_id"},
                prefer="resolution=ignore-duplicates,return=minimal",
                json_body=[{"user_id": ctx.client.user_id, "card_id": c} for c in chunk])
            if status not in (200, 201, 204):
                result["error"] = f"ajout à ta liste de souhaits refusé (HTTP {status}) : {str(data)[:200]}"
                break
            added.update({c: cards[c]["title"] for c in chunk})
            result["added"] += len(chunk)
        stale = sorted(c for c in added if c not in targets)
        for chunk in _chunks(stale, LOOKUP_BATCH):
            if not live or result["error"]:
                break
            status, data = await ctx.client.supabase(
                "DELETE", "wishlist_items", prefer="return=minimal",
                params={"user_id": f"eq.{ctx.client.user_id}", "card_id": f"in.({','.join(chunk)})"})
            if status not in (200, 204):
                result["error"] = f"retrait de ta liste de souhaits refusé (HTTP {status}) : {str(data)[:200]}"
                break
            for c in chunk:
                added.pop(c, None)
            result["removed"] += len(chunk)
        for best, lower, title in moves[:MOVES_PER_RUN]:
            if not live:
                log.info("[SIMULÉ] favori déplacé sur %s, meilleur exemplaire", title)
                continue
            if result["error"]:
                break
            # D'abord le nouveau : la carte n'est jamais sans favori, même si la suite échoue.
            if not await _star(ctx, str(best["id"]), True) or not all([await _star(ctx, str(r["id"]), False)
                                                                        for r in lower]):
                result["error"] = f"favori de {title} pas déplacé"
                break
            result["moved"].append(title)
    except ApiError as exc:
        result["error"] = str(exc)
    await ctx.db.kv_set(KV_KEY, state)
    return result


class FavoriteWatch(Worker):
    name = "favorites"
    topic = Topic.COLLECTION

    async def step(self) -> float:
        result = await sync(self.ctx)
        self.log.info("favoris : %d carte(s) à monter, %d ajoutée(s) à ta liste de souhaits, %d retirée(s), "
                      "%d favori(s) déplacé(s)", result["tracked"], result["added"], result["removed"],
                      len(result["moved"]))
        if result["error"]:
            self.log.warning("favoris : %s", result["error"])
        if result["added"] or result["moved"]:
            lines = [f"{result['tracked']} carte(s) en favori à monter, au plus à leur cote, jusqu'à L."]
            if result["added"]:
                lines.append(f"{result['added']} ajoutée(s) à ta liste de souhaits : le site annonce leurs ventes.")
            if result["moved"]:
                shown = ", ".join(result["moved"][:15]) + ("…" if len(result["moved"]) > 15 else "")
                lines.append(f"Favori passé au meilleur exemplaire (l'ancien redevient vendable) : {shown}")
            self.notifier.info("Favoris à monter", "\n".join(lines))
        return random.uniform(0.8, 1.2) * self.settings.favorites_check_minutes * 60
