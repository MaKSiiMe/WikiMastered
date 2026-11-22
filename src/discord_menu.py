"""/menu : toutes les commandes slash en boutons, pour ne pas avoir à les retenir.

Un bouton par groupe (/bot, /compte, /sniper…), puis un bouton par commande du groupe. Une commande sans paramètre
part tout de suite ; sinon un formulaire demande ses textes et nombres (5 champs au plus, obligatoires d'abord),
puis des listes ses choix et ses oui/non. Le menu appelle la commande elle-même (`Command.callback`) : même code,
mêmes réponses que la commande tapée. Réservé à DISCORD_OWNER_ID, comme les commandes.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import discord
from discord import app_commands

log = logging.getLogger("wikimastered.discord")

MENU_SECONDS = 600       # le menu ne répond plus après 10 min sans clic : /menu pour en rouvrir un
MAX_FIELDS = 5           # champs d'un formulaire Discord
MAX_SELECTS = 4          # listes d'un message (une rangée chacune, la cinquième pour « Lancer »)
TEXT_TYPES = (discord.AppCommandOptionType.string, discord.AppCommandOptionType.integer,
              discord.AppCommandOptionType.number)


def _is_choice(command: app_commands.Command, param: app_commands.Parameter) -> bool:
    """Le paramètre attend-il un objet Choice (annotation app_commands.Choice[…]) plutôt que sa valeur ?"""
    annotation = inspect.signature(command.callback).parameters[param.name].annotation
    return "Choice" in str(annotation)


def _choices(param: app_commands.Parameter) -> list[tuple[str, str]]:
    """(libellé, valeur) d'une liste : les choix du paramètre, ou Non/Oui pour un booléen."""
    if param.type == discord.AppCommandOptionType.boolean:
        return [("Non", "0"), ("Oui", "1")]
    return [(str(c.name), str(c.value)) for c in param.choices][:25]


def plan(command: app_commands.Command) -> tuple[list[app_commands.Parameter], list[app_commands.Parameter], str]:
    """(champs du formulaire, listes, problème) : ce que le menu demande avant de lancer la commande."""
    fields = [p for p in command.parameters if not p.choices and p.type in TEXT_TYPES]
    fields = sorted(fields, key=lambda p: not p.required)
    lists = [p for p in command.parameters
             if p.choices or p.type == discord.AppCommandOptionType.boolean]
    problem = ""
    if any(p.required for p in fields[MAX_FIELDS:]) or any(p.required for p in lists[MAX_SELECTS:]):
        problem = "trop de paramètres obligatoires pour le menu"
    return fields[:MAX_FIELDS], lists[:MAX_SELECTS], problem


def convert(param: app_commands.Parameter, raw: str) -> Any:
    """Texte saisi -> valeur du type du paramètre ; ValueError si ce n'est pas un nombre attendu."""
    raw = raw.strip()
    if param.type == discord.AppCommandOptionType.integer:
        return int(raw.replace(" ", ""))
    if param.type == discord.AppCommandOptionType.number:
        return float(raw.replace(",", ".").replace(" ", ""))
    if param.type == discord.AppCommandOptionType.boolean:
        return raw == "1"
    return raw


async def run(command: app_commands.Command, interaction: discord.Interaction, values: dict[str, Any]) -> None:
    """Lance la commande comme si elle avait été tapée."""
    log.info("Discord : /%s par le menu", command.qualified_name)
    try:
        await command.callback(interaction, **values)  # type: ignore[call-arg]
    except Exception as exc:  # comme OwnerTree.on_error : une erreur répond au lieu de rester muette
        log.exception("menu : /%s", command.qualified_name)
        text = f"Erreur : {type(exc).__name__} : {exc}"[:1900]
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=MENU_SECONDS)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Menu réservé au propriétaire du bot.", ephemeral=True)
        return False


class ChoicesView(OwnerView):
    """Listes des choix et oui/non d'une commande, puis « Lancer »."""

    def __init__(self, owner_id: int, command: app_commands.Command, lists: list[app_commands.Parameter],
                 values: dict[str, Any]):
        super().__init__(owner_id)
        self.command, self.lists, self.values, self.picked = command, lists, values, {}
        for row, param in enumerate(lists):
            select = discord.ui.Select(
                placeholder=f"{param.display_name}{' (obligatoire)' if param.required else ''} : "
                            f"{param.description}"[:150],
                options=[discord.SelectOption(label=label[:100], value=value) for label, value in _choices(param)],
                row=row)
            select.callback = self._picker(param, select)
            self.add_item(select)
        launch = discord.ui.Button(label="Lancer", style=discord.ButtonStyle.success, emoji="▶️", row=min(4, len(lists)))
        launch.callback = self._launch
        self.add_item(launch)

    def _picker(self, param: app_commands.Parameter, select: discord.ui.Select):
        async def picked(interaction: discord.Interaction) -> None:
            self.picked[param.name] = select.values[0]
            await interaction.response.defer()
        return picked

    async def _launch(self, interaction: discord.Interaction) -> None:
        missing = [p.display_name for p in self.lists if p.required and p.name not in self.picked]
        if missing:
            await interaction.response.send_message(f"À choisir d'abord : {', '.join(missing)}.", ephemeral=True)
            return
        values = dict(self.values)
        for param in self.lists:
            if param.name not in self.picked:
                continue
            raw = self.picked[param.name]
            if _is_choice(self.command, param):
                label = next(label for label, value in _choices(param) if value == raw)
                values[param.name] = app_commands.Choice(name=label, value=convert(param, raw))
            else:
                values[param.name] = convert(param, raw)
        self.stop()
        await run(self.command, interaction, values)


class ParamsModal(discord.ui.Modal):
    """Textes et nombres d'une commande ; ensuite ses listes, ou la commande elle-même."""

    def __init__(self, owner_id: int, command: app_commands.Command, fields: list[app_commands.Parameter],
                 lists: list[app_commands.Parameter]):
        super().__init__(title=f"/{command.qualified_name}"[:45], timeout=MENU_SECONDS)
        self.owner_id, self.command, self.fields, self.lists = owner_id, command, fields, lists
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for param in fields:
            text = discord.ui.TextInput(label=param.display_name[:45], required=param.required,
                                        placeholder=param.description[:100], max_length=1000,
                                        style=discord.TextStyle.short)
            self.inputs[param.name] = text
            self.add_item(text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values: dict[str, Any] = {}
        for param in self.fields:
            raw = str(self.inputs[param.name].value or "")
            if not raw.strip():
                continue
            try:
                values[param.name] = convert(param, raw)
            except ValueError:
                await interaction.response.send_message(f"« {param.display_name} » doit être un nombre : {raw}",
                                                        ephemeral=True)
                return
        if self.lists:
            await interaction.response.send_message(
                f"**/{self.command.qualified_name}** : choisis puis lance.", ephemeral=True,
                view=ChoicesView(self.owner_id, self.command, self.lists, values))
            return
        await run(self.command, interaction, values)


class GroupView(OwnerView):
    """Les commandes d'un groupe, un bouton chacune, et le retour au menu."""

    def __init__(self, owner_id: int, group: app_commands.Group, groups: dict[str, app_commands.Group],
                 meta: dict[str, tuple[str, str]]):
        super().__init__(owner_id)
        self.groups, self.meta = groups, meta
        for command in group.commands:
            if not isinstance(command, app_commands.Command):
                continue
            button = discord.ui.Button(label=command.name, style=discord.ButtonStyle.primary)
            button.callback = self._opener(command)
            self.add_item(button)
        back = discord.ui.Button(label="Retour", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
        back.callback = self._back
        self.add_item(back)

    def _opener(self, command: app_commands.Command):
        async def open_command(interaction: discord.Interaction) -> None:
            fields, lists, problem = plan(command)
            if problem:
                await interaction.response.send_message(
                    f"/{command.qualified_name} : {problem}, tape la commande.", ephemeral=True)
            elif fields:
                await interaction.response.send_modal(ParamsModal(self.owner_id, command, fields, lists))
            elif lists:
                await interaction.response.send_message(
                    f"**/{command.qualified_name}** : choisis puis lance (rien de choisi : la valeur par défaut).",
                    ephemeral=True, view=ChoicesView(self.owner_id, command, lists, {}))
            else:
                await run(command, interaction, {})
        return open_command

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=menu_embed(self.meta),
                                                view=MenuView(self.owner_id, self.groups, self.meta))


class MenuView(OwnerView):
    """Un bouton par groupe de commandes."""

    def __init__(self, owner_id: int, groups: dict[str, app_commands.Group], meta: dict[str, tuple[str, str]]):
        super().__init__(owner_id)
        self.groups, self.meta = groups, meta
        for name, group in groups.items():
            button = discord.ui.Button(label=name, emoji=meta[name][0], style=discord.ButtonStyle.secondary)
            button.callback = self._opener(group)
            self.add_item(button)

    def _opener(self, group: app_commands.Group):
        async def open_group(interaction: discord.Interaction) -> None:
            emoji, description = self.meta[group.name]
            lines = [f"**{c.name}** · {c.description}" for c in group.commands]
            embed = discord.Embed(title=f"{emoji} /{group.name}", description=f"{description}\n\n" + "\n".join(lines),
                                  color=0x5865F2)
            await interaction.response.edit_message(embed=embed,
                                                    view=GroupView(self.owner_id, group, self.groups, self.meta))
        return open_group


def menu_embed(meta: dict[str, tuple[str, str]]) -> discord.Embed:
    lines = [f"{emoji} **/{name}** · {description}" for name, (emoji, description) in meta.items()]
    return discord.Embed(title="📋 Menu", color=0x5865F2,
                         description="\n".join(lines) + "\n\n✅ **/verif-faite** reste une commande à part.")


def register_menu(tree: app_commands.CommandTree, groups: dict[str, app_commands.Group],
                  meta: dict[str, tuple[str, str]], owner_id: int) -> None:
    @tree.command(name="menu", description="Toutes les commandes en boutons")
    async def menu(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=menu_embed(meta), view=MenuView(owner_id, groups, meta),
                                                ephemeral=True)
