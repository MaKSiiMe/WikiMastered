"""Point d'entrée : orchestre les workers et expose quelques commandes CLI.

    python -m src.main run                      # le bot (commande par défaut du conteneur)
    python -m src.main check                    # vérifie la session et affiche le solde
    python -m src.main inspect                  # capture et compare les vraies réponses JSON
    python -m src.main inventory scan|report|list|discard [--apply]
    python -m src.main report                   # envoie le bilan du jour sur Discord
    python -m src.main roi [--days 7]           # bénéfice et ROI des reventes, jour par jour
    python -m src.main prices "Titre"           # ventes observées d'une carte, estimation du bot
    python -m src.main collection               # collection à compléter (jours de l'année…)
    python -m src.main market [--pages 4] [--rarity UR]  # où sont les fins imminentes dans le marché
    python -m src.main profile                  # profil du joueur : solde, paquets, pity…
    python -m src.main hours [--rarity UR,L]    # à quelle heure de fin les enchères se vendent le mieux
    python -m src.main gifs                     # les cartes GIF se vendent-elles mieux ?
    python -m src.main sniper-report [--hours 24] [--summary] [--breakdown]  # enchères misées par le sniper
    python -m src.main sniper-backtest [--days 7] [--capital 20000]  # autres réglages rejoués sur les ventes vues
    python -m src.main reset-simulation [--apply]  # efface les mises et ventes simulées
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import statistics as st
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from logging.handlers import RotatingFileHandler

from src.account import bilan_text as account_bilan
from src.backup import backup_loop
from src.config import RARITY_RANK, Settings, get_settings
from src.core import timeutils
from src.core.api import WikiMastersAPI
from src.core.client import WikiMastersClient
from src.core.database import Database
from src.core.endpoints import Endpoints
from src.core.models import MarketPage
from src.core.notifier import DiscordNotifier, Topic
from src.core.pricing import PriceService
from src.core.timeutils import local_dt, next_local, now_local, set_timezone, sleep_until, utcnow
from src.overrides import load as load_overrides
from src.pauses import load as load_pauses
from src.recap import recap_loop
from src.roi import Roi, roi, roi_text
from src.sniper_backtest import cases_from
from src.sniper_backtest import render as backtest_text
from src.sniper_report import breakdown, sniper_report
from src.tools.inspect_api import inspect
from src.workers.account_value import AccountValue
from src.workers.achievements import AchievementsWorker
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context, Worker
from src.workers.collection import CollectionWorker, progress_lines, summary_line, targets, unknown_text
from src.workers.themes import ThemeTagger
from src.workers.collection import refresh as refresh_collection
from src.workers.inventory_manager import InventoryManager
from src.workers.inventory_manager import cli as inventory_cli
from src.workers.profile_guard import ProfileGuard
from src.workers.trade_watch import TradeWatch
from src.workers.value_scan import ValueScan

log = logging.getLogger("wikimastered")


def setup_logging(settings: Settings) -> None:
    (settings.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(settings.data_dir / "logs" / "bot.log", maxBytes=5_000_000, backupCount=5,
                                       encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [console, file_handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpcore", "hpack", "aiosqlite", "discord"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextlib.asynccontextmanager
async def build_context(settings: Settings):
    set_timezone(settings.timezone)
    notifier = DiscordNotifier(settings.discord_webhook_url, settings.discord_alert_webhook_url,
                               settings.discord_mention, settings.dry_run, footer=f"Mode {settings.mode_label}",
                               activity_webhook_url=settings.discord_activity_webhook_url)
    notifier.start()
    endpoints = Endpoints.load(settings.endpoints_file, settings.wm_base_url)
    client = WikiMastersClient(settings, endpoints, notifier)
    db = Database(settings.data_dir / "wikimastered.db")
    await db.connect()
    api = WikiMastersAPI(client)
    ctx = Context(settings, client, api, db, notifier, PriceService(api, db, settings.price_cache_ttl_minutes, settings.price_recent_sales))
    try:
        yield ctx
    finally:
        await client.close()
        await db.close()
        await notifier.close()


async def daily_report(ctx: Context, *, send: bool = True) -> dict:
    stats, description, sections = await daily_report_parts(ctx)
    if send:
        ctx.notifier.info(f"Bilan du {now_local():%d/%m}", description, topic=Topic.REPORT, sections=sections)
    return stats


async def daily_report_parts(ctx: Context) -> tuple[dict, str, list[tuple[str, str]]]:
    """Chiffres du jour, puis le bilan tel qu'envoyé le soir (aussi demandé par /bilan sur Discord)."""
    since = local_dt(now_local().date(), time(0, 0))
    stats = await ctx.db.report(since)
    try:
        stats["balance"] = await asyncio.wait_for(ctx.api.balance(), timeout=60)
    except Exception:  # noqa: BLE001 - le bilan part même sans solde
        stats["balance"] = "?"
    description, sections = report_sections(stats, live_bids=ctx.settings.is_live("place_bid"))
    extra = []
    if not ctx.settings.is_live("place_bid"):  # sniper simulé : on juge la simulation
        extra.append(validation_text(await ctx.db.validation_stats(since, ctx.settings.price_min_sales)))
    if ctx.settings.collection_sets or ctx.settings.collection_titles:
        extra.append(summary_line(await ctx.db.kv_get("collection"), targets(ctx)))
    sections += [as_section(text) for text in extra]
    try:
        value = await account_bilan(ctx, stats["balance"] if isinstance(stats["balance"], int) else None)
    except Exception:  # noqa: BLE001 - le bilan part sans la valeur du compte
        log.exception("Valeur du compte indisponible pour le bilan")
        value = None
    if value:
        sections.insert(0, ("Compte", value))
    return stats, description, sections


def report_sections(stats: dict, *, live_bids: bool) -> tuple[str, list[tuple[str, str]]]:
    """Bilan du soir : le solde en tête, puis un bloc par activité de la journée."""
    balance = stats["balance"]
    description = f"Solde : **{balance} WB**" if isinstance(balance, int) else "Solde : inconnu"
    # Positions simulées et mises de collection réelles (SIMULATE=sniper) : les deux comptes, sinon les cartes
    # gagnées (toujours réelles) semblaient venir des mises simulées.
    rejected = stats.get("bids_rejected", 0)
    counts = [f"{stats['bids_placed']} mise(s) placée(s)" if live_bids or stats["bids_placed"] else "",
              f"{rejected} refusée(s) par le site" if rejected else "",
              f"{stats['bids_simulated']} mise(s) simulée(s)" if not live_bids or stats["bids_simulated"] else ""]
    bids = ", ".join(c for c in counts if c)
    won = [f"{stats['auctions_won']} position(s) pour {stats['spent_won']} WB" if stats["auctions_won"] else "",
           f"{stats.get('won_collection', 0)} carte(s) de collection pour {stats.get('spent_collection', 0)} WB"
           if stats.get("won_collection") else ""]
    if any(won):
        bids += "\nGagnées : " + ", ".join(w for w in won if w)
    sales = [f"{stats['doublons_vendus']} doublon(s) vendu(s) pour {stats['doublons_recette']} WB"]
    if stats["listings_posted"] or stats["listings_scheduled"]:
        sales.append(f"{stats['listings_posted']} mise(s) en vente, {stats['listings_scheduled']} programmée(s)")
    if stats["discarded"]:
        sales.append(f"{stats['discarded']} carte(s) défaussée(s)")
    positions = [f"{stats['positions_held']} réelle(s) détenue(s), {stats['capital_in_positions']} WB immobilisés"]
    if stats.get("positions_held_sim"):
        positions.append(f"{stats['positions_held_sim']} simulée(s) (sniper de test, "
                         f"{stats.get('capital_simulated', 0)} WB fictifs)")
    if stats["positions_sold"]:
        positions.append(f"{stats['positions_sold']} revendue(s), bénéfice {stats['profit']:+d} WB")
    if stats["profit_simulated"]:
        positions.append(f"Bénéfice simulé : {stats['profit_simulated']:+d} WB")
    return description, [("Sniper", bids), ("Ventes", "\n".join(sales)),
                         ("Positions", "\n".join(positions))]


def as_section(text: str) -> tuple[str, str]:
    """« **Titre** : texte » ou « **Titre**\ntexte » devient un bloc (titre, texte)."""
    if text.startswith("**"):
        head, _, rest = text[2:].partition("**")
        return head, rest.removeprefix(" :").strip() or "—"
    return "Divers", text


def validation_text(v: dict[str, int]) -> str:
    """Bloc « faut-il passer en réel ? » du bilan, pendant la simulation."""
    lines = [
        "**Validation de la simulation (depuis le début)**",
        f"Marché observé : {v['observed_today']} ventes aujourd'hui, {v['observed_total']} au total, "
        f"{v['cards_priced']} cartes cotables.",
        f"Positions : {v['sim_bought']} achetées, {v['sim_sold']} revendues ({v['sim_profit']:+d} WB), "
        f"{v['sim_kept']} gardées invendues ({v['sim_kept_cost']} WB immobilisés), {v['sim_held']} en cours.",
    ]
    if v["uncertain"]:
        lines.append(f"Écritures incertaines en attente : {v['uncertain']}.")
    if v["sim_sold"] + v["sim_kept"] < 10:
        lines.append("Pas encore assez de reventes simulées pour conclure (10 au moins).")
    elif v["sim_profit"] - v["sim_kept_cost"] > 0:
        lines.append("Bilan positif, même en comptant les invendues comme perdues : le passage en réel est "
                     "envisageable (DRY_RUN=false).")
    else:
        lines.append("Bilan négatif si on compte les invendues comme perdues : rester en simulation.")
    return "\n".join(lines)


async def report_loop(ctx: Context) -> None:
    """Bilan quotidien, puis entretien de la base. Une erreur n'arrête jamais le bot."""
    while True:
        await sleep_until(next_local(ctx.settings.daily_report_time))
        try:
            await daily_report(ctx)
        except Exception:  # noqa: BLE001 - le bot continue sans bilan
            log.exception("Bilan quotidien impossible")
        try:
            removed = await ctx.db.purge(utcnow())
            log.info("Entretien de la base : %s", ", ".join(f"{t} -{n}" for t, n in removed.items() if n) or "rien")
        except Exception:  # noqa: BLE001
            log.exception("Entretien de la base impossible")


async def sniper_report_loop(ctx: Context) -> None:
    """Chaque matin, le récap de toutes les enchères misées par le sniper sur les dernières heures."""
    while True:
        await sleep_until(next_local(ctx.settings.sniper_report_time))
        try:
            await send_sniper_report(ctx, ctx.settings.sniper_report_hours)
        except Exception:  # noqa: BLE001 - le bot continue sans ce récap
            log.exception("Récap du sniper impossible")


async def send_sniper_report(ctx: Context, hours: float, *, send: bool = True) -> str:
    until = utcnow()
    since = until - timedelta(hours=hours)
    if send:
        title, summary, sections = await sniper_report(ctx.db, since, until, ctx.settings.sale_fee_pct)
        ctx.notifier.info(title, summary, topic=Topic.SNIPER, sections=sections, important=True)
    title, summary, sections = await sniper_report(ctx.db, since, until, ctx.settings.sale_fee_pct, limit=None)
    return "\n\n".join([f"{title}\n{summary}", *(f"== {name} ==\n{body}" for name, body in sections)])


def bid_lift(sales) -> tuple[list[float], int]:
    """(prix final / prix de départ de chaque vente, nombre de ventes au prix de départ, sans surenchère)."""
    ratios = [s["price"] / s["base_amount"] for s in sales if s["base_amount"]]
    single = sum(1 for s in sales if s["base_amount"] and s["price"] <= s["base_amount"])
    return ratios, single


def gif_text(rows) -> str:
    """Les cartes à image animée (GIF) se vendent-elles mieux ? Par rareté : ventes, prix médian, prix final /
    prix de départ, part des ventes sans surenchère ; GIF contre images fixes, depuis le début du suivi."""
    groups: dict[tuple[str, bool], list] = {}
    for r in rows:
        groups.setdefault((r["rarity"], bool(r["is_gif"])), []).append(r)
    if not any(gif for _, gif in groups):
        return "Aucune vente de carte GIF observée pour l'instant (suivi depuis le déploiement)."
    lines = ["Ventes observées depuis le début du suivi des GIF (shiny exclues) :"]
    for rarity in sorted({r for r, _ in groups}, key=lambda r: -RARITY_RANK.get(r, -1)):
        for gif in (True, False):
            sales = groups.get((rarity, gif), [])
            if not sales:
                continue
            ratios, single = bid_lift(sales)
            cards = len({s["card_id"] for s in sales})
            lift = f", final/départ ×{st.median(ratios):.2f}, {single * 100 // max(1, len(ratios))} % sans " \
                   "surenchère" if ratios else ""
            lines.append(f"  {rarity:>2} {'GIF' if gif else 'img'} : {len(sales)} ventes ({cards} cartes), "
                         f"prix médian {st.median(s['price'] for s in sales):.0f}{lift}")
    return "\n".join(lines)


PROFILE_HIDDEN = ("email", "token", "secret", "password", "phone", "ip_address")  # jamais affichés, même collés ensuite


def profile_text(profile) -> str:
    """Réponse brute de `get_my_profile` (solde, paquets, régénération, pity…), une ligne par champ.
    Les champs personnels (e-mail, jetons…) sont masqués."""
    if not isinstance(profile, dict):
        return f"Réponse inattendue : {json.dumps(profile, ensure_ascii=False)[:300]}"
    lines = ["Profil lu sur le site (get_my_profile) :"]
    for key in sorted(profile):
        value = "***" if any(h in key.lower() for h in PROFILE_HIDDEN) else json.dumps(profile[key], ensure_ascii=False)
        lines.append(f"  {key} = {value[:200]}")
    return "\n".join(lines)


HOURS_MIN_SALES = 20  # en dessous, la médiane d'une heure ne dit rien : l'heure est marquée « ? »


def hours_text(rows, rarities: list[str]) -> str:
    """À quelle heure de fin (Paris) les enchères se vendent-elles le mieux ? Par rareté et par heure : ventes,
    prix médian, prix final / prix de départ, part des ventes sans surenchère."""
    if not rows:
        return "Aucune vente observée pour ces raretés."
    ends = [datetime.fromisoformat(r["ended_at"]).astimezone(timeutils.PARIS) for r in rows]
    lines = [f"Ventes observées du {min(ends):%d/%m %H:%M} au {max(ends):%d/%m %H:%M} (shiny et ventes suspectes "
             f"exclues), par heure de fin (Paris). Seules les enchères vendues sont vues : le volume dépend aussi de "
             f"l'offre, pas seulement de l'affluence. « ? » : moins de {HOURS_MIN_SALES} ventes."]
    for rarity in sorted(rarities, key=lambda r: -RARITY_RANK.get(r, -1)):
        sales = [(r, e) for r, e in zip(rows, ends, strict=True) if r["rarity"] == rarity]
        if not sales:
            continue
        lines.append(f"\n{rarity} : {len(sales)} ventes")
        lines.append("  heure   ventes   prix médian   final/départ   sans surenchère")
        for hour in range(24):
            here = [r for r, e in sales if e.hour == hour]
            if not here:
                continue
            ratios, single = bid_lift(here)
            ratio = f"×{st.median(ratios):.2f}" if ratios else "-"
            part = f"{single * 100 // len(ratios)} %" if ratios else "-"
            flag = " ?" if len(here) < HOURS_MIN_SALES else ""
            lines.append(f"  {hour:02d}h    {len(here):>6}   {st.median(r['price'] for r in here):>11.0f}   "
                         f"{ratio:>12}   {part:>15}{flag}")
    return "\n".join(lines)


PRICES_SHOWN = 15  # ventes observées affichées par rareté


def prices_text(title: str, observed, bought, selling, quotes: dict, site: dict, me: str | None, now: datetime,
                md: bool = False) -> str:
    """Ventes observées d'une carte, estimation du bot, achats du bot et ventes en cours (commande prices).
    `md` : version Discord (/prix), lignes courtes en markdown, sans bloc de code."""
    def paris(value) -> str:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        text = f"{moment.astimezone(timeutils.PARIS):%d/%m %H:%M}"
        return f"`{text}`" if md else text

    if md:
        lines = ["Ventes finies avec au moins une mise, vues par le bot (une mise de dernière seconde peut manquer)."]
    else:
        lines = [f"{title}", "Ventes observées par le bot : enchères finies avec au moins une mise, prix lu moins de "
                 "2 min avant la fin (une mise de dernière seconde peut manquer). Une vente sans mise n'est jamais "
                 "comptée."]
    rarities = sorted({r["rarity"] for r in observed} | set(quotes), key=lambda r: -RARITY_RANK.get(r, -1))
    if not rarities:
        lines.append("  aucune")
    for rarity in rarities:
        sales = [r for r in observed if r["rarity"] == rarity and not r["is_shiny"]]
        quote = quotes.get(rarity)
        estimate = f"~{quote.average:.0f} WB" if quote and quote.average is not None else "aucune"
        average = site.get(rarity, (None, None))[0]
        site_text = f"{average:.0f} WB" if average is not None else "inconnue"
        if md:
            lines.append(f"\n**{rarity}** · {len(sales)} vente(s) · estimation du bot **{estimate}** (médiane des 5 "
                         f"dernières, plafonnée par la cote du site : {site_text})")
        else:
            lines.append(f"\n{rarity} : {len(sales)} vente(s) observée(s) · estimation du bot {estimate} (médiane des "
                         f"5 dernières, plafonnée par la moyenne du site : {site_text})")
        for r in sales[:PRICES_SHOWN]:
            notes = []
            if r["base_amount"] and r["price"] <= r["base_amount"]:
                notes.append("sans surenchère" if md else "au prix de départ, sans surenchère")
            if r["suspect"]:
                notes.append("suspecte, ignorée")
            start = f"départ {r['base_amount']}" if r["base_amount"] else "départ ?"
            if md:
                lines.append(f"{paris(r['ended_at'])} **{r['price']} WB** ({start})"
                             + (f" · {', '.join(notes)}" if notes else ""))
                continue
            notes_text = "  " + ", ".join(notes) if notes else ""
            lines.append(f"  {paris(r['ended_at'])}  {r['price']:>6} WB  ({start}){notes_text}")
        if len(sales) > PRICES_SHOWN:
            lines.append(f"  … et {len(sales) - PRICES_SHOWN} plus ancienne(s)")
    if bought:
        lines.append("\nAchats du bot :")
        for b in bought:
            kind = "simulé" if b["dry_run"] else "réel"
            sold = f", revendue {b['sale_price']}" if b["sale_price"] else ""
            ref = f"{b['reference_price']:.0f}" if b["reference_price"] else "?"
            lines.append(f"  {paris(b['placed_at'])}  {b['rarity']}  {kind} : {b['amount']} WB, estimée ~{ref}{sold}")
    heading = f"En vente maintenant ({len(selling)})" if selling else "En vente maintenant : aucune"
    lines.append(f"\n**{heading}**" if md else f"\n{heading}" + (" :" if selling else ""))
    for a in sorted(selling, key=lambda a: a.min_bid):
        price = f"mise actuelle {a.current_bid}" if a.current_bid else f"départ {a.base_amount}, aucune mise"
        mine = "  (toi)" if me and (a.seller_id or "").lower() == me.lower() else ""
        left = int(a.seconds_left(now) // 60)
        if md:
            amount = f"**{a.current_bid} WB** (mise)" if a.current_bid else \
                f"**{a.base_amount} WB** (départ, aucune mise)"
            lines.append(f"`fin dans {left // 60} h {left % 60:02d}` **{a.rarity or '?'}**{' ✨' if a.shiny else ''} "
                         f"{amount}{' · toi' if mine else ''}")
            continue
        lines.append(f"  {a.rarity or '?':<3}{' ✨' if a.shiny else ''} {price}, fin dans {left // 60} h "
                     f"{left % 60:02d}{mine}")
    return "\n".join(lines)


async def prices_status(ctx: Context, title: str, md: bool = False) -> str:
    observed = await ctx.db.observed_for_title(title)
    auctions, _ = await ctx.api.market_page(sort="ending_soon", q=title, limit=50)
    now = utcnow()
    selling = [a for a in auctions if a.title.casefold() == title.casefold() and a.seconds_left(now) > 0]
    card_ids = {r["card_id"] for r in observed} | {a.catalog_id for a in selling}
    if not card_ids:
        return f"« {title} » : ni vente observée ni vente en cours (titre exact de la carte, sans « (L) »)."
    quotes, site = {}, {}
    for card_id in card_ids:  # en principe une seule carte par titre
        quotes.update({r: q for r, q in (await ctx.prices.quotes(card_id)).items() if r in RARITY_RANK})
        cached = await ctx.db.price_get(card_id)
        site.update(cached[1] if cached else {})
    name = selling[0].title if selling else title
    return prices_text(name, observed, await ctx.db.bids_for_title(title), selling, quotes, site,
                       ctx.client.user_id, now, md)


async def roi_report(ctx: Context, days: int) -> Roi:
    today = now_local().date()
    rows = await ctx.db.position_results(local_dt(today - timedelta(days=days - 1), time(0, 0)))
    held = await ctx.db.positions_summary()
    return roi(rows, days, today, ctx.settings.sale_fee_pct, (held["real_count"], held["real_cost"]))


async def roi_status(ctx: Context, days: int) -> str:
    return roi_text(await roi_report(ctx, days))


async def heartbeat(ctx: Context) -> None:
    path = ctx.settings.data_dir / "heartbeat"
    while True:
        path.write_text(now_local().isoformat(), encoding="utf-8")
        ctx.client.check_cookie_drop()
        await asyncio.sleep(30)


SUPERVISE_BASE_SECONDS = 30  # 1re relance d'une tâche arrêtée, doublée à chaque nouvel arrêt (10 min au plus)


async def supervise(name: str, start: Callable[[], Awaitable[None]], notifier) -> None:
    """Fait tourner une tâche et la relance si elle s'arrête : un worker en panne ne coupe jamais les autres."""
    failures = 0
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await start()
            reason = "terminée sans raison"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - la tâche est relancée, les autres continuent
            log.exception("La tâche %s s'est arrêtée", name)
            reason = f"{type(exc).__name__} : {exc}"[:300]
        failures = 1 if asyncio.get_running_loop().time() - started > 3600 else failures + 1  # relance rapide après 1 h stable
        delay = min(600, SUPERVISE_BASE_SECONDS * 2 ** (failures - 1))
        log.error("tâche %s relancée dans %d s (%s)", name, delay, reason)
        if failures in (1, 5):
            notifier.critical(f"{name} : tâche arrêtée, relancée", f"{reason}\nLes autres workers continuent. "
                              f"Nouvel essai dans {delay} s.")
        await asyncio.sleep(delay)


async def run_bot(settings: Settings) -> None:
    async with build_context(settings) as ctx:
        await load_pauses(ctx)
        if changed := await load_overrides(ctx):
            log.info("Réglages changés sur Discord : %s", ", ".join(f"{k}={v}" for k, v in changed.items()))
        workers: list[Worker] = []
        if settings.enable_sniper:
            workers.append(AuctionSniper(ctx))
        if settings.enable_inventory:
            workers.append(InventoryManager(ctx))
        if settings.collection_sets or settings.collection_titles:
            workers.append(CollectionWorker(ctx))
        if settings.enable_theme_tags:
            workers.append(ThemeTagger(ctx))
        if settings.enable_achievements:
            workers.append(AchievementsWorker(ctx))
        if settings.enable_value_scan:
            workers.append(ValueScan(ctx))
        if settings.enable_account_value:
            workers.append(AccountValue(ctx))
        if settings.enable_trade_watch:
            workers.append(TradeWatch(ctx))
        if settings.enable_profile_guard and settings.supabase_anon_key:
            workers.insert(0, ProfileGuard(ctx))  # en premier : une sanction arrête le bot avant les autres
        elif settings.enable_profile_guard:
            ctx.notifier.warning("Garde des sanctions inactive", "SUPABASE_ANON_KEY manquante : le profil ne peut "
                                 "pas être lu, une sanction du site n'arrêterait pas le bot.")
        if ctx.client.halted:
            ctx.notifier.critical("Bot à l'arrêt", f"{ctx.client.halted}\nAucune requête ne part. Pour reprendre : "
                                  "`rm data/HALT` dans la console du conteneur, puis redémarre la stack.")

        mode = settings.mode_label
        names = ", ".join(w.name for w in workers) or "aucun"
        log.info("Démarrage en mode %s — workers : %s — joueur %s", mode, names, ctx.client.user_id or "?")
        ctx.notifier.info("Bot démarré", f"Mode {mode}\nWorkers : {names}\nSession : {ctx.client.session_status()}",
                          important=True)

        # Chaque tâche est supervisée à part : si l'une plante, elle est relancée et les autres continuent.
        started_at = utcnow()
        jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = [(w.name, w.run) for w in workers]
        jobs += [("report", lambda: report_loop(ctx)), ("heartbeat", lambda: heartbeat(ctx))]
        if settings.recap_interval_hours > 0:
            jobs.append(("recap", lambda: recap_loop(ctx, workers, started_at)))
        if settings.enable_sniper and settings.sniper_report_hours > 0:
            jobs.append(("sniper-report", lambda: sniper_report_loop(ctx)))
        if settings.enable_backup and settings.backup_keep > 0:
            jobs.append(("sauvegarde", lambda: backup_loop(ctx)))
        if settings.discord_bot_token and settings.discord_owner_id <= 0:
            ctx.notifier.warning("Commandes Discord inactives", "DISCORD_OWNER_ID manquant ou pas un nombre : il faut "
                                 "ton identifiant utilisateur (Mode développeur, clic droit sur ton pseudo > Copier "
                                 "l'identifiant utilisateur), pas ton pseudo.")
        elif settings.discord_bot_token:
            if settings.discord_guild_id < 0:
                ctx.notifier.warning("DISCORD_GUILD_ID ignoré", "Pas un nombre (clic droit sur le serveur > Copier "
                                     "l'identifiant du serveur) : commandes globales, jusqu'à 1 h pour apparaître.")
            from src.discord_bot import run_discord  # discord.py chargé seulement avec un bot configuré

            jobs.append(("discord", lambda: run_discord(ctx, workers, sys.modules[__name__], started_at)))
        tasks = [asyncio.create_task(supervise(name, start, ctx.notifier), name=name) for name, start in jobs]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        log.info("Arrêt en cours…")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ctx.notifier.warning("Bot arrêté")


async def reset_simulation(settings: Settings, apply: bool) -> None:
    """Repart d'une simulation propre : les réelles, les ventes observées et le reste sont gardés."""
    db = Database(settings.data_dir / "wikimastered.db")
    await db.connect()
    try:
        counts = await db.reset_simulation(apply=apply)
    finally:
        await db.close()
    what = f"{counts['mises']} mise(s) simulée(s) et {counts['ventes']} vente(s) simulée(s)"
    if apply:
        print(f"Effacé : {what}. Le bilan repart de zéro pour la validation de la simulation.")
    else:
        print(f"À effacer : {what}. Relance avec --apply pour les effacer (les données réelles sont gardées).")


async def sniper_backtest(settings: Settings, days: float, capital: int, resale: float, rarity: str) -> None:
    """Réglages du sniper rejoués sur les ventes observées : base seule, aucune requête au site."""
    db = Database(settings.data_dir / "wikimastered.db")
    await db.connect()
    try:
        since = utcnow() - timedelta(days=days)
        cases = cases_from(await db.backtest_sales(rarity), await db.site_average_history(rarity), since, settings)
        actual = await db.position_bids_outcome(since, rarity)
    finally:
        await db.close()
    budget = int(capital * settings.portfolio_capital_ratio / max(1, settings.portfolio_slots))
    print(backtest_text(cases, actual, settings, budget, days, resale))


async def collection_status(ctx: Context) -> None:
    """Relit la collection à compléter et affiche possédées / manquantes."""
    s = ctx.settings
    if not (s.collection_sets or s.collection_titles):
        print("Aucune collection configurée : ajoute COLLECTION_SETS=jours (et COLLECTION_MAX_PRICE).")
        return
    print(f"COLLECTION_SETS = {s.collection_sets}  COLLECTION_TITLES = {s.collection_titles}")
    state = await refresh_collection(ctx)
    if state["unknown_sets"]:
        print(unknown_text(s, state["unknown_sets"]))
    by_key = targets(ctx)
    print("\n".join(progress_lines(state, by_key, utcnow(), days=30)))
    print("Manquantes : " + (", ".join(by_key.get(k, k) for k in state["missing"]) or "aucune"))
    if state["unmatched"]:
        print("Titres trouvés mais non reconnus (format différent ?) : " + ", ".join(state["unmatched"]))
    print(f"Mise maximale par carte manquante : {s.collection_max_price} WB")
    if state.get("tag"):
        live = "" if s.is_live("tag_card") else " (simulé : DRY_RUN)"
        print(f"Étiquette « {state['tag']} » : {state['tagged']} ajoutée(s){live}, {state['to_tag']} restante(s)")
    if state.get("tag_error"):
        print(f"Étiquettes : {state['tag_error']}")


async def themes_status(ctx: Context) -> None:
    """Classe toute la collection par thème (Wikidata) et montre ce que le worker étiquettes poserait, sans rien
    écrire. Les faits lus sur Wikidata sont gardés en base, comme par le worker."""
    from src.core.wikidata import Wikidata
    from src.workers import themes

    wikidata = Wikidata()
    try:
        state = await themes.run_pass(ctx, wikidata, write=False)
    finally:
        await wikidata.close()
    print(themes.summary(state))
    if state["untagged"]:
        print("Sans thème (corrections possibles dans config/etiquettes.yaml) : " + ", ".join(state["untagged"]))
    print(f"À poser : {state['to_add']} ; « {ctx.settings.theme_duplicate_tag} » à retirer : {state['to_remove']}")
    if state["missing_tags"]:
        print("Étiquettes à créer sur le site : " + ", ".join(state["missing_tags"]))
    print("Champ « category » des cartes : " + ", ".join(f"{k} ({n})" for k, n in state["categories"].items()))


async def market_status(ctx: Context, pages: int, sort: str, rarity: str) -> None:
    """Lit quelques pages du marché (50 enchères, 3 s d'écart) et montre où sont les fins imminentes,
    avec les en-têtes de cache de la réponse (une réponse en cache peut dater de plusieurs secondes)."""
    await ctx.client._maybe_refresh()
    for page in range(1, pages + 1):
        if page > 1:
            await asyncio.sleep(3)
        resp = await ctx.client._send(ctx.client.endpoints.build("market_page", page=page, sort=sort, rarity=rarity))
        now = utcnow()
        cache = {k: v for k, v in resp.headers.items()
                 if k.lower() in ("age", "cache-control", "date", "etag", "last-modified", "expires")
                 or "cache" in k.lower()}
        if resp.status_code != 200:
            print(f"page {page} : HTTP {resp.status_code} {resp.text[:200]}")
            break
        result = MarketPage.model_validate(resp.json())
        auctions, more = result.items(), result.hasMore
        print(f"page {page} : en-têtes {cache}")
        left = sorted(a.seconds_left(now) for a in auctions)
        statuses = dict(Counter(a.status for a in auctions))
        span = f"fins de {left[0]:+.0f} s à {left[-1]:+.0f} s" if left else "vide"
        print(f"page {page} : {len(auctions)} enchères, {sum(x <= 0 for x in left)} terminées, "
              f"{sum(0 < x <= 60 for x in left)} finissent dans la minute, {span}, statuts {statuses}, "
              f"hasMore={more}")
        for a in auctions[:3] + auctions[-2:]:
            print(f"    {a.seconds_left(now):+8.0f} s  {a.status:<8} {a.rarity or '?':<3} {a.title[:50]}")
        if not more:
            break


async def check_session(ctx: Context) -> str:
    balance = await asyncio.wait_for(ctx.api.balance(), timeout=120)
    stats = await ctx.api.collection_stats()
    return (f"Session OK — joueur {ctx.client.user_id or '?'} — solde {balance} WB — {stats.total} exemplaires\n"
            f"Session : {ctx.client.session_status()}")


async def profile_status(ctx: Context) -> str:
    status, data = await ctx.client.supabase("POST", "rpc/get_my_profile", json_body={})
    return profile_text(data) if status == 200 else f"HTTP {status} : {profile_text(data)}"


async def gifs_status(ctx: Context) -> str:
    return gif_text(await ctx.db.gif_stats())


async def hours_status(ctx: Context, rarity: str) -> str:
    rarities = [r.strip().upper() for r in rarity.split(",") if r.strip()]
    return hours_text(await ctx.db.hour_stats(rarities), rarities)


async def sniper_report_status(ctx: Context, hours: float, summary: bool, with_breakdown: bool = False) -> str:
    text = await send_sniper_report(ctx, hours, send=False)
    text = text.split("\n\n== ", 1)[0] if summary else text
    if with_breakdown:
        rows = await ctx.db.bids_since(utcnow() - timedelta(hours=hours))
        text += "\n\n" + breakdown(rows, ctx.settings.sale_fee_pct)
    return text


def in_context(settings: Settings, command: Callable[[Context], Awaitable[object]]) -> None:
    """Exécute une commande avec le client, la base et Discord ; affiche son résultat, s'il y en a un."""
    async def _run() -> None:
        async with build_context(settings) as ctx:
            result = await command(ctx)
            if result is not None:
                print(result)
    asyncio.run(_run())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wikimastered")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="lance le bot")
    sub.add_parser("check", help="vérifie la session")
    sub.add_parser("report", help="envoie le bilan du jour")
    ins = sub.add_parser("inspect", help="capture les vraies réponses JSON (lecture seule)")
    ins.add_argument("--out", default=None)
    sub.add_parser("collection", help="collection à compléter : possédées / manquantes (lecture seule)")
    sub.add_parser("etiquettes", help="thèmes de toute la collection (Wikidata) et étiquettes à poser (sans écrire)")
    mk = sub.add_parser("market", help="où sont les fins imminentes dans le marché (lecture seule)")
    mk.add_argument("--pages", type=int, default=4)
    mk.add_argument("--sort", default="ending_soon")
    mk.add_argument("--rarity", default="", help="C, PC, R, SR, UR, L…")
    sim = sub.add_parser("reset-simulation", help="efface les mises et ventes simulées (sans --apply : compte)")
    sim.add_argument("--apply", action="store_true", help="efface vraiment")
    sub.add_parser("gifs", help="les cartes GIF se vendent-elles mieux ? (ventes observées, lecture seule)")
    sub.add_parser("profile", help="profil du joueur lu sur le site : solde, paquets, pity… (lecture seule)")
    hr = sub.add_parser("hours", help="à quelle heure de fin les enchères se vendent-elles le mieux ? (lecture seule)")
    hr.add_argument("--rarity", default="UR,L", help="raretés séparées par des virgules (défaut UR,L)")
    snr = sub.add_parser("sniper-report", help="toutes les enchères misées sur les N dernières heures (lecture seule)")
    snr.add_argument("--hours", type=float, default=24)
    snr.add_argument("--summary", action="store_true", help="les chiffres seulement, sans la liste des enchères")
    snr.add_argument("--breakdown", action="store_true",
                     help="taux de victoire des positions par critère (enchère déjà disputée, mise / estimation…)")
    bt = sub.add_parser("sniper-backtest", help="autres réglages du sniper rejoués sur les ventes observées "
                                                "(lecture seule, sans requête au site)")
    bt.add_argument("--days", type=float, default=7)
    bt.add_argument("--capital", type=int, default=20000, help="WikiBidous + positions (budget d'une position)")
    bt.add_argument("--resale", type=float, default=1.0, help="revente = estimation × ce facteur (défaut 1)")
    bt.add_argument("--rarity", default="L")
    roi = sub.add_parser("roi", help="bénéfice et ROI des reventes du sniper, jour par jour (lecture seule)")
    roi.add_argument("--days", type=int, default=7)
    pr = sub.add_parser("prices", help="ventes observées d'une carte, estimation du bot et ventes en cours "
                                       "(lecture seule)")
    pr.add_argument("title", help="titre exact de la carte, entre guillemets")
    inv = sub.add_parser("inventory", help="analyse / vente / défausse")
    inv.add_argument("action", choices=["scan", "report", "list", "discard"])
    inv.add_argument("--apply", action="store_true", help="exécute (simulé si DRY_RUN=true)")
    inv.add_argument("--lookups", type=int, default=None,
                     help="report : cotes demandées au site (route limitée ; défaut SELL_PRICE_LOOKUPS_PER_RUN)")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)
    command = args.command or "run"
    if command == "run":
        asyncio.run(run_bot(settings))
        return
    if command == "reset-simulation":  # base seule : aucune requête au site
        asyncio.run(reset_simulation(settings, args.apply))
        return
    if command == "sniper-backtest":  # base seule : aucune requête au site
        asyncio.run(sniper_backtest(settings, args.days, args.capital, args.resale, args.rarity.upper()))
        return
    commands: dict[str, Callable[[Context], Awaitable[object]]] = {
        "check": check_session,
        "report": daily_report,
        "inspect": lambda ctx: inspect(ctx, args.out),
        "collection": collection_status,
        "etiquettes": themes_status,
        "market": lambda ctx: market_status(ctx, max(1, min(args.pages, 10)), args.sort, args.rarity.upper()),
        "gifs": gifs_status,
        "profile": profile_status,
        "hours": lambda ctx: hours_status(ctx, args.rarity),
        "sniper-report": lambda ctx: sniper_report_status(ctx, args.hours, args.summary, args.breakdown),
        "prices": lambda ctx: prices_status(ctx, args.title),
        "roi": lambda ctx: roi_status(ctx, max(1, args.days)),
        "inventory": lambda ctx: inventory_cli(ctx, args.action, args.apply, args.lookups),
    }
    in_context(settings, commands[command])


if __name__ == "__main__":
    main()
