"""Commandes slash Discord : demander une info au bot, ou une carte au sniper, sans ouvrir la console.

Un bot Discord (DISCORD_BOT_TOKEN) tourne dans le même processus que les workers : il lit la même base et passe
par le même client du site (mêmes écarts entre requêtes). Les webhooks gardent les messages du bot ; ce bot-ci
ne fait que répondre, et seulement à DISCORD_OWNER_ID.

- Lecture : /bilan, /roi, /solde, /positions, /ventes, /prix, /collection, /sniper.
- Actions : /snipe (confirmée par un bouton), /snipes, /annuler. Voir src/snipes.py.
- Pilotage : /etat (avec boutons), /pause, /reprise (src/pauses.py), /verif-faite. Une vérification anti-bot
  demandée par le site arrive aussi en message privé, avec un bouton « C'est fait ».
- Cartes : /garder, /vendre, /carte, /opportunites… et les boutons des messages de vente (src/discord_cards.py).
- Connecté, le bot envoie lui-même les messages de vente à boutons (dans le salon de leur webhook) et les alertes
  critiques en message privé ; déconnecté, tout repart par les webhooks.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from types import ModuleType

import discord
from discord import app_commands

from src import backup, pauses, snipes
from src.core import timeutils
from src.core.timeutils import utcnow
from src.recap import STALE_AFTER
from src.roi import roi_discord, wb
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context, Worker
from src.workers.collection import progress_lines, targets

log = logging.getLogger("wikimastered.discord")

TEXT_LIMIT = 3900  # description d'un embed : 4096 au plus, avec le bloc de code
CONFIRM_SECONDS = 120
RARITIES = ["C", "PC", "R", "SR", "UR", "L"]
COLOR, GREEN, ORANGE, RED, GREY = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245, 0x99AAB5
# /pause et /reprise : ce qui est mis en pause (None : tous les workers, sauf la garde des sanctions).
# « ventes » arrête aussi la revente des positions du sniper : pour la défausse ou les doublons seuls, /defausse et
# /doublons.
PAUSE_TARGETS = {"sniper": ["sniper"], "ventes": ["inventory"], "cotes": ["cotes"], "tout": None}
PAUSE_MINUTES = [("30 min", 30), ("1 h", 60), ("2 h", 120), ("6 h", 360), ("jusqu'à /reprise", 0)]
VERIFY_ID = "wm:verif-faite"  # bouton du message privé, encore actif après un redémarrage du bot
WATCH_SECONDS = 15  # vérification anti-bot demandée : vue au plus tard après ce délai
# Boutons en mémoire : actifs jusqu'au redémarrage du bot (ceux de /etat, /valeur et des ventes survivent à un
# redémarrage : src/discord_cards.py, MessageButton). Une confirmation expire, elle (CONFIRM_SECONDS).
BUTTON_SECONDS: float | None = None


def paris(value: str | datetime | None) -> str:
    if not value:
        return "?"
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return f"{moment.astimezone(timeutils.PARIS):%d/%m %H:%M}"


def text_embed(title: str, text: str, *, code: bool = True) -> tuple[discord.Embed, discord.File | None]:
    """Embed du texte ; trop long, il est coupé et joint en entier dans un fichier."""
    file = None
    if len(text) > TEXT_LIMIT:
        file = discord.File(io.BytesIO(text.encode()), filename="detail.txt")
        text = text[:TEXT_LIMIT].rsplit("\n", 1)[0] + "\n… (la suite dans le fichier joint)"
    return discord.Embed(title=title, description=f"```\n{text}\n```" if code else text, color=COLOR), file


async def reply(interaction: discord.Interaction, title: str, text: str, *, code: bool = True) -> None:
    embed, file = text_embed(title, text, code=code)
    extra = {"file": file} if file else {}
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, **extra)
    else:
        await interaction.response.send_message(embed=embed, **extra)


def ago(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


def worker_line(w: Worker, now: datetime, session_ok: bool) -> str:
    """État d'un worker, sans remettre à zéro les compteurs du récap."""
    if w.paused:
        return f"⏸️ {w.paused}"
    if w.blocked:
        return f"🛑 {w.blocked}"
    if w.last_ok is None:
        return "⏳ pas encore passé" + (f" · ⚠️ {w.last_error[:80]}" if w.errors_in_a_row else "")
    stale = session_ok and w.next_due is not None and now - w.next_due > STALE_AFTER
    line = f"{'❌ arrêté ?' if stale else '✅'} dernier passage il y a {ago(now - w.last_ok)}"
    if w.errors_in_a_row:
        line += f" · ⚠️ {w.errors_in_a_row} erreur(s) de suite : {w.last_error[:80]}"
    return line


def state_embed(ctx: Context, workers: list[Worker], started_at: datetime) -> discord.Embed:
    """/etat : mode, session, vérification anti-bot, routes en pause, puis chaque worker."""
    now = utcnow()
    client = ctx.client
    lines = [f"Mode **{ctx.settings.mode_label}** · démarré le {paris(started_at)} (il y a {ago(now - started_at)})"]
    color = GREEN
    if client.halted:
        lines.append(f"🛑 **Arrêté par la garde** : {client.halted}\nPour reprendre : `rm data/HALT` dans la console, "
                     "puis redémarre la stack.")
        color = RED
    if client.session_ok:
        lines.append(f"✅ Session : {client.session_status()}")
    else:
        lines.append("❌ **Session en pause** : les workers attendent un nouveau cookie.")
        color = RED
    sniper = next((w for w in workers if isinstance(w, AuctionSniper)), None)
    if sniper is not None and sniper.antibot_since:
        lines.append(f"🛑 **Vérification anti-bot** demandée depuis {paris(sniper.antibot_since)} · "
                     f"{sniper.antibot_missed} mise(s) non placée(s). Fais-la dans ton navigateur, puis "
                     "« Vérification faite ».")
        color = RED if color == RED else ORANGE
    lines.append(f"💾 Sauvegarde : {backup.describe(ctx.last_backup)}")
    if routes := client.paused_routes():
        lines.append("⏳ Routes du site en pause : " + ", ".join(f"{name} ({ago(timedelta(seconds=left))})"
                                                               for name, left in sorted(routes.items())))
    states = [f"**{w.name}** : {worker_line(w, now, client.session_ok)}" for w in workers]
    if color == GREEN and any(w.paused or w.errors_in_a_row for w in workers):
        color = ORANGE
    embed = discord.Embed(title="État du bot", description="\n".join(lines), color=color)
    for i, value in enumerate(fields(states)):
        embed.add_field(name="Workers" if i == 0 else "\u200b", value=value, inline=False)
    return embed


def antibot_embed(since: datetime, missed: int) -> discord.Embed:
    return discord.Embed(title="🛑 Vérification anti-bot demandée", color=RED, description=(
        f"Le site refuse les mises depuis {paris(since)}" + (f" ({missed} non placée(s))" if missed else "") +
        ".\nFais-la dans ton navigateur (mise à la main sur le marché, ou page Paquets si elle la demande), "
        "puis appuie sur **C'est fait** : la prochaine mise part tout de suite pour la tester."))


Action = Callable[[discord.Interaction], Awaitable[None]]


class Buttons(discord.ui.View):
    """Boutons d'un message, pour le seul propriétaire : (libellé, style, action)."""

    def __init__(self, owner_id: int, buttons: list[tuple[str, discord.ButtonStyle, Action]],
                 timeout: float | None = BUTTON_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        for label, style, action in buttons:
            button: discord.ui.Button = discord.ui.Button(label=label, style=style)
            button.callback = action  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class VerifyButton(discord.ui.View):
    """Bouton « C'est fait » du message privé anti-bot : identifiant fixe, encore actif après un redémarrage."""

    def __init__(self, owner_id: int, action: Action):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="C'est fait", emoji="✅", style=discord.ButtonStyle.success, custom_id=VERIFY_ID)
    async def done(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.action(interaction)


def fields(lines: list[str], limit: int = 1024) -> list[str]:
    """Lignes regroupées en valeurs de champ d'embed (1024 caractères au plus chacune)."""
    out: list[str] = []
    for line in lines:
        if out and len(out[-1]) + 1 + len(line) <= limit:
            out[-1] += "\n" + line
        else:
            out.append(line[:limit])
    return out


def positions_text(rows) -> str:
    """Une ligne par position (sans bloc de code : lisible sur un téléphone)."""
    if not rows:
        return "Aucune position détenue."
    lines = []
    for r in rows:
        sim = " *(simulée)*" if r["dry_run"] else ""
        if r["listing_status"] in ("posted", "uncertain", "simulated"):
            sale = f"🏷️ en vente {wb(r['listing_price'])} WB, fin {paris(r['listing_end'])}"
        elif r["listing_status"]:
            sale = f"🕒 vente programmée à {wb(r['listing_price'])} WB"
        else:
            sale = "⏳ attend une place de vente"
        lines.append(f"**{r['rarity'] or '?'}** {r['title'][:60]}{sim}\nachetée {wb(r['amount'])} WB · {sale}")
    return "\n".join(lines)


def listings_text(rows) -> str:
    if not rows:
        return "Aucune vente programmée ni en cours."
    lines = []
    for r in rows:
        when = f"fin {paris(r['target_end'])}" if r["status"] != "scheduled" else f"publiée {paris(r['post_at'])}"
        kind = {"scheduled": "🕒 programmée", "simulated": "🧪 simulée", "uncertain": "❓ à vérifier",
                "waiting": "⏳ en attente"}.get(r["status"], "🏷️ en cours")
        lines.append(f"**{r['rarity'] or '?'}** {r['title'][:60]}\n{wb(r['base_amount'])} WB · {kind} · {when}")
    return "\n".join(lines)


def snipes_text(found: dict[str, snipes.Snipe]) -> str:
    if not found:
        return "Aucune carte demandée. /snipe pour en ajouter une."
    return "\n".join(f"🎯 **{s.label}** (depuis le {paris(s.added_at)})" for s in found.values())


class Confirm(discord.ui.View):
    """Boutons Confirmer / Annuler, pour le seul propriétaire."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=CONFIRM_SECONDS)
        self.owner_id = owner_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _answer(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._answer(interaction, True)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._answer(interaction, False)


class OwnerTree(app_commands.CommandTree):
    """Toutes les commandes, pour le seul DISCORD_OWNER_ID ; une erreur répond au lieu de rester muette."""

    def __init__(self, client: discord.Client, owner_id: int):
        super().__init__(client)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        if interaction.type == discord.InteractionType.application_command:
            await interaction.response.send_message("Commandes réservées au propriétaire du bot.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        cause = getattr(error, "original", error)
        log.error("commande /%s : %s", interaction.command.name if interaction.command else "?", cause,
                  exc_info=cause)
        text = f"Erreur : {type(cause).__name__} : {cause}"[:1900]
        if interaction.response.is_done():
            await interaction.followup.send(text)
        else:
            await interaction.response.send_message(text)


def build_client(ctx: Context, workers: list[Worker], cli: ModuleType,
                 started_at: datetime | None = None) -> discord.Client:
    """`cli` : le module src.main (bilan, ROI, prix… comme les commandes de la console), passé par lui-même :
    lancé par `python -m src.main`, il n'est pas importable sous ce nom sans être rechargé."""
    s = ctx.settings
    started = started_at or utcnow()
    sniper = next((w for w in workers if isinstance(w, AuctionSniper)), None)
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    tree = client.tree = OwnerTree(client, s.discord_owner_id)  # type: ignore[attr-defined]
    guild = discord.Object(id=s.discord_guild_id) if s.discord_guild_id > 0 else None

    # ------------------------------------------------------------ pilotage (boutons et commandes)
    def verify() -> str:
        if sniper is None:
            return "Sniper désactivé : aucune vérification à tester."
        if sniper.verification_done():
            return "👍 Noté : la prochaine mise teste la vérification. Je te préviens dès qu'une mise passe."
        return "Aucune vérification en attente : les mises passent."

    async def verify_pressed(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(verify())

    def pause_label(name: str) -> str:
        worker = next((w for w in workers if w.name == name), None)
        return (worker.paused if worker else None) or ""

    def state_view() -> discord.ui.View:
        """Boutons de /etat, encore actifs après un redémarrage du bot."""
        buttons: list[tuple[str, int, str, discord.ButtonStyle]] = []
        if sniper is not None and sniper.antibot_since:
            buttons.append(("etatverif", 0, "✅ Vérification faite", discord.ButtonStyle.success))
        if sniper is not None:
            buttons.append(("etatreprise", 0, "▶️ Reprendre le sniper", discord.ButtonStyle.primary)
                           if pause_label("sniper") else
                           ("etatpause", 0, "⏸️ Pause sniper 1 h", discord.ButtonStyle.secondary))
        buttons.append(("etat", 0, "🔄 Actualiser", discord.ButtonStyle.secondary))
        return persistent_view(buttons)

    async def refresh(interaction: discord.Interaction, note: str = "") -> None:
        await interaction.response.edit_message(content=note or None, embed=state_embed(ctx, workers, started),
                                                view=state_view())

    async def verify_and_refresh(interaction: discord.Interaction, _ref: int = 0) -> None:
        await refresh(interaction, verify())

    async def pause_sniper(interaction: discord.Interaction, _ref: int = 0) -> None:
        await pauses.pause(ctx, ["sniper"], utcnow() + timedelta(hours=1))
        log.info("Discord : sniper en pause 1 h")
        await refresh(interaction, "⏸️ Sniper en pause 1 h.")

    async def resume_sniper(interaction: discord.Interaction, _ref: int = 0) -> None:
        await pauses.resume(ctx, ["sniper"])
        log.info("Discord : sniper repris")
        await refresh(interaction, "▶️ Sniper repris.")

    async def watch_antibot() -> None:
        """Vérification anti-bot demandée : message privé avec « C'est fait » ; levée, le message le dit."""
        await client.wait_until_ready()
        alerted: datetime | None = None
        message: discord.Message | None = None
        while not client.is_closed():
            since = sniper.antibot_since if sniper is not None else None
            try:
                if sniper is not None and since is not None and since != alerted:
                    alerted = since
                    owner = await client.fetch_user(s.discord_owner_id)
                    message = await owner.send(embed=antibot_embed(since, sniper.antibot_missed),
                                               view=VerifyButton(s.discord_owner_id, verify_pressed))
                elif since is None and alerted is not None:
                    alerted = None
                    if message is not None:
                        await message.edit(embed=discord.Embed(
                            title="✅ Vérification levée", color=GREEN,
                            description=f"Les mises passent de nouveau ({paris(utcnow())})."), view=None)
                    message = None
            except discord.HTTPException as exc:  # messages privés fermés… : l'alerte du webhook reste
                log.warning("Discord : message privé anti-bot impossible (%s)", exc)
            await asyncio.sleep(WATCH_SECONDS)

    channels: dict[str, discord.abc.Messageable] = {}

    async def owner_dm() -> discord.User:
        return client.get_user(s.discord_owner_id) or await client.fetch_user(s.discord_owner_id)

    async def channel_for(url: str) -> discord.abc.Messageable:
        """Salon d'un webhook (lu une fois) ; inaccessible au bot (salon privé…) : ton message privé."""
        if url not in channels:
            try:
                hook = await discord.Webhook.from_url(url, client=client).fetch()
                channel = client.get_channel(hook.channel_id) or await client.fetch_channel(hook.channel_id)
                channels[url] = channel  # type: ignore[assignment]
            except (discord.HTTPException, ValueError) as exc:
                log.warning("Discord : salon du webhook illisible (%s), messages à boutons en privé", exc)
                channels[url] = await owner_dm()
        return channels[url]

    async def with_buttons(actions: dict, embed: dict, url: str) -> bool:
        view = message_view(actions)
        if view is None or not client.is_ready():
            return False
        target = await channel_for(url) if url else await owner_dm()
        try:
            await target.send(embed=discord.Embed.from_dict(embed), view=view)
        except discord.Forbidden:  # le bot ne peut pas écrire dans ce salon : en privé
            channels[url] = await owner_dm()
            await channels[url].send(embed=discord.Embed.from_dict(embed), view=view)
        return True

    async def direct(embed: dict) -> None:
        if client.is_ready():
            await (await owner_dm()).send(embed=discord.Embed.from_dict(embed))

    from src.discord_cards import message_view, persistent_view, register  # importe ce module : déjà chargé

    register(client, tree, ctx, workers)
    client.wm_handlers.update({  # type: ignore[attr-defined]  # boutons de /etat (persistants)
        "etat": lambda interaction, _ref: refresh(interaction), "etatverif": verify_and_refresh,
        "etatpause": pause_sniper, "etatreprise": resume_sniper})

    async def setup_hook() -> None:
        client.add_view(VerifyButton(s.discord_owner_id, verify_pressed))  # boutons envoyés avant un redémarrage
        if guild:
            tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        log.info("Discord : %d commande(s) enregistrée(s) %s", len(synced),
                 "sur le serveur" if guild else "(globales : jusqu'à 1 h avant d'apparaître)")
        if sniper is not None:
            client.watch_task = asyncio.create_task(watch_antibot())  # type: ignore[attr-defined]
        ctx.notifier.with_buttons, ctx.notifier.direct = with_buttons, direct
    client.setup_hook = setup_hook  # type: ignore[method-assign]

    @tree.command(name="etat", description="Santé du bot : session, vérification anti-bot, workers, pauses")
    async def etat(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=state_embed(ctx, workers, started), view=state_view())

    @tree.command(name="sauvegarde", description="Sauvegarder la base maintenant (avant un gros changement)")
    async def sauvegarde(interaction: discord.Interaction) -> None:
        if not s.enable_backup:
            await interaction.response.send_message("Sauvegardes désactivées (ENABLE_BACKUP).")
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await backup.run_backup(ctx)
        except Exception as exc:  # noqa: BLE001 - l'erreur est dite à l'utilisateur
            log.exception("Sauvegarde demandée impossible")
            await interaction.followup.send(f"❌ Sauvegarde impossible : {type(exc).__name__} : {exc}"[:1900])
            return
        await interaction.followup.send(f"💾 **{result.path.name}** : {result.size >> 20} Mo (base "
                                        f"{result.db_size >> 20} Mo) en {result.seconds:.0f} s. "
                                        f"{s.backup_keep} sauvegardes gardées au plus.")

    @tree.command(name="verif-faite", description="Vérification anti-bot faite : les mises reprennent sans attendre")
    async def verif_faite(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(verify())

    @tree.command(name="pause", description="Mettre le sniper, les ventes ou tout le bot en pause")
    @app_commands.describe(quoi="sniper par défaut", duree="1 h par défaut")
    @app_commands.choices(quoi=[app_commands.Choice(name=n, value=n) for n in PAUSE_TARGETS],
                          duree=[app_commands.Choice(name=n, value=m) for n, m in PAUSE_MINUTES])
    async def pause(interaction: discord.Interaction, quoi: app_commands.Choice[str] | None = None,
                    duree: app_commands.Choice[int] | None = None) -> None:
        what = quoi.value if quoi else "sniper"
        minutes = duree.value if duree else 60
        names = PAUSE_TARGETS[what] or [w.name for w in workers]
        until = utcnow() + timedelta(minutes=minutes) if minutes else None
        paused = await pauses.pause(ctx, [n for n in names if any(w.name == n for w in workers)], until)
        if not paused:
            await interaction.response.send_message(f"Rien à mettre en pause : « {what} » n'est pas actif.")
            return
        log.info("Discord : pause de %s jusqu'à %s", ", ".join(paused), until or "/reprise")
        end = f"jusqu'à {until.astimezone(timeutils.PARIS):%H:%M}" if until else "jusqu'à /reprise"
        text = (f"⏸️ **{', '.join(paused)}** en pause {end}.\nLes mises déjà placées et les ventes en cours ne "
                "sont pas annulées ; leurs résultats sont relus à la reprise.")

        async def resume_now(button_interaction: discord.Interaction) -> None:
            resumed = await pauses.resume(ctx, paused)
            log.info("Discord : reprise de %s (bouton)", ", ".join(resumed) or "rien")
            await button_interaction.response.edit_message(
                content=f"▶️ Repris : {', '.join(resumed) or 'déjà repris'}.", view=None)

        await interaction.response.send_message(text, view=Buttons(s.discord_owner_id, [
            ("▶️ Reprendre", discord.ButtonStyle.primary, resume_now)]))

    @tree.command(name="reprise", description="Reprendre ce qui est en pause")
    @app_commands.describe(quoi="tout par défaut")
    @app_commands.choices(quoi=[app_commands.Choice(name=n, value=n) for n in PAUSE_TARGETS])
    async def reprise(interaction: discord.Interaction, quoi: app_commands.Choice[str] | None = None) -> None:
        names = PAUSE_TARGETS[quoi.value] if quoi else None
        resumed = await pauses.resume(ctx, names)
        log.info("Discord : reprise de %s", ", ".join(resumed) or "rien")
        await interaction.response.send_message(f"▶️ Repris : {', '.join(resumed)}." if resumed
                                                else "Rien n'était en pause.")

    @client.event
    async def on_ready() -> None:
        log.info("Discord : connecté en tant que %s", client.user)

    async def title_choices(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if len(current) < 2:
            return []
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in await ctx.db.known_titles(current)]

    # ------------------------------------------------------------ lecture
    @tree.command(name="bilan", description="Le bilan du jour, comme celui du soir")
    async def bilan(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        _, description, sections = await cli.daily_report_parts(ctx)
        embed = discord.Embed(title=f"Bilan du {timeutils.now_local():%d/%m} (à {timeutils.now_local():%H:%M})",
                              description=description, color=COLOR)
        for name, value in sections:
            embed.add_field(name=name, value=value[:1024] or "—", inline=False)
        await interaction.followup.send(embed=embed)

    @tree.command(name="roi", description="Bénéfice et ROI des reventes, jour par jour")
    @app_commands.describe(jours="nombre de jours, aujourd'hui compris (7 par défaut)")
    async def roi(interaction: discord.Interaction, jours: app_commands.Range[int, 1, 60] = 7) -> None:
        head, days = roi_discord(await cli.roi_report(ctx, jours))
        embed = discord.Embed(title=f"ROI des reventes · {jours} jour(s)", description=head, color=COLOR)
        for i, value in enumerate(fields(days)):
            embed.add_field(name="Par jour" if i == 0 else "\u200b", value=value, inline=False)
        await interaction.response.send_message(embed=embed)

    @tree.command(name="solde", description="Solde du compte et capital immobilisé dans les positions")
    async def solde(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        balance = await ctx.api.balance()
        held = await ctx.db.positions_summary()
        text = f"Solde : **{balance} WB**\nPositions : {held['real_count']} détenue(s), {held['real_cost']} WB " \
               f"immobilisés\nTotal : **{balance + held['real_cost']} WB**"
        await reply(interaction, "Solde", text, code=False)

    @tree.command(name="positions", description="Cartes achetées pour la revente, pas encore revendues")
    async def positions(interaction: discord.Interaction) -> None:
        rows = await ctx.db.held_positions()
        await reply(interaction, f"Positions ({len(rows)})", positions_text(rows), code=False)

    @tree.command(name="ventes", description="Ventes programmées et en cours")
    async def ventes(interaction: discord.Interaction) -> None:
        rows = await ctx.db.open_listings()
        await reply(interaction, f"Ventes ({len(rows)})", listings_text(rows), code=False)

    @tree.command(name="prix", description="Ventes observées d'une carte, estimation du bot, ventes en cours")
    @app_commands.describe(titre="titre exact de la carte")
    @app_commands.autocomplete(titre=title_choices)
    async def prix(interaction: discord.Interaction, titre: str) -> None:
        await interaction.response.defer(thinking=True)
        known = await ctx.db.known_titles(titre, limit=10)
        exact = next((t for t in known if t.casefold() == titre.strip().casefold()), None)
        if exact is None and known:  # « hitler » : les titres exacts qui le contiennent
            await reply(interaction, "Quelle carte ?", f"Pas de carte « {titre} » exactement. Titres vus sur le "
                        "marché :\n" + "\n".join(f"• {t}" for t in known) + "\n\nRelance /prix en choisissant "
                        "dans la liste qui s'affiche pendant la frappe.", code=False)
            return
        await reply(interaction, (exact or titre)[:250], await cli.prices_status(ctx, exact or titre, md=True),
                    code=False)

    @tree.command(name="collection", description="Avancement des collections et cartes manquantes")
    async def collection(interaction: discord.Interaction) -> None:
        state = await ctx.db.kv_get("collection")
        by_key = targets(ctx)
        lines = progress_lines(state, by_key, utcnow(), days=7)
        if state:
            missing = [by_key.get(k, k) for k in state["missing"]]
            lines.append(f"\n**Manquantes ({len(missing)})** : " + (", ".join(missing) or "aucune"))
            lines.append(f"Mise max. par carte manquante : {s.collection_max_price} WB (/plafond-collection)")
        await reply(interaction, "Collection", "\n".join(lines), code=False)

    @tree.command(name="sniper", description="Récap des enchères misées par le sniper")
    @app_commands.describe(heures="période, en heures (24 par défaut)")
    async def sniper_report(interaction: discord.Interaction, heures: app_commands.Range[float, 1, 168] = 24) -> None:
        text = await cli.sniper_report_status(ctx, heures, True)  # en gras (**…**) : pas de bloc de code
        await reply(interaction, f"Sniper, {heures:g} dernières heures", text, code=False)

    # ------------------------------------------------------------ actions
    @tree.command(name="snipe", description="Demander une carte au sniper : il la mise jusqu'à ta mise maximale")
    @app_commands.describe(titre="titre exact de la carte", prix_max="mise maximale en WB",
                           rarete="une seule rareté (toutes par défaut)")
    @app_commands.autocomplete(titre=title_choices)
    @app_commands.choices(rarete=[app_commands.Choice(name=r, value=r) for r in RARITIES])
    async def snipe(interaction: discord.Interaction, titre: str, prix_max: app_commands.Range[int, 1, 100_000],
                    rarete: app_commands.Choice[str] | None = None) -> None:
        rarity = rarete.value if rarete else ""
        notes = []
        if titre.casefold() not in {t.casefold() for t in await ctx.db.known_titles(titre, limit=200)}:
            notes.append("⚠️ Jamais vue sur le marché par le bot : vérifie le titre exact.")
        if sniper is None:
            notes.append("⚠️ Sniper désactivé (ENABLE_SNIPER) : rien ne sera misé.")
        elif not s.is_live("collection_bid"):
            notes.append("ℹ️ Mises de collection simulées (DRY_RUN / SIMULATE) : la mise sera simulée.")
        label = snipes.Snipe(titre, prix_max, rarity).label
        embed = discord.Embed(title="Demander cette carte ?", color=COLOR,
                              description=f"**{label}**\nMise au minimum, jusqu'à {prix_max} WB ; gardée, pas "
                                          "revendue." + ("\n\n" + "\n".join(notes) if notes else ""))
        view = Confirm(s.discord_owner_id)
        await interaction.response.send_message(embed=embed, view=view)
        await view.wait()
        if not view.value:
            embed.title, embed.color = ("Annulé" if view.value is False else "Annulé (pas de réponse)"), 0x99AAB5
            await interaction.edit_original_response(embed=embed, view=None)
            return
        added = await snipes.add(ctx.db, titre, prix_max, rarity)
        if sniper is not None:
            await sniper.reload_snipes()
        log.info("Discord : carte demandée %s", added.label)
        embed.title, embed.color = "Carte demandée", 0x57F287
        await interaction.edit_original_response(embed=embed, view=None)

    @tree.command(name="snipes", description="Cartes demandées au sniper")
    async def snipes_list(interaction: discord.Interaction) -> None:
        await reply(interaction, "Cartes demandées", snipes_text(await snipes.load(ctx.db)), code=False)

    async def snipe_choices(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=sn.label[:100], value=sn.title[:100])
                for sn in (await snipes.load(ctx.db)).values() if current.casefold() in sn.title.casefold()][:25]

    @tree.command(name="annuler", description="Ne plus demander une carte au sniper")
    @app_commands.describe(titre="carte demandée")
    @app_commands.autocomplete(titre=snipe_choices)
    async def annuler(interaction: discord.Interaction, titre: str) -> None:
        removed = await snipes.remove(ctx.db, titre)
        if sniper is not None:
            await sniper.reload_snipes()
        text = f"Retirée : {removed.label}" if removed else f"« {titre} » n'était pas demandée."
        await reply(interaction, "Carte demandée", text, code=False)

    return client


async def run_discord(ctx: Context, workers: list[Worker], cli: ModuleType, started_at: datetime) -> None:
    """Connecte le bot et répond aux commandes jusqu'à l'arrêt (relancé par `supervise` s'il tombe)."""
    async with build_client(ctx, workers, cli, started_at) as client:
        try:
            await client.start(ctx.settings.discord_bot_token)
        finally:
            ctx.notifier.with_buttons = ctx.notifier.direct = None  # messages de nouveau par les webhooks seuls
            if task := getattr(client, "watch_task", None):
                task.cancel()
