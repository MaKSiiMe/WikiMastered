"""Collection publique d'un autre joueur (/collection-joueur), comparée à la collection « gifs » : ses cartes par rareté,
celles de notre liste, et celles hors liste qui sont vraiment animées (notre liste est-elle complète ?).

Lecture seule, en requêtes de fond, à la demande ; aucune mise : rien ne compte pour la vérification anti-bot. Le
résultat est gardé en base (kv « player_collection »), exemplaires compris, pour l'analyser ensuite. La route a été
relevée le 02/11 sur le profil d'un joueur ; sa réponse n'a pas encore été vue, d'où les autres clés gardées dans `extra`.
"""

from __future__ import annotations

import json
from collections import Counter

from src.config import RARITIES
from src.core.models import OwnedCard
from src.core.timeutils import utcnow
from src.core.wikidata import Wikidata
from src.workers.base import Context
from src.workers.collection import collection_key, defined
from src.workers.themes import animated, known_frames

KV_KEY = "player_collection"
SHOWN = 15  # cartes animées hors liste montées dans le message
EXTRA_MAX = 2000


def analyse(rows: list[OwnedCard], listed: tuple[str, ...], frames: dict[str, int]) -> dict:
    """Résumé d'une collection lue : cartes par rareté, part de la liste, cartes hors liste selon leur image."""
    wanted = {collection_key(t) for t in listed}
    cards = {row.catalog_id: row for row in rows}
    outside = [row for row in cards.values() if collection_key(row.title) not in wanted]
    status = [animated(row.card, frames) for row in outside]
    by_rarity = Counter(rarity for _, rarity in {(row.catalog_id, row.rarity or "?") for row in rows})
    return {
        "copies": len(rows), "cards": len(cards),
        "by_rarity": {r: by_rarity[r] for r in (*reversed(RARITIES), "?") if by_rarity[r]},
        "in_list": len({collection_key(row.title) for row in rows} & wanted), "list_total": len(wanted),
        "outside_animated": sorted(r.title for r, a in zip(outside, status, strict=True) if a),
        "outside_fixed": status.count(False), "outside_unknown": status.count(None),
        "rows": [[r.title, r.rarity, r.card.gif_file, r.is_shiny] for r in rows],
    }


async def scan(ctx: Context, username: str, tag_id: str = "") -> dict:
    """Lit la collection du joueur (une de ses étiquettes si `tag_id`), l'analyse et garde le résultat en base."""
    rows, extra = await ctx.api.load_player_collection(username, tag_id)
    wikidata = Wikidata()
    try:
        frames = await known_frames(ctx, wikidata, rows)
    finally:
        await wikidata.close()
    state = {"at": utcnow().isoformat(), "username": username, "tag_id": tag_id,
             "extra": json.dumps(extra, ensure_ascii=False, default=str)[:EXTRA_MAX],
             **analyse(rows, defined(ctx.settings)["gifs"].titles, frames)}
    await ctx.db.kv_set(KV_KEY, state)
    return state


def summary(state: dict) -> str:
    """Le message de la commande."""
    rarities = " · ".join(f"{r} {n}" for r, n in state["by_rarity"].items()) or "aucune"
    moving = state["outside_animated"]
    lines = [f"{state['cards']} cartes ({state['copies']} exemplaires)", f"Raretés : {rarities}",
             f"Dans notre liste de GIF : {state['in_list']}/{state['list_total']}",
             f"Hors liste : {len(moving)} animée(s), {state['outside_fixed']} fixe(s), "
             f"{state['outside_unknown']} inconnue(s)"]
    if moving:
        lines.append("Animées hors liste : " + ", ".join(moving[:SHOWN]) + ("…" if len(moving) > SHOWN else ""))
    if not state["cards"]:
        lines.append(f"Réponse du site sans carte reconnue : {state['extra'][:500]}")
    return "\n".join(lines)
