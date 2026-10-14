"""Module G : compléter une collection (par défaut, les 366 jours de l'année).

- Toutes les COLLECTION_REFRESH_MINUTES, lit les cartes possédées de la collection par
  recherche dans ta collection (une recherche par mois pour les jours : 12 requêtes au lieu
  de 175 pages), et enregistre les possédées / manquantes (kv « collection »).
- Le sniper mise sur une carte manquante en vente, jusqu'à COLLECTION_MAX_PRICE, dans la
  même fenêtre que ses autres mises. Une carte gagnée est gardée, jamais revendue.
- Jamais deux enchères en même temps pour la même carte ; jamais sur une carte que tu
  cherches toi-même (tu mises déjà dessus).

- Étiquette (COLLECTION_TAG, « date » par défaut) : ajoutée à un exemplaire de chaque carte
  possédée qui n'en a pas, comme le fait l'éditeur d'étiquettes du site (tables Supabase `tags`
  et `user_card_tags` [WP tags.js]). Avec PROTECTED_TAGS=date, cet exemplaire n'est jamais vendu.

Les titres attendus sont ceux de Wikipédia (« 1er janvier », « 14 octobre ») : non vérifié
sur les cartes du site. `python -m src.main collection` montre ce qui est reconnu.
"""

from __future__ import annotations

import re

import logging

from src.core.client import ApiError
from src.core.database import utcnow
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.text import normalize_title
from src.workers.base import Context, Worker

log = logging.getLogger(__name__)

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


SET_ALIASES = {"jours": "jours", "jour": "jours", "dates": "jours", "date": "jours", "days": "jours",
               "calendrier": "jours"}


def sets(ctx: Context) -> tuple[set[str], list[str]]:
    """(ensembles reconnus, valeurs inconnues) de COLLECTION_SETS."""
    known: set[str] = set()
    unknown: list[str] = []
    for value in ctx.settings.collection_sets:
        alias = SET_ALIASES.get(normalize_title(value))
        if alias:
            known.add(alias)
        else:
            unknown.append(value)
    return known, unknown


def targets(ctx: Context) -> dict[str, str]:
    """Clé -> titre affiché, pour tous les ensembles et titres demandés."""
    s = ctx.settings
    titles: list[str] = []
    if "jours" in sets(ctx)[0]:
        titles += year_days()
    titles += s.collection_titles
    return {collection_key(t): t for t in titles}


async def refresh(ctx: Context) -> dict:
    """Relit les cartes possédées de la collection et enregistre l'état (kv)."""
    wanted = targets(ctx)
    known, unknown = sets(ctx)
    searches = list(MONTHS) if "jours" in known else []
    searches += ctx.settings.collection_titles
    copies: dict[str, list[OwnedCard]] = {}
    near: set[str] = set()  # titres trouvés mais non reconnus (diagnostic du format des titres)
    for q in searches:
        for row in await ctx.api.load_collection(q=q):
            key = collection_key(row.title)
            if key in wanted:
                copies.setdefault(key, []).append(row)
            elif re.match(r"^\d", key):
                near.add(row.title)
    owned = set(copies)
    state = {"owned": sorted(owned), "missing": sorted(set(wanted) - owned, key=list(wanted).index),
             "total": len(wanted), "unmatched": sorted(near)[:20], "unknown_sets": unknown,
             "at": utcnow().isoformat()}
    state.update(await tag_owned(ctx, copies))
    await ctx.db.kv_set(KV_KEY, state)
    return state


async def tag_owned(ctx: Context, copies: dict[str, list[OwnedCard]]) -> dict:
    """Ajoute l'étiquette de collection au meilleur exemplaire de chaque carte possédée qui n'en a pas."""
    from src.workers.inventory_manager import keep_order  # import local : évite un cycle

    tag = ctx.settings.collection_tag.strip()
    if not tag:
        return {}
    wanted = tag.casefold()
    untagged = [min(rows, key=keep_order) for rows in copies.values()
                if not any(wanted in r.tag_names for r in rows)]
    out: dict = {"tag": tag, "to_tag": len(untagged), "tagged": 0}
    if not untagged:
        return out
    live = ctx.settings.is_live("tag_card")
    try:
        status, data = await ctx.client.supabase("GET", "tags", params={"select": "id,name",
                                                                          "user_id": f"eq.{ctx.api.user_id}"})
    except ApiError as exc:
        out["tag_error"] = str(exc)
        return out
    tag_id = next((t["id"] for t in data or [] if isinstance(t, dict)
                   and str(t.get("name", "")).strip().casefold() == wanted), None) if status == 200 else None
    if tag_id is None:
        out["tag_error"] = f"étiquette « {tag} » introuvable (HTTP {status}) : crée-la une fois sur le site"
        return out
    for row in untagged[: ctx.settings.collection_tag_max_per_run]:
        if not live:
            log.info("[DRY RUN] étiquette « %s » sur %s", tag, row.title)
            continue
        status, data = await ctx.client.supabase("POST", "user_card_tags", prefer="return=minimal",
                                                 json_body={"user_card_id": row.id, "tag_id": tag_id})
        if status in (200, 201, 204, 409):  # 409 : déjà étiquetée
            out["tagged"] += 1
        else:
            out["tag_error"] = f"ajout refusé (HTTP {status}) : {str(data)[:200]}"
            break
    out["to_tag"] -= out["tagged"]
    return out


def summary_line(state: dict | None, targets_by_key: dict[str, str]) -> str:
    if not state:
        return "Collection : pas encore lue."
    missing = [targets_by_key.get(k, k) for k in state["missing"]]
    shown = ", ".join(missing[:12]) + (f"… (+{len(missing) - 12})" if len(missing) > 12 else "")
    return f"**Collection** : {len(state['owned'])}/{state['total']} — il manque : {shown or 'rien !'}"


class CollectionWorker(Worker):
    name = "collection"
    topic = Topic.SNIPER

    async def step(self) -> float:
        state = await refresh(self.ctx)
        self.log.info("collection : %d/%d possédées, %d manquantes", len(state["owned"]), state["total"],
                      len(state["missing"]))
        if state.get("unknown_sets"):
            self.notifier.warning("Collection : ensemble inconnu", f"COLLECTION_SETS={state['unknown_sets']} : "
                                  "valeur attendue « jours ».", topic=Topic.SYSTEM)
        if state.get("tagged"):
            self.notifier.info("Collection : étiquettes ajoutées",
                               f"« {state['tag']} » ajoutée à {state['tagged']} carte(s) ; reste {state['to_tag']}.")
        if state.get("tag_error"):
            self.log.warning("étiquettes de collection : %s", state["tag_error"])
        return self.settings.collection_refresh_minutes * 60
