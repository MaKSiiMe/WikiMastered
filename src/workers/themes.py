"""Module J : étiquettes de thème (Personne, Pays, Musique…) et « Doublon » sur toute la collection.

- Toutes les THEME_TAGS_HOURS, lit toute la collection (requêtes de fond) et, pour chaque carte, l'exemplaire gardé
  (keep_order : favori, shiny, plus haute rareté, puis le plus ancien ; la vente prend toujours le plus récent).
- Thèmes de la carte : règles de config/etiquettes.yaml appliquées à ce que Wikidata dit de son article (voir
  src/core/wikidata.py), gardé en base (table card_facts) ; THEME_LOOKUPS_PER_RUN cartes nouvelles par passage.
  Les corrections du fichier passent avant Wikidata.
- Posées sur l'exemplaire gardé ; les autres exemplaires reçoivent THEME_DUPLICATE_TAG. Le bot n'enlève jamais un
  thème ; il retire seulement « Doublon » de l'exemplaire gardé (son doublon est parti, ou la carte gardée a changé).
- Écritures : table Supabase `user_card_tags`, comme l'éditeur d'étiquettes du site (voir collection.tag_owned),
  THEME_TAGS_MAX_PER_RUN par passage ; groupe « themes » de LIVE_WRITES / SIMULATE. Tant qu'il en reste, le passage
  suivant a lieu BACKLOG_MINUTES plus tard.
- Aucune mise ni ouverture de paquet : rien ne compte pour la vérification anti-bot.

`python -m src.main etiquettes` montre le classement sans rien écrire.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from src.account import store_collection
from src.core.client import ApiError
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.text import tag_key
from src.core.timeutils import utcnow
from src.core.wikidata import Facts, Wikidata
from src.workers.base import Context, Worker
from src.workers.inventory_manager import group_by_card

log = logging.getLogger(__name__)

KV_KEY = "etiquettes"
RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "etiquettes.yaml"
PERSON = "Q5"  # être humain : les règles « description » et « metiers » ne valent que pour les personnes
MISSING_RETRY = timedelta(days=7)  # article sans élément Wikidata : relu après N jours
BACKLOG_MINUTES = 30
SHOWN = 40  # cartes sans thème gardées dans l'état (commande, message)


@dataclass(frozen=True)
class Theme:
    name: str
    natures: frozenset[str] = frozenset()
    taxa: frozenset[str] = frozenset()
    jobs: frozenset[str] = frozenset()
    words: tuple[str, ...] = ()
    unless: tuple[str, ...] = ()
    alone: bool = False


@dataclass(frozen=True)
class Rules:
    themes: tuple[Theme, ...]
    babie: str = ""
    babie_born_from: int = 0
    babie_alive: bool = True
    babie_words: tuple[str, ...] = ()
    corrections: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def targets(self) -> tuple[set[str], set[str], set[str]]:
        """Classes cherchées sur Wikidata : (natures, métiers, taxons)."""
        natures = {PERSON}.union(*(t.natures for t in self.themes))
        return natures, set().union(*(t.jobs for t in self.themes)), set().union(*(t.taxa for t in self.themes))

    @property
    def fingerprint(self) -> str:
        """Change quand les classes cherchées changent : les faits gardés en base sont alors relus."""
        return hashlib.sha1(json.dumps([sorted(x) for x in self.targets]).encode()).hexdigest()[:12]

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.themes] + ([self.babie] if self.babie else [])


def _words(value: Any) -> tuple[str, ...]:
    return tuple(str(w).casefold().strip() for w in value or () if str(w).strip())


def load_rules(path: Path | None = None) -> Rules:
    raw = yaml.safe_load((path or RULES_FILE).read_text(encoding="utf-8")) or {}
    themes = tuple(Theme(str(name), frozenset(map(str, spec.get("natures") or ())),
                         frozenset(map(str, spec.get("taxons") or ())), frozenset(map(str, spec.get("metiers") or ())),
                         _words(spec.get("description")), tuple(map(str, spec.get("sauf") or ())), bool(spec.get("seul")))
                   for name, spec in (raw.get("themes") or {}).items())
    babie = raw.get("babie") or {}
    return Rules(themes, str(babie.get("etiquette") or "").strip(), int(babie.get("nee_apres") or 0),
                 bool(babie.get("vivante", True)), _words(babie.get("description")),
                 {str(t): tuple(map(str, v or ())) for t, v in (raw.get("corrections") or {}).items()})


def describes(description: str, words: tuple[str, ...]) -> bool:
    """Un mot de la description commence par l'un des mots (« footballeu ») ; un mot court (« dj », « miss ») : le mot
    exact (pas « djihadiste », « missionnaire ») ; une expression (espace, tiret) : contenue telle quelle."""
    text = description.casefold()
    tokens = re.findall(r"\w+", text)
    return any(w in text if " " in w or "-" in w else
               any(t == w or (len(w) > 4 and t.startswith(w)) for t in tokens) for w in words)


def themes_of(facts: Facts, rules: Rules) -> list[str]:
    """Thèmes d'un article, dans l'ordre du fichier."""
    person = PERSON in facts.natures
    found: list[str] = []
    for theme in rules.themes:
        if facts.natures & theme.natures or facts.taxa & theme.taxa:
            found.append(theme.name)
        elif person and (describes(facts.description, theme.words) if facts.description else facts.jobs & theme.jobs):
            found.append(theme.name)
    kept = [name for name in found if not set(next(t for t in rules.themes if t.name == name).unless) & set(found)]
    kept = [name for name in kept if not next(t for t in rules.themes if t.name == name).alone or len(kept) == 1]
    if (rules.babie and person and facts.female and describes(facts.description, rules.babie_words)
            and facts.birth_year is not None and facts.birth_year >= rules.babie_born_from
            and not (rules.babie_alive and facts.dead)):
        kept.append(rules.babie)
    return kept


async def classify(ctx: Context, wikidata: Wikidata, titles: set[str], rules: Rules,
                   limit: int) -> tuple[dict[str, list[str]], int]:
    """Thèmes de chaque titre (corrections, sinon faits gardés en base) ; lit sur Wikidata au plus `limit` titres
    inconnus. Renvoie (thèmes connus, titres encore à lire). Une erreur de Wikidata : on garde ce qui est connu."""
    known = await ctx.db.card_facts()
    now = utcnow()

    def stale(title: str) -> bool:
        if title not in known:
            return True
        facts, fingerprint, fetched = known[title]
        return fingerprint != rules.fingerprint or (facts is None and now - fetched > MISSING_RETRY)

    todo = sorted(t for t in titles if t not in rules.corrections and stale(t))
    if todo[:limit]:
        try:
            found = await wikidata.lookup(todo[:limit], *rules.targets)
        except ApiError as exc:
            log.warning("Wikidata : %s (nouvel essai au prochain passage)", exc)
        else:
            await ctx.db.card_facts_put({t: f.to_json() if f.qid else None for t, f in found.items()},
                                        rules.fingerprint)
            known = await ctx.db.card_facts()
    out: dict[str, list[str]] = {}
    for title in titles:
        if title in rules.corrections:
            out[title] = list(rules.corrections[title])
        elif title in known and not stale(title):
            facts = known[title][0]
            out[title] = themes_of(Facts.from_json(facts), rules) if facts else []
    return out, sum(1 for t in todo if t not in out)


@dataclass
class Plan:
    add: list[tuple[OwnedCard, str]] = field(default_factory=list)
    remove: list[tuple[OwnedCard, str]] = field(default_factory=list)


def plan(groups: dict[str, list[OwnedCard]], themes: dict[str, list[str]], duplicate: str) -> Plan:
    """Étiquettes à poser (thèmes sur l'exemplaire gardé, doublon sur les autres) et doublon à retirer du gardé."""
    out = Plan()
    dup = tag_key(duplicate)
    for copies in groups.values():
        main = copies[0]
        out.add += [(main, t) for t in themes.get(main.title, []) if tag_key(t) not in main.tag_names]
        if dup:
            out.add += [(c, duplicate) for c in copies[1:] if dup not in c.tag_names]
            if dup in main.tag_names:
                out.remove.append((main, duplicate))
    return out


async def run_pass(ctx: Context, wikidata: Wikidata, *, write: bool = True) -> dict:
    """Un passage complet ; `write` False : rien n'est écrit (commande). Renvoie l'état, aussi gardé en base."""
    s = ctx.settings
    rules = load_rules()
    rows = await ctx.api.load_collection(background=True)
    await store_collection(ctx, rows)  # la valeur du compte profite de la lecture
    groups = group_by_card(rows)
    titles = {copies[0].title for copies in groups.values() if copies[0].title}
    themes, pending = await classify(ctx, wikidata, titles, rules, s.theme_lookups_per_run)
    todo = plan(groups, themes, s.theme_duplicate_tag.strip())
    counts = Counter(t for title in titles for t in themes.get(title, []))
    state: dict = {
        "at": utcnow().isoformat(), "cards": len(titles), "copies": len(rows), "classified": len(themes),
        "pending_lookups": pending, "counts": dict(counts.most_common()),
        "untagged": sorted(t for t in titles if t in themes and not themes[t])[:SHOWN],
        "untagged_count": sum(1 for t in titles if t in themes and not themes[t]),
        "categories": dict(Counter(str(c.card.category) for c in rows).most_common(10)),
        "to_add": len(todo.add), "to_remove": len(todo.remove), "added": 0, "removed": 0, "missing_tags": [],
        "live": write and s.is_live("theme_tag"),
    }
    if todo.add or todo.remove:
        await apply(ctx, todo, rules, state, write)
    await ctx.db.kv_set(KV_KEY, state)
    return state


async def apply(ctx: Context, todo: Plan, rules: Rules, state: dict, write: bool) -> None:
    """Retraits puis ajouts, THEME_TAGS_MAX_PER_RUN au plus ; une étiquette absente du site est ignorée."""
    s = ctx.settings
    status, data = await ctx.client.supabase("GET", "tags", params={"select": "id,name",
                                                                     "user_id": f"eq.{ctx.api.user_id}"},
                                             background=True)
    ids = {tag_key(str(t.get("name", ""))): t["id"] for t in data or [] if isinstance(t, dict)} if status == 200 else {}
    wanted = {*rules.names, *(t for v in rules.corrections.values() for t in v), s.theme_duplicate_tag.strip()} - {""}
    state["missing_tags"] = sorted(t for t in wanted if tag_key(t) not in ids)
    ops = [("remove", c, t) for c, t in todo.remove] + [("add", c, t) for c, t in todo.add]
    ops = [op for op in ops if tag_key(op[2]) in ids][: s.theme_tags_max_per_run]
    for kind, copy, tag in ops:
        if not state["live"]:
            log.info("[SIMULÉ] étiquette « %s » %s %s (%s)", tag, "retirée de" if kind == "remove" else "sur",
                     copy.title, copy.rarity)
            continue
        tag_id = ids[tag_key(tag)]
        if kind == "add":
            status, data = await ctx.client.supabase("POST", "user_card_tags", prefer="return=minimal",
                                                     json_body={"user_card_id": copy.id, "tag_id": tag_id},
                                                     background=True)
            ok = status in (200, 201, 204, 409)  # 409 : déjà étiquetée
        else:
            status, data = await ctx.client.supabase("DELETE", "user_card_tags", prefer="return=minimal",
                                                     params={"user_card_id": f"eq.{copy.id}", "tag_id": f"eq.{tag_id}"},
                                                     background=True)
            ok = status in (200, 204)
        if not ok:
            state["tag_error"] = f"{'ajout' if kind == 'add' else 'retrait'} refusé (HTTP {status}) : {str(data)[:200]}"
            break
        state["added" if kind == "add" else "removed"] += 1
    state["to_add"] -= state["added"]
    state["to_remove"] -= state["removed"]


def summary(state: dict) -> str:
    counts = " · ".join(f"{name} {n}" for name, n in state["counts"].items())
    lines = [f"{state['classified']}/{state['cards']} cartes classées" +
             (f" ({state['pending_lookups']} à lire sur Wikidata)" if state["pending_lookups"] else ""),
             counts or "aucun thème"]
    if state["untagged_count"]:
        lines.append(f"Sans thème : {state['untagged_count']} (ex. {', '.join(state['untagged'][:8])})")
    return "\n".join(lines)


class ThemeTagger(Worker):
    name = "etiquettes"
    topic = Topic.COLLECTION

    async def setup(self) -> None:
        load_rules()  # un fichier invalide : erreur dès le démarrage du worker
        self.wikidata = Wikidata()

    async def step(self) -> float:
        state = await run_pass(self.ctx, self.wikidata)
        self.log.info("étiquettes : %d/%d cartes classées, %d posée(s), %d retirée(s), %d + %d restante(s)%s",
                      state["classified"], state["cards"], state["added"], state["removed"], state["to_add"],
                      state["to_remove"], "" if state["live"] else " (simulé)")
        if state["missing_tags"]:
            self.log.warning("étiquettes absentes du site (à créer) : %s", ", ".join(state["missing_tags"]))
        if state.get("tag_error"):
            self.log.warning("étiquettes : %s", state["tag_error"])
        if state["added"] or state["removed"]:
            self.notifier.info("Étiquettes de thème", f"{state['added']} posée(s), {state['removed']} retirée(s) ; "
                               f"reste {state['to_add'] + state['to_remove']}.\n{summary(state)}")
        backlog = state["pending_lookups"] or (state["live"] and state["to_add"] + state["to_remove"]
                                               and not state.get("tag_error"))
        return BACKLOG_MINUTES * 60 if backlog else self.settings.theme_tags_hours * 3600
