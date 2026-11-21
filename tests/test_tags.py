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

    client, notifier = make_client(tmp_path, handler, dry_run=False, enable_theme_tags=True, showcase_tag="Vitrine",
                                   wish_tag="", position_unsold_tag="", manual_resale_tag="",
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    status, found = await tags.site_tags(ctx)
    assert status == 200 and "icon=ball" in tags.overview(found)
    todo = {t.name: t for t in tags.missing(found, tags.wanted(ctx))}
    # Noms comparés sans émojis ni casse : Football et Film existent déjà.
    assert "Football" not in todo and "Film" not in todo
    assert todo["Plante"].label == "🌿 Plante" and todo["Plante"].color == "#4ade80"
    assert todo["Vitrine"].label == "🏛️ Vitrine"
    assert todo["Doublon"].color in tags.PALETTE  # pas de couleur dans le fichier : celle de la palette
    assert await tags.create(ctx, todo["Plante"]) == (True, "")
    assert posted == [{"user_id": ctx.api.user_id, "name": "🌿 Plante", "color": "#4ade80"}]
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
