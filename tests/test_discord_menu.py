"""/menu : chaque commande des groupes est lançable par boutons, avec ses paramètres convertis comme Discord le ferait."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import httpx
import pytest
from discord import app_commands

from src import discord_menu
from tests.test_integration import make_client, make_context


class FakeResponse:
    def __init__(self):
        self.sent, self.done = [], False

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        self.done = True

    async def defer(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def fake_interaction(user_id=42):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=FakeResponse(),
                           followup=SimpleNamespace(send=FakeResponse().send_message))


@pytest.mark.asyncio
async def test_every_grouped_command_can_be_opened_from_the_menu(tmp_path):
    from src import main as cli
    from src.discord_bot import build_client

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404), discord_owner_id=42)
    ctx = await make_context(tmp_path, client, notifier)
    bot = build_client(ctx, [], cli)
    groups = [c for c in bot.tree.get_commands() if isinstance(c, app_commands.Group)]  # type: ignore[attr-defined]
    problems = {f"{g.name} {c.name}": discord_menu.plan(c)[2] for g in groups for c in g.commands
                if discord_menu.plan(c)[2]}
    assert problems == {}
    vitrine = next(c for g in groups for c in g.commands if c.qualified_name == "profil vitrine")
    fields, lists, _ = discord_menu.plan(vitrine)
    assert [p.name for p in fields][:2] == ["numero", "cartes"] and [p.name for p in lists] == ["disposition"]
    # Les vues et formulaires se construisent dans les limites de Discord (libellés, boutons, listes).
    from src.discord_bot import COMMAND_GROUPS
    by_name = {g.name: g for g in groups}
    discord_menu.MenuView(42, by_name, COMMAND_GROUPS)
    for g in groups:
        discord_menu.GroupView(42, g, by_name, COMMAND_GROUPS)
        for c in g.commands:
            fields, lists, _ = discord_menu.plan(c)
            if fields:
                discord_menu.ParamsModal(42, c, fields, lists)
            if lists:
                discord_menu.ChoicesView(42, c, lists, {})
    await bot.close()
    await ctx.db.close()


@pytest.mark.asyncio
async def test_menu_converts_fields_and_choices_before_calling_the_command():
    group = app_commands.Group(name="essai", description="essai")
    calls = []

    @group.command(name="faire", description="essai")
    @app_commands.choices(rarete=[app_commands.Choice(name="Légendaire", value="L")],
                          nombre=[app_commands.Choice(name="10", value=10)])
    async def faire(interaction: discord.Interaction, titre: str, prix: int, rarete: app_commands.Choice[str],
                    nombre: int = 25, shiny: bool = False, note: str = "") -> None:
        calls.append((titre, prix, rarete, nombre, shiny, note))

    fields, lists, problem = discord_menu.plan(faire)
    assert problem == "" and [p.name for p in fields] == ["titre", "prix", "note"]
    assert [p.name for p in lists] == ["rarete", "nombre", "shiny"]
    assert discord_menu.convert(fields[1], " 1 500 ") == 1500
    with pytest.raises(ValueError):
        discord_menu.convert(fields[1], "beaucoup")

    view = discord_menu.ChoicesView(42, faire, lists, {"titre": "Zidane", "prix": 1500})
    interaction = fake_interaction()
    await view._launch(interaction)  # rareté obligatoire pas encore choisie
    assert calls == [] and "rarete" in interaction.response.sent[0][0][0]
    view.picked = {"rarete": "L", "shiny": "1"}
    await view._launch(fake_interaction())
    assert calls == [("Zidane", 1500, app_commands.Choice(name="Légendaire", value="L"), 25, True, "")]


@pytest.mark.asyncio
async def test_menu_answers_only_its_owner():
    view = discord_menu.MenuView(42, {}, {})
    stranger = fake_interaction(user_id=7)
    assert await view.interaction_check(stranger) is False and stranger.response.sent
    assert await view.interaction_check(fake_interaction()) is True
