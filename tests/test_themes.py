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
from src.workers.themes import describes, load_rules, plan, themes_of
from tests.test_integration import make_client, make_context

RULES = load_rules()


def person(description: str, *, jobs: set[str] = frozenset()) -> Facts:
    return Facts("Q1", {"Q5", "Q729"}, set(jobs), set(), description)


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
    # Une actrice : Personne, Cinéma (pas Animal : l'être humain est un animal pour Wikidata ; pas Babie : posée à
    # la main).
    (person("actrice, productrice et mannequin israélienne"), ["Personne", "Cinéma / Série"]),
    # Métiers secondaires de Wikidata ignorés : la description fait foi (Camus, gardien de but étant jeune).
    (person("écrivain, philosophe et journaliste français", jobs={"Q2066131", "Q33999"}), ["Personne", "Littérature / BD"]),
    # Sans description : les métiers.
    (person("", jobs={"Q2066131"}), ["Personne", "Sport"]),
    (person("rappeur et chanteur français"), ["Personne", "Musique"]),
    (person("femme politique française"), ["Personne", "Politique"]),
    # « sauf » ne retire pas un thème trouvé par la description : une personne cumule ses métiers.
    (person("actrice, réalisatrice, scénariste et écrivaine française"), ["Personne", "Cinéma / Série", "Littérature / BD"]),
    (person("pasteur et homme d'État allemand"), ["Personne", "Politique", "Religion"]),
    (person("compositeur, musicien, disc-jockey et producteur italien"), ["Personne", "Musique"]),
    # Un pays est aussi une île et une entité territoriale : Pays seul.
    (Facts("Q17", {"Q6256", "Q23442", "Q56061", "Q271669"}), ["Pays"]),
    (Facts("Q140", set(), taxa={"Q729"}), ["Animal"]),
    (Facts("Q49740", {"Q7889", "Q7397"}), ["Jeu vidéo"]),  # un jeu vidéo est un logiciel
    (Facts("Q243", {"Q41176", "Q811979", "Q8205328", "Q570116"}), ["Lieu"]),  # tour : objet seulement faute de mieux
    (Facts("Q1", {"Q39546"}), ["Objet"]),
    (Facts("Q4", {"Q16970", "Q41176", "Q811979", "Q8205328"}), ["Lieu", "Religion"]),  # une église reste un lieu
    (Facts("Q5", {"Q55488", "Q41176", "Q811979", "Q8205328"}), ["Lieu", "Transport"]),  # une gare aussi
    (Facts("Q6", {"Q1114461", "Q95074"}), ["Littérature / BD"]),  # un personnage de BD, pas de cinéma
    (Facts("Q7", {"Q177597", "Q11446", "Q8205328"}), ["Militaire"]),  # un navire de guerre, pas le transport
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
    assert "Objet" in state["missing_tags"]
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


def gif_copy(oid, card, image, tags=()):
    url = f"https://upload.wikimedia.org/{image}?utm_source=x" if image else None
    return OwnedCard.model_validate({"id": oid, "card_id": card, "tags": [{"id": 9, "name": t} for t in tags],
                                     "card": {"id": card, "wikipedia_title": card, "rarity": "C", "image_url": url}})


def test_removable_theme_leaves_only_once_another_theme_is_on_the_card():
    themes = {"gare": ["Lieu", "Transport"], "attente": ["Transport"], "vrai": ["Objet"], "seule": [],
              "doublon": ["Transport"]}
    groups = {
        "posee": [gif_copy("p", "gare", None, ["📦 Objet", "Transport"])],
        "attente": [gif_copy("a", "attente", None, ["Objet"])],  # Transport pas encore posé (ou absent du site)
        "vrai": [gif_copy("v", "vrai", None, ["Objet"])],
        "seule": [gif_copy("s", "seule", None, ["Objet"])],  # aucun thème trouvé : on ne touche à rien
        "inconnue": [gif_copy("i", "pas encore lue", None, ["Objet", "Lieu"])],  # pas encore classée
        "doublon": [gif_copy("d1", "doublon", None, ["Transport"]), gif_copy("d2", "doublon", None, ["Objet"])],
    }
    todo = plan(groups, themes, "", removable=["Objet"])
    assert [(c.id, t) for c, t in todo.remove] == [("p", "Objet"), ("d2", "Objet")]
    assert ("a", "Transport") in [(c.id, t) for c, t in todo.add]
    assert not plan(groups, themes, "").remove  # sans `retirer` : rien


def test_only_objet_is_removable_in_the_rules():
    assert RULES.removable == ["Objet"]


def test_gif_tag_only_on_kept_copy_of_animated_cards():
    frames = {"Anim.gif": 17, "Fixe.gif": 1}
    groups = {
        "new": [gif_copy("n1", "new", "thumb/2/23/Anim.gif/330px-Anim.gif")],
        # L'exemplaire gardé est étiqueté (émoji ignoré) ; le doublon perd l'étiquette et reste donc à vendre.
        "dup": [gif_copy("d1", "dup", "Anim.gif", ["🎞️ GIF"]), gif_copy("d2", "dup", "Anim.gif", ["GIF"])],
        "fixed": [gif_copy("f1", "fixed", "Fixe.gif", ["GIF"])],
        "jpg": [gif_copy("j1", "jpg", "x.jpg", ["GIF"])],
        "unread": [gif_copy("u1", "unread", "Inconnu.gif", ["GIF"])],  # nombre d'images pas encore lu : on ne touche à rien
        "no-image": [gif_copy("i1", "no-image", None, ["GIF"])],
    }
    todo = plan(groups, {}, "", "GIF", frames)
    assert [(c.id, t) for c, t in todo.add] == [("n1", "GIF")]
    assert sorted(c.id for c, _ in todo.remove) == ["d2", "f1", "j1"]
    assert not plan(groups, {}, "", "", frames).add and not plan(groups, {}, "", "", frames).remove


def test_gif_file_from_thumbnail_or_original():
    def file(path):
        return gif_copy("o", "c", path).card.gif_file

    assert file("wikipedia/commons/thumb/2/23/Franks_expansion.gif/330px-Franks_expansion.gif") == "Franks_expansion.gif"
    assert file("wikipedia/commons/2/23/Caf%C3%A9.GIF") == "Café.GIF"
    assert file("x.jpg") is None


@pytest.mark.asyncio
async def test_gif_frames_read_once_with_title_changes_and_continuation(tmp_path):
    from src.workers.themes import known_frames

    calls = []

    def wiki(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "iicontinue" not in request.url.params:
            return httpx.Response(200, json={
                "continue": {"iicontinue": "x", "continue": "||"},
                "query": {"normalized": [{"from": "File:anim_x.gif", "to": "Fichier:Anim x.gif"}],
                          "pages": [{"title": "Fichier:Anim x.gif", "imageinfo": [
                              {"metadata": [{"name": "frameCount", "value": 17}]}]},
                                    {"title": "File:Gone.gif", "missing": True}]}})
        return httpx.Response(200, json={"query": {"pages": [
            {"title": "File:Fixe.gif", "imageinfo": [{"metadata": [{"name": "frameCount", "value": 1}]}]}]}})

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    ctx = await make_context(tmp_path, client, notifier)
    wikidata = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(wiki)), spacing=0)
    rows = [gif_copy("a", "a", "anim_x.gif"), gif_copy("b", "b", "Fixe.gif"), gif_copy("c", "c", "Gone.gif"),
            gif_copy("d", "d", "x.jpg")]
    assert await known_frames(ctx, wikidata, rows) == {"anim_x.gif": 17, "Fixe.gif": 1}
    assert len(calls) == 2
    # Déjà lus : plus de requête que pour le fichier introuvable ; Wikipédia en panne : on garde ce qui est connu.
    down = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), spacing=0)
    assert await known_frames(ctx, down, rows) == {"anim_x.gif": 17, "Fixe.gif": 1}
    await ctx.db.close()
