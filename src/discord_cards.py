"""Commandes Discord sur les cartes, et boutons des messages de vente.

- /garder, /gardees, /liberer : cartes jamais vendues ni défaussées (src/keeps.py) ; une revente en cours de la carte
  est arrêtée (ventes pas encore publiées annulées).
- /vendre : vend un exemplaire au prix choisi, à la première place de vente libre (confirmée par un bouton).
- /plafond-collection : mise maximale d'une carte de collection manquante, sans redéployer (src/overrides.py).
- /carte : tes exemplaires d'une carte, sa cote, ce que le bot en fait ; boutons garder, vendre, demander.
- /opportunites : les ventes suivies par le sniper et son plafond pour chacune.
- Boutons des messages « Revente sans acheteur », « Revente à 1 WB », « Position invendue » : identifiant fixe
  (wm:<action>:<vente>), encore actifs après un redémarrage du bot.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

import discord
from discord import app_commands

from src import account, keeps, orders, overrides, pauses, snipes
from src.charts import number, value_line
from src.core.models import OwnedCard
from src.core.timeutils import utcnow
from src.discord_bot import COLOR, CONFIRM_SECONDS, GREY, RARITIES, Buttons, ago, paris
from src.roi import wb
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context, Worker
from src.workers.inventory_manager import InventoryManager, copies_of, newest_sellable, stop_resale

log = logging.getLogger("wikimastered.discord")

OPPORTUNITIES_SHOWN = 20
OPEN_STATUSES = ("scheduled", "waiting")  # vente pas encore publiée : prix modifiable, annulable
# Boutons des messages de vente, par sorte de message : (action, libellé, style).
MESSAGE_BUTTONS = {
    "relist": [("price", "✏️ Changer le prix", discord.ButtonStyle.primary),
               ("cheap", "⚡ Brader à 1 WB", discord.ButtonStyle.secondary),
               ("keep", "🔒 Garder", discord.ButtonStyle.secondary)],
    "fire_sale": [("price", "✏️ Changer le prix", discord.ButtonStyle.primary),
                  ("keep", "🔒 Garder", discord.ButtonStyle.secondary)],
    "kept": [("resell", "💰 Remettre en vente…", discord.ButtonStyle.primary)],
    "stale": [("stalefire", "⚡ Brader", discord.ButtonStyle.primary),
              ("stalekeep", "🔒 Garder", discord.ButtonStyle.secondary)],
}

Handler = Callable[[discord.Interaction, int], Awaitable[None]]


class MessageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wm:(?P<action>[a-z]+):(?P<ref>\d+)"):
    """Bouton d'un message de vente : l'action et la vente sont dans son identifiant (actif après un redémarrage).
    Les actions sont branchées sur le client (`client.wm_handlers`), le propriétaire seul peut s'en servir."""

    def __init__(self, action: str, ref: int, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"wm:{action}:{ref}"))
        self.action, self.ref = action, ref

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> MessageButton:
        return cls(match["action"], int(match["ref"]), item.label or "", item.style)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == getattr(interaction.client, "wm_owner_id", None)

    async def callback(self, interaction: discord.Interaction) -> None:
        handler: Handler | None = getattr(interaction.client, "wm_handlers", {}).get(self.action)
        if handler is None:
            await interaction.response.send_message("Action inconnue (bot mis à jour ?).", ephemeral=True)
            return
        await handler(interaction, self.ref)


class PriceModal(discord.ui.Modal):
    """Saisie d'un prix en WB ; `on_price` reçoit l'interaction de l'envoi et le prix."""

    def __init__(self, title: str, on_price: Callable[[discord.Interaction, int], Awaitable[None]],
                 default: int | None = None):
        super().__init__(title=title[:45])
        self.on_price = on_price
        self.price: discord.ui.TextInput = discord.ui.TextInput(
            label="Prix en WB", placeholder="ex. 1500", default=str(default) if default else None, max_length=7)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.price.value.replace(" ", "").replace(" ", "")
        if not text.isdigit() or int(text) < 1:
            await interaction.response.send_message(f"« {self.price.value} » n'est pas un prix en WB.", ephemeral=True)
            return
        await self.on_price(interaction, int(text))


async def close_buttons(interaction: discord.Interaction, note: str) -> None:
    """Retire les boutons du message d'origine et y ajoute ce qui a été fait."""
    message = interaction.message
    if message is None:
        await interaction.response.send_message(note)
        return
    embed = message.embeds[0] if message.embeds else None
    if embed is not None:
        embed.add_field(name="Fait", value=note[:1024], inline=False)
    if interaction.response.is_done():
        await message.edit(embed=embed, view=None)
    else:
        await interaction.response.edit_message(embed=embed, view=None)


def card_lines(copies: list[OwnedCard], excluded: set[str], kept: list[keeps.Keep], selling: set[str],
               protected_tags: set[str]) -> list[str]:
    """Une ligne par exemplaire, avec ce qui l'empêche d'être vendu : en vente (`selling` : programmée ou publiée),
    étiquette protégée, gardé, sinon un échange en attente (seule raison restante d'`excluded`)."""
    lines = []
    for c in sorted(copies, key=lambda c: (c.rarity or "", c.obtained_at or utcnow())):
        notes = []
        if c.is_shiny:
            notes.append("✨ shiny")
        if c.starred:
            notes.append("⭐ favori")
        if c.id in selling:
            notes.append("🏷️ en vente")
        if any(k.matches(c.title, c.rarity) for k in kept):
            notes.append("🔒 gardée")
        if c.tag_names & protected_tags:
            notes.append("🛡️ étiquette protégée")
        elif c.id in excluded and c.id not in selling and not any(k.matches(c.title, c.rarity) for k in kept):
            notes.append("🤝 en échange")
        if c.tag_names:
            notes.append("étiquettes : " + ", ".join(sorted(c.tag_names)))
        got = f"obtenue le {paris(c.obtained_at)}" if c.obtained_at else ""
        lines.append(f"**{c.rarity or '?'}** {got}" + (f" · {' · '.join(notes)}" if notes else ""))
    return lines


def value_embed(value: dict, snapshot: dict) -> discord.Embed:
    """/valeur : le total en gros, puis d'où il vient, la collection par rareté et les cartes les plus chères."""
    lines = [f"## {wb(value['total'])} WB"]
    if value["previous"]:
        day, before = value["previous"]
        lines.append(f"{wb(value['total'] - before, True)} WB depuis le {day[8:10]}/{day[5:7]}")
    unknown = f" · {snapshot['unknown']} sans estimation" if snapshot["unknown"] else ""
    lines += ["", f"💰 Solde : **{wb(value['balance'])} WB**",
              f"🃏 Cartes : **{wb(value['cards'])} WB** ({wb(snapshot['copies'])} exemplaires{unknown})",
              f"🏷️ En vente : **{wb(value['selling'])} WB** ({value['listings']} vente(s), au prix de départ)",
              f"⏳ Mises en tête : **{wb(value['bids'])} WB** ({value['leading']} enchère(s))"]
    embed = discord.Embed(title="Valeur du compte", description="\n".join(lines), color=COLOR)
    rarities = [f"**{r}** {wb(n)} · {wb(v)} WB" for r, (n, v) in snapshot["by_rarity"].items() if n]
    if rarities:
        embed.add_field(name="Cartes par rareté", value="\n".join(rarities)[:1024], inline=True)
    if snapshot["top"]:
        embed.add_field(name="Les plus chères", inline=True, value="\n".join(
            f"**{rarity}**{' ✨' if shiny else ''} {title[:30]} · {wb(v)} WB"
            for v, title, rarity, shiny in snapshot["top"])[:1024])
    counted = datetime.fromisoformat(snapshot["at"])
    embed.set_footer(text=f"Collection comptée il y a {ago(utcnow() - counted)} · estimation du bot (ventes "
                          "observées, cote du site)")
    return embed


def sellable(copies: list[OwnedCard], excluded: set[str], rarity: str = "") -> tuple[OwnedCard | None, str]:
    """Exemplaire à vendre (le plus récent, hors favori, shiny et exemplaires exclus) et, sinon, pourquoi."""
    wanted = [c for c in copies if not rarity or c.rarity == rarity]
    if not wanted:
        return None, "aucun exemplaire" + (f" en {rarity}" if rarity else "") + " dans ta collection"
    free = [c for c in wanted if c.id not in excluded and not c.is_shiny]
    if not free:
        return None, "tous tes exemplaires sont gardés, protégés, shiny, déjà en vente ou en échange"
    copy_id = newest_sellable(free)
    if copy_id is None:
        return None, "tes exemplaires libres sont en favori : retire l'étoile sur le site pour le vendre"
    return next(c for c in free if c.id == copy_id), ""


def register(client: discord.Client, tree: app_commands.CommandTree, ctx: Context, workers: list[Worker]) -> None:
    s = ctx.settings
    owner = s.discord_owner_id
    sniper = next((w for w in workers if isinstance(w, AuctionSniper)), None)
    inventory = next((w for w in workers if isinstance(w, InventoryManager)), None)
    client.wm_owner_id = owner  # type: ignore[attr-defined]

    async def title_choices(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if len(current) < 2:
            return []
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in await ctx.db.known_titles(current)]

    rarity_choices = [app_commands.Choice(name=r, value=r) for r in RARITIES]

    # ------------------------------------------------------------ garder
    async def keep_card(title: str, rarity: str = "") -> str:
        keep = await keeps.add(ctx.db, title, rarity)
        stopped = cancelled = 0
        for bid in await ctx.db.held_for_title(title):
            if not rarity or bid["rarity"] == rarity:
                cancelled += await stop_resale(ctx, bid["id"])
                stopped += 1
        posted = [r for r in await ctx.db.open_listings()
                  if r["title"].casefold() == title.casefold() and r["status"] not in OPEN_STATUSES
                  and (not rarity or r["rarity"] == rarity)]
        log.info("Discord : carte gardée %s (%d revente(s) arrêtée(s))", keep.label, stopped)
        text = f"🔒 **{keep.label}** gardée : jamais vendue ni défaussée."
        if stopped:
            text += f"\nRevente arrêtée ({stopped} position(s), {cancelled} vente(s) programmée(s) annulée(s))."
        if posted:
            text += (f"\n⚠️ {len(posted)} vente(s) déjà publiée(s) sur le site, que le bot ne peut pas annuler : "
                     "retire-la toi-même si besoin.")
        return text

    @tree.command(name="garder", description="Ne jamais vendre ni défausser une carte (arrête aussi sa revente)")
    @app_commands.describe(carte="titre exact", rarete="une seule rareté (toutes par défaut)")
    @app_commands.autocomplete(carte=title_choices)
    @app_commands.choices(rarete=rarity_choices)
    async def garder(interaction: discord.Interaction, carte: str,
                     rarete: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.send_message(await keep_card(carte, rarete.value if rarete else ""))

    @tree.command(name="gardees", description="Cartes gardées (jamais vendues ni défaussées)")
    async def gardees(interaction: discord.Interaction) -> None:
        kept = await keeps.load(ctx.db)
        text = "\n".join(f"🔒 **{k.label}** (depuis le {paris(k.added_at)})" for k in kept) or \
            "Aucune carte gardée. /garder pour en ajouter une."
        await interaction.response.send_message(embed=discord.Embed(title=f"Cartes gardées ({len(kept)})",
                                                                    description=text[:4000], color=COLOR))

    async def kept_choices(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=k.label[:100], value=k.label[:100]) for k in await keeps.load(ctx.db)
                if current.casefold() in k.label.casefold()][:25]

    @tree.command(name="liberer", description="Ne plus garder une carte : elle peut de nouveau être vendue")
    @app_commands.describe(carte="carte gardée")
    @app_commands.autocomplete(carte=kept_choices)
    async def liberer(interaction: discord.Interaction, carte: str) -> None:
        removed = await keeps.remove(ctx.db, carte)
        await interaction.response.send_message(f"🔓 **{removed.label}** n'est plus gardée." if removed
                                                else f"« {carte} » n'était pas gardée.")

    # ------------------------------------------------------------ vendre
    async def sell(title: str, price: int, rarity: str = "") -> str:
        """Programme la vente d'un exemplaire ; renvoie le message pour l'utilisateur."""
        if inventory is None:
            return "Ventes désactivées (ENABLE_INVENTORY) : rien n'est vendu."
        copies, excluded = await copies_of(ctx, title)
        copy, why = sellable(copies, excluded, rarity)
        if copy is None:
            return f"Impossible de vendre « {title} » : {why}."
        listing_id = await inventory.sell_copy(copy, price)
        if not listing_id:
            return f"« {title} » : cet exemplaire est déjà en vente."
        log.info("Discord : vente demandée %s (%s) à %d WB", copy.title, copy.rarity, price)
        mode = "" if inventory.listing_live else " (simulée : ventes en DRY_RUN)"
        return f"💰 **{copy.title}** ({copy.rarity}) mise en vente à **{wb(price)} WB**{mode}, à la première place " \
               "libre, avant les autres ventes. Je te préviens quand elle est vendue."

    async def sale_check(title: str, price: int, rarity: str) -> tuple[discord.Embed, bool]:
        """Embed de confirmation de /vendre ; False si rien n'est vendable."""
        copies, excluded = await copies_of(ctx, title)
        copy, why = sellable(copies, excluded, rarity)
        if copy is None:
            return discord.Embed(title="Rien à vendre", description=f"« {title} » : {why}.", color=GREY), False
        notes = []
        same = [c for c in copies if c.rarity == copy.rarity]
        if len(same) == 1:
            notes.append(f"⚠️ C'est ton seul exemplaire en {copy.rarity}.")
        quote = (await ctx.prices.observed_quote(copy.catalog_id)).get(copy.rarity or "")
        if quote and quote.average:
            notes.append(f"Ventes observées : ~{wb(quote.average)} WB ({quote.count} vente(s)).")
            if price < quote.average * 0.5:
                notes.append("⚠️ Moins de la moitié de ce qui se vend d'habitude.")
        if inventory is not None and not inventory.listing_live:
            notes.append("ℹ️ Ventes simulées (DRY_RUN) : rien ne sera vraiment mis en vente.")
        description = f"**{copy.title}** ({copy.rarity}) à **{wb(price)} WB**, à la première place de vente libre." + \
            ("\n\n" + "\n".join(notes) if notes else "")
        return discord.Embed(title="Vendre cette carte ?", description=description, color=COLOR), True

    @tree.command(name="vendre", description="Vendre un exemplaire au prix choisi, à la première place libre")
    @app_commands.describe(carte="titre exact", prix="prix de départ en WB", rarete="rareté de l'exemplaire")
    @app_commands.autocomplete(carte=title_choices)
    @app_commands.choices(rarete=rarity_choices)
    async def vendre(interaction: discord.Interaction, carte: str, prix: app_commands.Range[int, 1, 1_000_000],
                     rarete: app_commands.Choice[str] | None = None) -> None:
        if inventory is None:
            await interaction.response.send_message("Ventes désactivées (ENABLE_INVENTORY).")
            return
        await interaction.response.defer(thinking=True)
        rarity = rarete.value if rarete else ""
        embed, ok = await sale_check(carte, prix, rarity)
        if not ok:
            await interaction.followup.send(embed=embed)
            return

        async def confirm(button: discord.Interaction) -> None:
            await button.response.defer()  # la collection est relue sur le site : plus de 3 s possibles
            await button.edit_original_response(content=await sell(carte, prix, rarity), embed=None, view=None)

        async def cancel(button: discord.Interaction) -> None:
            await button.response.edit_message(content="Annulé.", embed=None, view=None)

        await interaction.followup.send(embed=embed, view=Buttons(owner, [
            ("Vendre", discord.ButtonStyle.success, confirm), ("Annuler", discord.ButtonStyle.secondary, cancel)],
            timeout=CONFIRM_SECONDS))

    # ------------------------------------------------------------ réglages
    @tree.command(name="plafond-collection", description="Mise maximale d'une carte de collection manquante")
    @app_commands.rename(wb_max="wb")
    @app_commands.describe(wb_max="nouveau plafond en WB (sans valeur : affiche le plafond actuel)")
    async def plafond_collection(interaction: discord.Interaction,
                                 wb_max: app_commands.Range[int, 1, 100_000] | None = None) -> None:
        if wb_max is None:
            await interaction.response.send_message(f"Plafond actuel : **{s.collection_max_price} WB** par carte "
                                                    "de collection manquante.")
            return
        old = await overrides.set_value(ctx, "collection_max_price", wb_max)
        log.info("Discord : plafond de collection %s -> %d WB", old, wb_max)
        await interaction.response.send_message(f"📅 Plafond de collection : {old} → **{wb_max} WB**, tout de suite "
                                                "et après les redémarrages (remplace COLLECTION_MAX_PRICE).")

    switch = [app_commands.Choice(name="activer", value=1), app_commands.Choice(name="désactiver", value=0)]

    @tree.command(name="defausse", description="Activer ou couper la défausse automatique (sans toucher aux reventes)")
    @app_commands.describe(etat="sans valeur : affiche l'état actuel")
    @app_commands.choices(etat=switch)
    async def defausse(interaction: discord.Interaction, etat: app_commands.Choice[int] | None = None) -> None:
        if etat is None:
            paused = "cotes" in ctx.pauses
            await interaction.response.send_message(
                f"Défausse automatique : **{'activée' if s.auto_discard else 'coupée'}**"
                + (" · worker cotes en pause" if paused else ""))
            return
        on = bool(etat.value)
        await overrides.set_value(ctx, "auto_discard", on)
        # Le worker cotes ne sert qu'à la défausse des derniers exemplaires : en pause avec elle, ses requêtes
        # (route des cotes, limitée) restent au sniper.
        if on:
            await pauses.resume(ctx, ["cotes"])
        else:
            await pauses.pause(ctx, ["cotes"], None)
        log.info("Discord : défausse %s", "activée" if on else "coupée")
        await interaction.response.send_message(
            "🗑️ Défausse automatique **activée**, worker cotes repris." if on else
            "🗑️ Défausse automatique **coupée** et worker cotes en pause (il ne servait qu'à elle). Les reventes du "
            "sniper et tes ventes continuent. Gardé après les redémarrages ; `/defausse activer` pour reprendre.")

    @tree.command(name="doublons", description="Activer ou couper la vente automatique des doublons")
    @app_commands.describe(etat="sans valeur : affiche l'état actuel")
    @app_commands.choices(etat=switch)
    async def doublons(interaction: discord.Interaction, etat: app_commands.Choice[int] | None = None) -> None:
        if etat is None:
            await interaction.response.send_message(
                f"Vente automatique des doublons : **{'activée' if s.auto_list else 'coupée'}**")
            return
        on = bool(etat.value)
        await overrides.set_value(ctx, "auto_list", on)
        log.info("Discord : vente des doublons %s", "activée" if on else "coupée")
        cancelled = 0 if on else await ctx.db.cancel_scheduled_duplicates()
        extra = f" {cancelled} doublon(s) programmé(s) annulé(s)." if cancelled else ""
        await interaction.response.send_message(
            ("💰 Vente automatique des doublons **activée**." if on else
             "💰 Vente automatique des doublons **coupée** : les places de vente restent aux reventes du sniper et "
             "à tes ventes.") + extra + " Gardé après les redémarrages.")

    # ------------------------------------------------------------ fiche d'une carte
    @tree.command(name="carte", description="Tes exemplaires d'une carte, sa cote et ce que le bot en fait")
    @app_commands.describe(carte="titre exact")
    @app_commands.autocomplete(carte=title_choices)
    async def carte(interaction: discord.Interaction, carte: str) -> None:
        await interaction.response.defer(thinking=True)
        copies, excluded = await copies_of(ctx, carte)
        kept = await keeps.load(ctx.db)
        is_kept = any(k.matches(carte, None) and not k.rarity for k in kept)
        embed = discord.Embed(title=copies[0].title if copies else carte, color=COLOR)
        lines = card_lines(copies, excluded, kept, await ctx.db.scheduled_owned_ids(),
                           s.protected_tag_keys)
        embed.add_field(name=f"Tes exemplaires ({len(copies)})", value="\n".join(lines)[:1024] or "aucun",
                        inline=False)
        card_ids = {c.catalog_id for c in copies} | {r["card_id"] for r in await ctx.db.observed_for_title(carte)}
        quotes: dict[str, Any] = {}
        for card_id in card_ids:
            quotes.update(await ctx.prices.observed_quote(card_id))
        if quotes:
            embed.add_field(name="Ventes observées", inline=False, value="\n".join(
                f"**{r}** ~{wb(q.average)} WB ({q.count} vente(s))" for r, q in sorted(quotes.items())
                if q.average)[:1024] or "—")
        bot = [f"🎯 position achetée {wb(b['amount'])} WB ({b['rarity']})" for b in await ctx.db.held_for_title(carte)]
        bot += [f"🏷️ en vente {wb(r['base_amount'])} WB ({r['rarity']}), "
                + (f"fin {paris(r['target_end'])}" if r["status"] not in OPEN_STATUSES else "programmée")
                for r in await ctx.db.open_listings() if r["title"].casefold() == carte.casefold()]
        wanted = (await snipes.load(ctx.db)).get(snipes.Snipe(carte, 0).key)
        if wanted:
            bot.append(f"🎯 demandée au sniper : {wanted.label}")
        bot += [f"🔒 gardée : {k.label}" for k in kept if k.matches(carte, k.rarity or None)]
        if bot:
            embed.add_field(name="Le bot", value="\n".join(bot)[:1024], inline=False)

        async def keep_pressed(button: discord.Interaction) -> None:
            await button.response.send_message(await keep_card(carte))

        async def free_pressed(button: discord.Interaction) -> None:
            removed = await keeps.remove(ctx.db, carte)
            await button.response.send_message(f"🔓 **{carte}** n'est plus gardée." if removed
                                               else "Elle n'était pas gardée (ou seulement une rareté : /liberer).")

        async def sell_pressed(button: discord.Interaction) -> None:
            async def priced(submit: discord.Interaction, price: int) -> None:
                await submit.response.defer(thinking=True)
                await submit.followup.send(await sell(carte, price))
            await button.response.send_modal(PriceModal(f"Vendre {carte}", priced))

        async def snipe_pressed(button: discord.Interaction) -> None:
            async def priced(submit: discord.Interaction, price: int) -> None:
                added = await snipes.add(ctx.db, carte, price)
                if sniper is not None:
                    await sniper.reload_snipes()
                await submit.response.send_message(f"🎯 Carte demandée : **{added.label}**.")
            await button.response.send_modal(PriceModal(f"Mise max. pour {carte}", priced))

        buttons: list[tuple[str, discord.ButtonStyle, Any]] = [
            ("🔓 Ne plus garder", discord.ButtonStyle.secondary, free_pressed) if is_kept else
            ("🔒 Garder", discord.ButtonStyle.secondary, keep_pressed)]
        if inventory is not None and copies:
            buttons.append(("💰 Vendre…", discord.ButtonStyle.primary, sell_pressed))
        if sniper is not None:
            buttons.append(("🎯 Demander…", discord.ButtonStyle.secondary, snipe_pressed))
        await interaction.followup.send(embed=embed, view=Buttons(owner, buttons))

    # ------------------------------------------------------------ valeur du compte
    async def value_message(refresh: bool) -> tuple[discord.Embed, discord.ui.View]:
        snapshot = await account.collection_snapshot(ctx, refresh=refresh)
        embed = value_embed(await account.account_value(ctx, snapshot), snapshot)
        return embed, persistent_view([("valuehist", 0, "📈 Évolution", discord.ButtonStyle.primary),
                                       ("recount", 0, "🔄 Recompter la collection", discord.ButtonStyle.secondary)])

    async def value_history(interaction: discord.Interaction, _ref: int = 0) -> None:
        points = sorted((await ctx.db.kv_get(account.HISTORY_KEY, {})).items())[-30:]
        if len(points) < 2:
            await interaction.response.send_message(
                "Pas encore d'historique : la valeur du compte est notée une fois par jour, il en faut au moins "
                "deux. Reviens demain.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        values = [float(v) for _, v in points]
        png = await asyncio.to_thread(
            value_line, [date.fromisoformat(d) for d, _ in points], values, "Valeur du compte",
            f"{number(values[-1] - values[0], True)} WB sur {len(points)} jours")
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename="valeur.png"))

    async def recount(interaction: discord.Interaction, _ref: int = 0) -> None:
        # L'ancienne valeur disparaît pendant la relecture : on ne la prend pas pour la nouvelle.
        await interaction.response.edit_message(content=None, view=None, embed=discord.Embed(
            title="Valeur du compte", color=GREY,
            description="⏳ Je relis toute ta collection (2-3 min, le sniper passe avant)…"))
        started = utcnow()
        embed, view = await value_message(True)
        log.info("Discord : collection recomptée en %s", ago(utcnow() - started))
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    @tree.command(name="valeur", description="Valeur du compte : solde + cartes + ventes en cours + mises en tête")
    async def valeur(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        embed, view = await value_message(False)
        await interaction.followup.send(embed=embed, view=view)

    # ------------------------------------------------------------ ventes suivies
    @tree.command(name="opportunites", description="Ventes suivies par le sniper et son plafond pour chacune")
    async def opportunites(interaction: discord.Interaction) -> None:
        if sniper is None:
            await interaction.response.send_message("Sniper désactivé.")
            return
        now = utcnow()
        # Mise minimale déjà au-dessus du plafond : le sniper ne misera pas (sauf si sa cote remonte), à part.
        everything = sniper.watched()
        watched = [w for w in everything if w[1] is None or w[0].min_bid <= w[1]]
        lines = []
        for a, ceiling, why in watched[:OPPORTUNITIES_SHOWN]:
            cap = f"plafond {wb(ceiling)}" if ceiling is not None else "plafond à la cote"
            mark = "🎯" if why == "demandée" else "📅" if why == "collection" else "🔁" if "misée" in why else "👀"
            lines.append(f"{mark} **{a.rarity or '?'}** {a.title[:50]} · mise min {wb(a.min_bid)} · {cap} · "
                         f"fin dans {ago(a.end_at - now)}")
        more = f"\n… et {len(watched) - OPPORTUNITIES_SHOWN} autre(s)" if len(watched) > OPPORTUNITIES_SHOWN else ""
        if too_high := len(everything) - len(watched):
            more += f"\n{too_high} autre(s) suivie(s), déjà au-dessus du plafond"
        legend = "\n\n👀 revente · 🔁 déjà misée · 📅 collection · 🎯 demandée" if lines else ""
        await interaction.response.send_message(embed=discord.Embed(
            title=f"Ventes jouables ({len(watched)})", color=COLOR,
            description=("\n".join(lines) or "Aucune vente suivie pour l'instant.") + more + legend))

    # ------------------------------------------------------------ boutons des messages de vente
    async def listing_or_reply(interaction: discord.Interaction, listing_id: int):
        row = await ctx.db.get_listing(listing_id)
        if row is None:
            await interaction.response.send_message("Vente introuvable.", ephemeral=True)
        elif row["status"] not in OPEN_STATUSES:
            state = "publiée" if row["status"] == "posted" else f"terminée ({row['status']})"
            await close_buttons(interaction, f"Trop tard : la vente est déjà {state} à {wb(row['base_amount'])} WB.")
            return None
        return row

    async def change_price(interaction: discord.Interaction, listing_id: int) -> None:
        row = await listing_or_reply(interaction, listing_id)
        if row is None:
            return

        async def priced(submit: discord.Interaction, price: int) -> None:
            current = await ctx.db.get_listing(listing_id)
            if current is None or current["status"] not in OPEN_STATUSES:
                await submit.response.send_message("Trop tard : la vente est déjà publiée.")
                return
            await ctx.db.update_listing(listing_id, base_amount=price)
            log.info("Discord : vente %d à %d WB", listing_id, price)
            await close_buttons(submit, f"✏️ Prix changé : {wb(current['base_amount'])} → **{wb(price)} WB**.")
        await interaction.response.send_modal(PriceModal("Nouveau prix de départ", priced, row["base_amount"]))

    async def cheap(interaction: discord.Interaction, listing_id: int) -> None:
        row = await listing_or_reply(interaction, listing_id)
        if row is None:
            return
        if inventory is None:
            await interaction.response.send_message("Ventes désactivées (ENABLE_INVENTORY).", ephemeral=True)
            return
        await inventory.liquidate(listing_id)
        current = await ctx.db.get_listing(listing_id)
        log.info("Discord : vente %d bradée à 1 WB", listing_id)
        await close_buttons(interaction, f"⚡ Bradée : **1 WB**, publiée {paris(current['post_at'])} pour "
                                         f"{current['duration_minutes'] // 60} h, fin {paris(current['target_end'])}.")

    async def position_state(interaction: discord.Interaction, bid_id: int):
        """(position, ventes pas encore publiées, ventes publiées) ; répond et renvoie None si elle est déjà close."""
        bid = await ctx.db.get_bid(bid_id)
        if bid is None or bid["closed"]:
            await close_buttons(interaction, "Cette position est déjà close (vendue ou gardée).")
            return None
        rows = await ctx.db.open_listings_for_bid(bid_id)
        scheduled = [r for r in rows if r["status"] in OPEN_STATUSES]
        return bid, scheduled, [r for r in rows if r not in scheduled]

    async def stale_fire(interaction: discord.Interaction, bid_id: int) -> None:
        state = await position_state(interaction, bid_id)
        if state is None:
            return
        _, scheduled, _ = state
        if scheduled and inventory is not None:
            await inventory.liquidate(scheduled[0]["id"])
            current = await ctx.db.get_listing(scheduled[0]["id"])
            note = (f"⚡ Bradée à **1 WB**, publiée {paris(current['post_at'])} pour "
                    f"{current['duration_minutes'] // 60} h, fin {paris(current['target_end'])}.")
        else:
            await orders.give(ctx.db, bid_id, orders.FIRE)
            note = "⚡ Noté : si la vente en cours reste invendue, la carte part tout de suite à **1 WB**."
        log.info("Discord : position %d bradée", bid_id)
        await close_buttons(interaction, note)

    async def stale_keep(interaction: discord.Interaction, bid_id: int) -> None:
        state = await position_state(interaction, bid_id)
        if state is None:
            return
        _, _, posted = state
        if posted:  # déjà publiée : le bot ne peut pas la retirer du site, il garde la carte à sa fin
            await orders.give(ctx.db, bid_id, orders.KEEP)
            note = "🔒 Noté : si la vente en cours reste invendue, la carte reste dans ta collection."
        else:
            await stop_resale(ctx, bid_id)
            note = "🔒 Gardée : plus de revente, la carte reste dans ta collection."
        log.info("Discord : position %d gardée", bid_id)
        await close_buttons(interaction, note)

    async def keep_from_listing(interaction: discord.Interaction, listing_id: int) -> None:
        row = await listing_or_reply(interaction, listing_id)
        if row is None:
            return
        if row["bid_id"]:
            await stop_resale(ctx, row["bid_id"])
        else:
            await ctx.db.update_listing(listing_id, status="cancelled", settled_at=utcnow())
        log.info("Discord : vente %d annulée, carte gardée", listing_id)
        await close_buttons(interaction, "🔒 Remise en vente annulée : la carte reste dans ta collection.")

    async def resell(interaction: discord.Interaction, listing_id: int) -> None:
        row = await ctx.db.get_listing(listing_id)
        if row is None:
            await interaction.response.send_message("Vente introuvable.", ephemeral=True)
            return

        async def priced(submit: discord.Interaction, price: int) -> None:
            await submit.response.defer()  # la collection est relue sur le site : plus de 3 s possibles
            await close_buttons(submit, await sell(row["title"], price, row["rarity"] or ""))
        await interaction.response.send_modal(PriceModal(f"Revendre {row['title']}", priced, row["base_amount"]))

    handlers = {"price": change_price, "cheap": cheap, "keep": keep_from_listing, "resell": resell,
                "recount": recount, "valuehist": value_history, "stalefire": stale_fire, "stalekeep": stale_keep}
    client.wm_handlers = handlers  # type: ignore[attr-defined]
    client.add_dynamic_items(MessageButton)


def persistent_view(buttons: list[tuple[str, int, str, discord.ButtonStyle]]) -> discord.ui.View:
    """Boutons (action, référence, libellé, style) encore actifs après un redémarrage du bot (`MessageButton`) ;
    l'action doit être branchée dans `client.wm_handlers`."""
    view = discord.ui.View(timeout=None)
    for action, ref, label, style in buttons:
        view.add_item(MessageButton(action, ref, label, style))
    return view


def message_view(actions: dict[str, Any]) -> discord.ui.View | None:
    """Boutons d'un message de vente (notifier, `actions`) ; None si la sorte de message n'en a pas."""
    buttons = MESSAGE_BUTTONS.get(actions.get("kind", ""))
    ref = actions.get("listing") or actions.get("bid")  # la vente, ou la position (alerte « Position qui traîne »)
    if not buttons or not ref:
        return None
    return persistent_view([(action, int(ref), label, style) for action, label, style in buttons])

