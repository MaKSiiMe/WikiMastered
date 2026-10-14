"""Module G : compléter une collection (par défaut, les 366 jours de l'année).

- Toutes les COLLECTION_REFRESH_MINUTES, lit les cartes possédées de la collection par
  recherche dans ta collection (une recherche par mois pour les jours : 12 requêtes au lieu
  de 175 pages), et enregistre les possédées / manquantes (kv « collection »).
- Le sniper mise sur une carte manquante en vente, jusqu'à COLLECTION_MAX_PRICE, dans la
  même fenêtre que ses autres mises. Une carte gagnée est gardée, jamais revendue.
- Jamais deux enchères en même temps pour la même carte ; jamais sur une carte que tu
  cherches toi-même (tu mises déjà dessus).

Les titres attendus sont ceux de Wikipédia (« 1er janvier », « 14 octobre ») : non vérifié
sur les cartes du site. `python -m src.main collection` montre ce qui est reconnu.
"""

from __future__ import annotations

import re

from src.core.database import utcnow
from src.core.text import normalize_title
from src.workers.base import Context, Worker

KV_KEY = "collection"
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre")
MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def year_days() -> list[str]:
    """Les 366 jours, titrés comme sur Wikipédia : « 1er janvier », « 2 janvier »… « 29 février »."""
    return [f"{'1er' if d == 1 else d} {m}" for m, n in zip(MONTHS, MONTH_DAYS, strict=True) for d in range(1, n + 1)]


def collection_key(title: str) -> str:
    """« 1er janvier », « 1 janvier » et « 1ER JANVIER » donnent la même clé."""
    return re.sub(r"^1er\b", "1", normalize_title(title))


def targets(ctx: Context) -> dict[str, str]:
    """Clé -> titre affiché, pour tous les ensembles et titres demandés."""
    s = ctx.settings
    titles: list[str] = []
    if "jours" in {x.casefold() for x in s.collection_sets}:
        titles += year_days()
    titles += s.collection_titles
    return {collection_key(t): t for t in titles}


async def refresh(ctx: Context) -> dict:
    """Relit les cartes possédées de la collection et enregistre l'état (kv)."""
    wanted = targets(ctx)
    searches = list(MONTHS) if "jours" in {x.casefold() for x in ctx.settings.collection_sets} else []
    searches += ctx.settings.collection_titles
    owned: set[str] = set()
    near: set[str] = set()  # titres trouvés mais non reconnus (diagnostic du format des titres)
    for q in searches:
        for row in await ctx.api.load_collection(q=q):
            key = collection_key(row.title)
            if key in wanted:
                owned.add(key)
            elif re.match(r"^\d", key):
                near.add(row.title)
    state = {"owned": sorted(owned), "missing": sorted(set(wanted) - owned, key=list(wanted).index),
             "total": len(wanted), "unmatched": sorted(near)[:20], "at": utcnow().isoformat()}
    await ctx.db.kv_set(KV_KEY, state)
    return state


def summary_line(state: dict | None, targets_by_key: dict[str, str]) -> str:
    if not state:
        return "Collection : pas encore lue."
    missing = [targets_by_key.get(k, k) for k in state["missing"]]
    shown = ", ".join(missing[:12]) + (f"… (+{len(missing) - 12})" if len(missing) > 12 else "")
    return f"**Collection** : {len(state['owned'])}/{state['total']} — il manque : {shown or 'rien !'}"


class CollectionWorker(Worker):
    name = "collection"

    async def step(self) -> float:
        state = await refresh(self.ctx)
        self.log.info("collection : %d/%d possédées, %d manquantes", len(state["owned"]), state["total"],
                      len(state["missing"]))
        return self.settings.collection_refresh_minutes * 60
