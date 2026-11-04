"""Module G : compléter des collections de cartes (les 366 jours de l'année, les éléments chimiques…).

- Une collection = un nom, une étiquette et une liste de titres Wikipédia exacts. « jours » est
  intégrée ; les autres sont décrites dans config/collections.yaml (titres écrits là ou dans un fichier à
  part, un par ligne). COLLECTION_SETS choisit celles qui sont actives, COLLECTION_TITLES ajoute des titres isolés.
- Toutes les COLLECTION_REFRESH_MINUTES, lit les cartes possédées des collections et enregistre
  les possédées / manquantes (kv « collection »). Les jours seuls : une recherche par mois
  (12 requêtes au lieu de 175 pages) ; avec une collection du fichier, une lecture complète en
  requêtes de fond (le sniper passe avant).
- Le sniper mise sur une carte manquante en vente, jusqu'à COLLECTION_MAX_PRICE, dans la
  même fenêtre que ses autres mises. Une carte gagnée est gardée, jamais revendue.
- Rareté : la plus haute possédée de chaque carte est enregistrée (kv « collection », clé best). Avec
  COLLECTION_UPGRADE, le sniper mise aussi sur un exemplaire de rareté plus haute, jusqu'à la L.
- Jamais deux enchères en même temps pour la même carte ; jamais sur une carte que tu
  cherches toi-même (tu mises déjà dessus).

- Suivi de complétion : avancement de chaque collection (jours, titres) et date d'obtention de
  chaque carte (kv « collection », clé first_owned), pour le récap, le bilan et la commande.

- Étiquette de la collection (COLLECTION_TAG pour les jours, « date » par défaut) : posée sur l'exemplaire
  gardé de chaque carte (favori, shiny, plus haute rareté : keep_order) et retirée des autres, qui peuvent
  alors partir (le R remplacé par un SR gagné), comme le fait l'éditeur d'étiquettes du site
  (tables Supabase `tags` et `user_card_tags` [WP tags.js]). Avec PROTECTED_TAGS=date, cet exemplaire n'est jamais vendu.

Les titres attendus sont ceux de Wikipédia (« 1er janvier », « 14 octobre »), vérifiés sur les
cartes du site ; seuls les titres exacts comptent (pas « 14 août en sport » ni « 10 mai 1981 »).
`python -m src.main collection` montre ce qui est reconnu.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from src.config import RARITY_RANK, Settings
from src.core import timeutils
from src.core.client import ApiError
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.text import normalize_title, tag_key
from src.core.timeutils import utcnow
from src.workers.base import Context, Worker

log = logging.getLogger(__name__)

KV_KEY = "collection"
COLLECTIONS_FILE = Path(__file__).resolve().parents[2] / "config" / "collections.yaml"
TITLES = "_titres"  # COLLECTION_TITLES
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre")
MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def year_days() -> list[str]:
    """Les 366 jours, titrés comme sur Wikipédia : « 1er janvier », « 2 janvier »… « 29 février »."""
    return [f"{'1er' if d == 1 else d} {m}" for m, n in zip(MONTHS, MONTH_DAYS, strict=True) for d in range(1, n + 1)]


def best_rarity(rows: Iterable[OwnedCard]) -> str | None:
    """Rareté la plus haute parmi des exemplaires (None : aucune connue)."""
    known = [r.rarity for r in rows if r.rarity in RARITY_RANK]
    return max(known, key=RARITY_RANK.__getitem__) if known else None


def collection_key(title: str) -> str:
    """« 1er janvier », « 1 janvier » et « 1ER JANVIER » donnent la même clé."""
    return re.sub(r"^1er\b", "1", normalize_title(title))


SET_ALIASES = {"jour": "jours", "dates": "jours", "date": "jours", "days": "jours", "calendrier": "jours", "gif": "gifs"}


@dataclass(frozen=True)
class Collection:
    name: str                # nom affiché (récap, bilan, commande)
    titles: tuple[str, ...]  # titres Wikipédia exacts
    tag: str = ""            # étiquette posée sur un exemplaire de chaque carte possédée ("" = aucune)


def spec_titles(spec: dict) -> tuple[str, ...]:
    """Titres d'une collection du fichier : « titres » et, pour une longue liste, « fichier » (un titre par ligne, à côté
    de config/collections.yaml)."""
    titles = [str(t) for t in spec.get("titres") or ()]
    if name := spec.get("fichier"):
        lines = (COLLECTIONS_FILE.parent / str(name)).read_text(encoding="utf-8").splitlines()
        titles += [line.strip() for line in lines if line.strip()]
    return tuple(titles)


def defined(settings: Settings) -> dict[str, Collection]:
    """Collections connues : « jours » (intégrée), puis celles de config/collections.yaml."""
    out = {"jours": Collection("Jours de l'année", tuple(year_days()), settings.collection_tag.strip())}
    raw = yaml.safe_load(COLLECTIONS_FILE.read_text(encoding="utf-8")) if COLLECTIONS_FILE.exists() else None
    for key, spec in (raw or {}).items():
        out[normalize_title(str(key))] = Collection(str(spec.get("nom") or key), spec_titles(spec),
                                                    str(spec.get("etiquette") or "").strip())
    return out


def collections(settings: Settings) -> tuple[dict[str, Collection], list[str]]:
    """(collections actives, valeurs inconnues de COLLECTION_SETS)."""
    known = defined(settings)
    active: dict[str, Collection] = {}
    unknown: list[str] = []
    for value in settings.collection_sets:
        key = normalize_title(value)
        key = SET_ALIASES.get(key, key)
        if key in known:
            active[key] = known[key]
        else:
            unknown.append(value)
    if settings.collection_titles:
        active[TITLES] = Collection("Titres", tuple(settings.collection_titles), settings.collection_tag.strip())
    return active, unknown


def unknown_text(settings: Settings, unknown: list[str]) -> str:
    return f"COLLECTION_SETS : {', '.join(unknown)} inconnu(s) ; collections connues : {', '.join(defined(settings))}."


def targets(ctx: Context) -> dict[str, str]:
    """Clé -> titre affiché, pour toutes les collections actives."""
    return {collection_key(t): t for c in collections(ctx.settings)[0].values() for t in c.titles}


async def refresh(ctx: Context) -> dict:
    """Relit les cartes possédées des collections et enregistre l'état (kv)."""
    wanted = targets(ctx)
    active, unknown = collections(ctx.settings)
    full = bool(set(active) - {"jours", TITLES})  # une collection du fichier : toute la collection est lue
    searches = [""] if full else (list(MONTHS) if "jours" in active else []) + list(ctx.settings.collection_titles)
    copies: dict[str, list[OwnedCard]] = {}
    near: set[str] = set()  # titres trouvés mais non reconnus (diagnostic du format des titres)
    for q in searches:
        for row in await ctx.api.load_collection(q=q, background=full):
            key = collection_key(row.title)
            if key in wanted:
                copies.setdefault(key, []).append(row)
            elif re.match(r"^\d", key):
                near.add(row.title)
    owned = set(copies)
    previous = await ctx.db.kv_get(KV_KEY) or {}
    state = {"owned": sorted(owned), "missing": sorted(set(wanted) - owned, key=list(wanted).index),
             "total": len(wanted), "unmatched": sorted(near)[:20], "unknown_sets": unknown,
             "at": utcnow().isoformat(), "sets": set_progress(ctx, owned),
             "first_owned": first_owned(previous, owned, utcnow()),
             "best": {k: r for k, rows in sorted(copies.items()) if (r := best_rarity(rows))}}
    state.update(await tag_owned(ctx, copies, active.values()))
    await ctx.db.kv_set(KV_KEY, state)
    return state


async def record_win(ctx: Context, title: str, rarity: str | None = None) -> None:
    """Carte gagnée par le sniper : possédée tout de suite dans l'état (récap, bilan), sans attendre la
    prochaine lecture (COLLECTION_REFRESH_MINUTES, 6 h par défaut), qui la confirmera."""
    state = await ctx.db.kv_get(KV_KEY)
    key = collection_key(title)
    if not state:
        return
    best = state.setdefault("best", {})
    if rarity in RARITY_RANK and RARITY_RANK[rarity] > RARITY_RANK.get(best.get(key) or "", -1):
        best[key] = rarity
    if key not in state["missing"]:
        await ctx.db.kv_set(KV_KEY, state)
        return
    state["missing"].remove(key)
    state["owned"] = sorted({*state["owned"], key})
    state["sets"] = set_progress(ctx, set(state["owned"]))
    if "first_owned" in state:  # sinon, la première lecture n'a pas encore daté les cartes déjà là
        state["first_owned"][key] = utcnow().isoformat()
    await ctx.db.kv_set(KV_KEY, state)


def set_progress(ctx: Context, owned: set[str]) -> dict[str, list[int]]:
    """Avancement de chaque collection : {nom : [possédées, total]}."""
    out: dict[str, list[int]] = {}
    for c in collections(ctx.settings)[0].values():
        keys = {collection_key(t) for t in c.titles}
        out[c.name] = [len(keys & owned), len(keys)]
    return out


def first_owned(previous: dict, owned: set[str], now: datetime) -> dict[str, str | None]:
    """Date d'obtention de chaque carte possédée. À la première lecture, les cartes déjà là
    n'ont pas de date (inconnue) : elles ne comptent pas comme des gains récents."""
    known: dict[str, str | None] = previous.get("first_owned") or {}
    first_run = "first_owned" not in previous
    return {k: known[k] if k in known else (None if first_run else now.isoformat()) for k in sorted(owned)}


def progress_lines(state: dict | None, titles: dict[str, str], now: datetime, days: int = 7) -> list[str]:
    """Une ligne par collection, puis les cartes obtenues ces `days` derniers jours."""
    if not state:
        return ["Collection pas encore lue."]
    progress = state.get("sets") or {"Collection": [len(state["owned"]), state["total"]]}
    lines = [f"{name} : {owned}/{total} ({f'{100 * owned / total:.1f}'.replace('.', ',')} %)" if total
             else f"{name} : vide"
             for name, (owned, total) in progress.items()]
    recent = recent_cards(state, now, days)
    if recent:
        names = ", ".join(f"{titles.get(k, k)} ({at.astimezone(timeutils.PARIS):%d/%m})" for at, k in recent[:5])
        lines.append(f"+{len(recent)} en {days} jours : {names}{'…' if len(recent) > 5 else ''}")
    else:
        lines.append(f"Aucune nouvelle carte en {days} jours")
    return lines


async def tag_owned(ctx: Context, copies: dict[str, list[OwnedCard]], active: Iterable[Collection]) -> dict:
    """Pose l'étiquette de chaque collection sur l'exemplaire gardé de chaque carte possédée, et la retire des autres
    exemplaires : un exemplaire de rareté plus haute arrivé depuis la reprend, l'ancien n'est plus protégé."""
    from src.workers.inventory_manager import keep_order  # import local : évite un cycle

    keys_by_tag: dict[str, set[str]] = {}
    for c in active:
        if c.tag:
            keys_by_tag.setdefault(c.tag, set()).update(collection_key(t) for t in c.titles)
    if not keys_by_tag:
        return {}
    untagged: list[tuple[str, OwnedCard]] = []
    stale: list[tuple[str, OwnedCard]] = []  # étiquette sur un autre exemplaire que le gardé
    for tag, keys in keys_by_tag.items():
        for key, rows in copies.items():
            if key not in keys:
                continue
            kept = min(rows, key=keep_order)
            if tag_key(tag) not in kept.tag_names:
                untagged.append((tag, kept))
            stale += [(tag, r) for r in rows if r is not kept and tag_key(tag) in r.tag_names]
    out: dict = {"tag": ", ".join(keys_by_tag), "to_tag": len(untagged), "tagged": 0, "untagged": 0}
    if not untagged and not stale:
        return out
    live = ctx.settings.is_live("tag_card")
    try:
        status, data = await ctx.client.supabase("GET", "tags", params={"select": "id,name",
                                                                          "user_id": f"eq.{ctx.api.user_id}"})
    except ApiError as exc:
        out["tag_error"] = str(exc)
        return out
    ids = {tag_key(str(t.get("name", ""))): t["id"] for t in data or []
           if isinstance(t, dict)} if status == 200 else {}
    missing = [tag for tag in keys_by_tag if tag_key(tag) not in ids]
    if missing:
        out["tag_error"] = (f"étiquette(s) « {', '.join(missing)} » introuvable(s) (HTTP {status}) : "
                            "crée-la une fois sur le site")
    waiting = {(tag, collection_key(row.title)) for tag, row in untagged}  # cartes sans exemplaire étiqueté
    for tag, row in [u for u in untagged if tag_key(u[0]) in ids][: ctx.settings.collection_tag_max_per_run]:
        tag_id = ids[tag_key(tag)]
        if not live:
            log.info("[DRY RUN] étiquette « %s » sur %s", tag, row.title)
            continue
        status, data = await ctx.client.supabase("POST", "user_card_tags", prefer="return=minimal",
                                                 json_body={"user_card_id": row.id, "tag_id": tag_id})
        if status in (200, 201, 204, 409):  # 409 : déjà étiquetée
            out["tagged"] += 1
            waiting.discard((tag, collection_key(row.title)))
        else:
            out["tag_error"] = f"ajout refusé (HTTP {status}) : {str(data)[:200]}"
            return out
    out["to_tag"] -= out["tagged"]
    # Retrait seulement une fois l'exemplaire gardé étiqueté : la carte n'est jamais sans protection.
    for tag, row in stale:
        if tag_key(tag) not in ids or (tag, collection_key(row.title)) in waiting:
            continue
        if not live:
            log.info("[DRY RUN] étiquette « %s » retirée de %s (%s)", tag, row.title, row.rarity)
            continue
        status, data = await ctx.client.supabase("DELETE", "user_card_tags", prefer="return=minimal",
                                                 params={"user_card_id": f"eq.{row.id}",
                                                         "tag_id": f"eq.{ids[tag_key(tag)]}"})
        if status not in (200, 204):
            out["tag_error"] = f"retrait refusé (HTTP {status}) : {str(data)[:200]}"
            break
        out["untagged"] += 1
    return out


def progress_text(state: dict) -> list[str]:
    """« Jours de l'année : 366/366 ✅ », « Jours de l'année : 33/366 » : une par collection."""
    progress = state.get("sets") or {"Collection": [len(state["owned"]), state["total"]]}
    return [f"{name} : {owned}/{total}" + (" ✅" if owned >= total else "") if total else f"{name} : vide"
            for name, (owned, total) in progress.items()]


def recent_cards(state: dict, now: datetime, days: int = 7) -> list[tuple[datetime, str]]:
    """(obtenue le, clé) des cartes obtenues ces `days` derniers jours, les plus récentes d'abord."""
    since = now - timedelta(days=days)
    return sorted(((datetime.fromisoformat(at), key) for key, at in (state.get("first_owned") or {}).items()
                   if at and datetime.fromisoformat(at) >= since), reverse=True)


def summary_line(state: dict | None, targets_by_key: dict[str, str]) -> str:
    """Bloc collection du bilan : avancement, nouvelles cartes de la semaine, cartes manquantes."""
    if not state:
        return "pas encore lue"
    missing = [targets_by_key.get(k, k) for k in state["missing"]]
    recent = len(recent_cards(state, utcnow()))
    line = " · ".join(progress_text(state)) + (f" · +{recent} en 7 jours" if recent else "")
    if missing:
        line += "\nIl manque : " + ", ".join(missing[:12]) + (f"… (+{len(missing) - 12})" if len(missing) > 12 else "")
    return line


class CollectionWorker(Worker):
    name = "collection"
    topic = Topic.COLLECTION

    async def step(self) -> float:
        state = await refresh(self.ctx)
        self.log.info("collection : %d/%d possédées, %d manquantes", len(state["owned"]), state["total"],
                      len(state["missing"]))
        if state.get("unknown_sets"):
            self.notifier.warning("Collection : ensemble inconnu", unknown_text(self.settings, state["unknown_sets"]))
        if state.get("tagged") or state.get("untagged"):
            moved = f" ; retirée de {state['untagged']} exemplaire(s) remplacé(s)" if state.get("untagged") else ""
            self.notifier.info("Collection : étiquettes ajoutées",
                               f"« {state['tag']} » ajoutée(s) à {state['tagged']} carte(s){moved} ; "
                               f"reste {state['to_tag']}.")
        if state.get("tag_error"):
            self.log.warning("étiquettes de collection : %s", state["tag_error"])
        if self.settings.collection_wishlist:
            await self._sync_wishlist(state)
        return self.settings.collection_refresh_minutes * 60

    async def _sync_wishlist(self, state: dict) -> None:
        from src.workers import wishlist  # import local : wishlist.py importe ce module

        done = await wishlist.sync(self.ctx, state["missing"], targets(self.ctx))
        self.log.info("liste de souhaits : %d cartes annoncées par le site, +%d, -%d, %d titres sans carte",
                      done["covered"], done["added"], done["removed"], done["unknown"])
        if done["error"]:
            self.notifier.warning("Liste de souhaits", f"{done['error']}\nCes cartes restent cherchées par leur titre.")
