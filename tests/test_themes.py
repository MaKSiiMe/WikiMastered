"""Étiquettes de thème : noms comparés sans émojis, règles de config/etiquettes.yaml, exemplaire gardé, passage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from src.core.models import OwnedCard
from src.core.text import tag_key
from src.core.wikidata import Facts, Wikidata
from src.workers.themes import describes, load_rules, themes_of
from tests.test_integration import make_client, make_context

RULES = load_rules()


def person(description: str, *, female: bool = False, born: int | None = 1990, dead: bool = False,
           jobs: set[str] = frozenset()) -> Facts:
    return Facts("Q1", {"Q5", "Q729"}, set(jobs), set(), female, born, dead, description)


def test_tag_names_ignore_emojis_hash_and_case():
    assert tag_key("🎵 Musique") == tag_key("#musique") == tag_key("MUSIQUE ") == "musique"
    assert tag_key("🗺️ Pays") == "pays" and tag_key("Astéroïde ☄️") == "astéroïde"
    card = OwnedCard.model_validate({"id": "o1", "card": {"id": "c"}, "tags": [{"name": "📅 Jour"}, "Revente"]})
    assert card.tag_names == {"jour", "revente"}


def test_protected_tags_match_renamed_tags_with_emoji(tmp_path):
    """« date » renommée « 📅 Jour » sur le site : PROTECTED_TAGS=Jour la protège encore."""
    from src.config import Settings

    s = Settings(_env_file=None, protected_tags="date,Jour")
    card = OwnedCard.model_validate({"id": "o1", "card": {"id": "c"}, "tags": [{"name": "📅 Jour"}]})
    assert card.tag_names & s.protected_tag_keys


def test_description_words():
    assert describes("footballeuse française", ("footballeu",))
    assert describes("auteure-compositrice-interprète", ("interprète",))
    assert not describes("djihadiste français", ("dj",)) and describes("DJ et producteur", ("dj",))
    assert not describes("missionnaire", ("miss",)) and describes("Miss France 2016", ("miss",))
    assert describes("pilote automobile italien", ("pilote automobile",))
    assert not describes("écrivain et aviateur", ("pilote automobile",))


@pytest.mark.parametrize(("facts", "expected"), [
    # Une actrice jeune : Personne, Cinéma, Babie (pas Animal : l'être humain est un animal pour Wikidata).
    (person("actrice, productrice et mannequin israélienne", female=True, born=1985),
     ["Personne", "Cinéma / Série", "Babie"]),
    # Métiers secondaires de Wikidata ignorés : la description fait foi (Camus, gardien de but étant jeune).
    (person("écrivain, philosophe et journaliste français", jobs={"Q2066131", "Q33999"}), ["Personne"]),
    # Sans description : les métiers.
    (person("", jobs={"Q2066131"}), ["Personne", "Sport"]),
    (person("rappeur et chanteur français"), ["Personne", "Musique"]),
    # Babie : femme, née après 1960, vivante.
    (person("actrice américaine", female=True, born=1950), ["Personne", "Cinéma / Série"]),
    (person("princesse de Galles", female=True, born=1961, dead=True), ["Personne"]),
    (person("femme politique française", female=True, born=1980), ["Personne"]),
    (person("streameuse française", female=True, born=1993), ["Personne", "Babie"]),
    # Un pays est aussi une île et une entité territoriale : Pays seul.
    (Facts("Q17", {"Q6256", "Q23442", "Q56061", "Q271669"}), ["Pays"]),
    (Facts("Q140", set(), taxa={"Q729"}), ["Animal"]),
    (Facts("Q49740", {"Q7889", "Q7397"}), ["Jeu vidéo"]),  # un jeu vidéo est un logiciel
    (Facts("Q243", {"Q41176", "Q811979", "Q8205328", "Q570116"}), ["Lieu"]),  # tour : objet seulement faute de mieux
    (Facts("Q1", {"Q39546"}), ["Objet"]),
    (Facts("Q2", {"Q178561", "Q13418847"}), ["Histoire"]),
    (Facts("Q3", {"Q14795564"}), []),  # un jour de l'année
])
def test_themes_from_wikidata_facts(facts, expected):
    assert themes_of(facts, RULES) == expected


def test_kept_copy_is_the_oldest_and_sale_takes_the_newest():
    """Entre exemplaires identiques, l'exemplaire gardé (thèmes) n'est jamais celui que la vente choisit."""
    from src.workers.inventory_manager import group_by_card, newest_sellable

    def row(oid, day, **extra):
        return OwnedCard.model_validate({"id": oid, "card_id": "c", "card": {"id": "c", "rarity": "C"},
                                         "obtained_at": datetime(2026, 10, day, tzinfo=UTC).isoformat(), **extra})

    copies = group_by_card([row("z-new", 20), row("a-old", 10), row("m-mid", 15)])["c"]
    assert [c.id for c in copies] == ["a-old", "m-mid", "z-new"]
    assert newest_sellable(copies) == "z-new"
    # Le favori reste gardé même s'il est le plus récent.
    copies = group_by_card([row("old", 10), row("fav", 20, starred=True)])["c"]
    assert copies[0].id == "fav"


def wikidata_handler(qids: dict[str, str], targets: dict[str, list[tuple[str, str]]],
                     details: dict[str, dict] | None = None):
    """Faux Wikipédia (titre -> élément) et faux SPARQL (élément -> classes atteintes, puis détails)."""
    details = details or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fr.wikipedia.org":
            titles = request.url.params["titles"].split("|")
            pages = [{"title": t, "pageprops": {"wikibase_item": qids[t]}} if t in qids else {"title": t, "missing": True}
                     for t in titles]
            return httpx.Response(200, json={"query": {"pages": pages}})
        query = parse_qs(request.content.decode())["query"][0]
        uri = "http://www.wikidata.org/entity/"
        items = [q for q in set(qids.values()) if f"wd:{q} " in query + " "]
        if "?via" in query:
            rows = [{"item": {"value": uri + q}, "via": {"value": via}, "target": {"value": uri + t}}
                    for q in items for via, t in targets.get(q, [])]
        else:
            rows = [{"item": {"value": uri + q}, **{k: {"value": v} for k, v in details.get(q, {}).items()}}
                    for q in items]
        return httpx.Response(200, json={"results": {"bindings": rows}})
    return handler


@pytest.mark.asyncio
async def test_pass_tags_kept_copy_marks_duplicates_and_unmarks_kept_copy(tmp_path):
    from tests.test_session import cookie_for

    def own(oid, card, title, day, tags=()):
        return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title, "rarity": "C"},
                "obtained_at": datetime(2026, 10, day, tzinfo=UTC).isoformat(),
                "tags": [{"id": 9, "name": t} for t in tags]}

    collection = [
        own("j-old", "japon", "Japon", 1), own("j-new", "japon", "Japon", 5),
        # Ancien doublon devenu seul exemplaire : « Doublon » retiré ; « Pays » déjà posé (avec émoji).
        own("f-only", "france", "France", 2, tags=["🔁 Doublon", "🗺️ Pays"]),
        own("m-1", "marteau", "Marteau", 3),  # correction du fichier, sans Wikidata
        own("x-1", "xyz", "Xyz inconnu", 3),
    ]
    writes = []

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("supabase.co"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 1, "name": "🗺️ Pays"}, {"id": 2, "name": "Doublon"},
                                                 {"id": 3, "name": "Personne"}])
            writes.append((request.method, json.loads(request.content) if request.content else
                           dict(request.url.params)))
            return httpx.Response(201 if request.method == "POST" else 204)
        return httpx.Response(200, json={"collection": collection})

    client, notifier = make_client(tmp_path, site, dry_run=False, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    wiki = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(wikidata_handler(
        {"Japon": "Q17", "France": "Q142"}, {"Q17": [("n", "Q6256"), ("n", "Q23442")], "Q142": [("n", "Q6256")]}))),
        spacing=0)
    from src.workers.themes import run_pass

    state = await run_pass(ctx, wiki)
    assert state["live"] and state["classified"] == 4 and state["pending_lookups"] == 0
    assert state["counts"] == {"Pays": 2, "Objet": 1}
    assert state["untagged"] == ["Xyz inconnu"]
    # Objet n'existe pas sur le site : signalé, ignoré.
    assert "Objet" in state["missing_tags"] and "Babie" in state["missing_tags"]
    assert writes == [
        ("DELETE", {"user_card_id": "eq.f-only", "tag_id": "eq.2"}),
        ("POST", {"user_card_id": "j-old", "tag_id": 1}),
        ("POST", {"user_card_id": "j-new", "tag_id": 2}),
    ]
    assert state["added"] == 2 and state["removed"] == 1 and state["to_add"] == 1  # Marteau -> Objet (absente)

    # Deuxième passage : rien de neuf à lire sur Wikidata (faits gardés en base).
    wiki_off = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), spacing=0)
    state = await run_pass(ctx, wiki_off, write=False)
    assert state["classified"] == 4 and not state["live"]
    await ctx.db.close()


@pytest.mark.asyncio
async def test_simulated_writes_nothing_and_wikidata_errors_wait(tmp_path):
    from tests.test_session import cookie_for

    writes = []

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("supabase.co"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 2, "name": "Doublon"}])
            writes.append(request)
            return httpx.Response(201)
        return httpx.Response(200, json={"collection": [
            {"id": f"o{i}", "card_id": "c", "card": {"id": "c", "wikipedia_title": "Japon", "rarity": "C"}}
            for i in range(2)]})

    client, notifier = make_client(tmp_path, site, dry_run=False, simulate=["themes"], supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    wiki = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))), spacing=0)
    from src.workers.themes import run_pass

    state = await run_pass(ctx, wiki)
    assert not state["live"] and writes == []
    assert state["pending_lookups"] == 1 and state["classified"] == 0  # Wikidata refusé : relu au prochain passage
    assert state["to_add"] == 1  # le doublon, même sans thème connu
    await ctx.db.close()
