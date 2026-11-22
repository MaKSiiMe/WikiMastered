"""Étiquettes du site (/etiquettes) : celles que le bot pose, celles qui manquent, création comme l'extension."""

from __future__ import annotations

import json

import httpx
import pytest

from src import tags
from tests.test_integration import make_client, make_context


@pytest.mark.asyncio
async def test_missing_tags_are_created_with_their_emoji_and_colour(tmp_path):
    from tests.test_session import cookie_for

    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(201)
        return httpx.Response(200, json=[{"id": 1, "name": "⚽ football", "color": "#22c55e", "icon": "ball"},
                                         {"id": 2, "name": "🎬 Film", "color": "#f87171"}])

    client, notifier = make_client(tmp_path, handler, dry_run=False, enable_theme_tags=True, showcase_tag="Exposée",
                                   wish_tag="", position_unsold_tag="", manual_resale_tag="",
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    status, found = await tags.site_tags(ctx)
    assert status == 200 and "icon=ball" in tags.overview(found)
    todo = {t.name: t for t in tags.missing(found, tags.wanted(ctx))}
    # Noms comparés sans émojis ni casse : Football et Film existent déjà.
    assert "Football" not in todo and "Film" not in todo
    assert todo["Plante"].label == "🌱 Plante" and todo["Plante"].color == "#86efac"
    assert todo["Doublon"].label == "Doublon" and todo["Doublon"].color == "#27272a"  # couleur seule : pas d'émoji
    assert todo["Exposée"].color in tags.PALETTE  # absente du fichier : couleur de la palette
    assert await tags.create(ctx, todo["Plante"]) == (True, "")
    assert posted == [{"user_id": ctx.api.user_id, "name": "🌱 Plante", "color": "#86efac"}]
    await ctx.db.close()


@pytest.mark.asyncio
async def test_nothing_is_created_in_dry_run(tmp_path):
    def handler(request):
        raise AssertionError(f"requête inattendue {request.method} {request.url}")

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    assert await tags.create(ctx, tags.Wanted("Plante", "🌿", "#4ade80")) == (True, "")
    await ctx.db.close()


def test_themes_are_only_wanted_when_theme_tags_are_on(tmp_path):
    class Ctx:
        from src.config import Settings
        settings = Settings(_env_file=None, showcase_tag="", wish_tag="Souhait")

    names = [t.name for t in tags.wanted(Ctx)]
    assert "Football" not in names and "Souhait" in names


def test_restyle_keeps_the_site_emoji_unless_the_file_gives_one():
    known = {"jour": tags.Wanted("Jour", "📅", "#2dd4bf"), "revente": tags.Wanted("Revente", "💰", "#10b981"),
             "sport": tags.Wanted("Sport", "", "#84cc16"), "doublon": tags.Wanted("Doublon", "", "#27272a")}
    site = [{"id": 1, "name": "📆 Jour", "color": "#2dd4bf"}, {"id": 2, "name": "revente", "color": "#000000"},
            {"id": 3, "name": "⚽Sport", "color": "#84CC16"}, {"id": 4, "name": "Doublon", "color": "#52525b"},
            {"id": 5, "name": "🎀 Babie", "color": "#f472b6"}]
    changes = {c.id: (c.name, c.color) for c in tags.restyles(site, known)}
    # Sport : émoji du site gardé, mais un espace ajouté ; Babie absente du fichier : jamais touchée.
    assert changes == {1: ("📅 Jour", "#2dd4bf"), 2: ("💰 Revente", "#10b981"), 3: ("⚽ Sport", "#84cc16"),
                       4: ("Doublon", "#27272a")}
    assert tags.split_name("☄️Astéroïde") == ("☄️", "Astéroïde") and tags.split_name("L+") == ("", "L+")


@pytest.mark.asyncio
async def test_restyle_patches_name_and_colour_and_checks_a_row_came_back(tmp_path):
    from tests.test_session import cookie_for

    calls = []

    def handler(request):
        calls.append((request.method, dict(request.url.params), json.loads(request.content)))
        rows = [] if request.url.params["id"] == "eq.9" else [{"id": 1}]  # 9 : refusée sans erreur par la base
        return httpx.Response(200, json=rows)

    client, notifier = make_client(tmp_path, handler, dry_run=False, supabase_anon_key="anon-key",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    assert await tags.restyle(ctx, tags.Restyle(1, "revente", "#000000", "💰 Revente", "#10b981")) == (True, "")
    assert calls[0] == ("PATCH", {"id": "eq.1", "user_id": f"eq.{ctx.api.user_id}"},
                        {"name": "💰 Revente", "color": "#10b981"})
    ok, why = await tags.restyle(ctx, tags.Restyle(9, "x", "", "x", "#000000"))
    assert not ok and "aucune ligne" in why
    await ctx.db.close()


def test_every_tag_in_the_file_has_a_distinct_colour():
    colours = [t.color.lower() for t in tags.styles().values()]
    assert len(colours) == len(set(colours)), "deux étiquettes du fichier ont la même couleur"
