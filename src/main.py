"""Point d'entrée : orchestre les workers et expose quelques commandes CLI.

    python -m src.main run                      # le bot (commande par défaut du conteneur)
    python -m src.main check                    # vérifie la session et affiche le solde
    python -m src.main inspect                  # capture et compare les vraies réponses JSON
    python -m src.main inventory scan|list|discard [--apply]
    python -m src.main report                   # envoie le bilan du jour sur Discord
    python -m src.main trends [--days 7]        # les tendances auraient-elles rapporté ?
    python -m src.main packs [--hours 24]       # paquets ouverts récemment
    python -m src.main collection               # collection à compléter (jours de l'année…)
    python -m src.main market [--pages 4] [--rarity UR]  # où sont les fins imminentes dans le marché
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone
from logging.handlers import RotatingFileHandler

from src.config import RARITY_RANK, Settings, get_settings
from src.core.api import WikiMastersAPI
from src.core.client import WikiMastersClient
from src.core.database import Database
from src.core.endpoints import Endpoints
from src.core.models import MarketPage
from src.core.notifier import DiscordNotifier, Topic
from src.core.pricing import PriceService
from src.core.timeutils import local_dt, next_local, now_local, set_timezone, sleep_until
from src.recap import recap_loop
from src.sniper_report import sniper_report
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context, Worker
from src.workers.achievements import AchievementsWorker
from src.workers.collection import CollectionWorker, summary_line, targets
from src.workers.collection import refresh as refresh_collection
from src.workers.google_trends import GoogleTrends
from src.workers.inventory_manager import InventoryManager
from src.workers.inventory_manager import cli as inventory_cli
from src.workers.pack_opener import PackOpener
from src.workers.trend_report import day_line, trends_text
from src.workers.wiki_trends import WikiTrends

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
    for noisy in ("httpx", "httpcore", "hpack", "aiosqlite"):
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
    since = local_dt(now_local().date(), time(0, 0))
    stats = await ctx.db.report(since)
    try:
        stats["balance"] = await asyncio.wait_for(ctx.api.balance(), timeout=60)
    except Exception:  # noqa: BLE001 - le bilan part même sans solde
        stats["balance"] = "?"
    pulls = stats.pop("pulls_by_rarity")
    description, sections = report_sections(stats, pulls, live_bids=ctx.settings.is_live("place_bid"))
    extra = []
    if not ctx.settings.is_live("place_bid"):  # sniper simulé : on juge la simulation
        extra.append(validation_text(await ctx.db.validation_stats(since, ctx.settings.price_min_sales)))
    if ctx.settings.collection_sets or ctx.settings.collection_titles:
        extra.append(summary_line(await ctx.db.kv_get("collection"), targets(ctx)))
    if ctx.settings.enable_google_trends or ctx.settings.enable_wiki_trends:
        extra.append(await day_line(ctx.db, datetime.now(timezone.utc)))
    sections += [as_section(text) for text in extra]
    if now_local().weekday() == 6:  # dimanche : mesure des tendances de la semaine (détail : commande trends)
        text = await trends_text(ctx.db, 7, await min_profit(ctx), datetime.now(timezone.utc), detailed=False)
        sections.append(("Tendances de la semaine", text))
    if send:
        ctx.notifier.info(f"Bilan du {now_local():%d/%m}", description, topic=Topic.REPORT, sections=sections)
    return stats


def report_sections(stats: dict, pulls: dict[str, int], *, live_bids: bool) -> tuple[str, list[tuple[str, str]]]:
    """Bilan du soir : le solde en tête, puis un bloc par activité de la journée."""
    balance = stats["balance"]
    description = f"Solde : **{balance} WB**" if isinstance(balance, int) else "Solde : inconnu"
    rarities = " · ".join(f"{r} {n}" for r, n in sorted(pulls.items(), key=lambda p: RARITY_RANK.get(p[0], -1)))
    packs = f"{stats['boosters_ouverts']} booster(s), {stats['cards_pulled']} carte(s)"
    if rarities:
        packs += f"\n{rarities}"
    if stats["shiny_pulled"]:
        packs += f"\n✨ {stats['shiny_pulled']} shiny"
    # Positions simulées et mises de collection réelles (SIMULATE=sniper) : les deux comptes, sinon les cartes
    # gagnées (toujours réelles) semblaient venir des mises simulées.
    counts = [f"{stats['bids_placed']} mise(s) placée(s)" if live_bids or stats["bids_placed"] else "",
              f"{stats['bids_simulated']} mise(s) simulée(s)" if not live_bids or stats["bids_simulated"] else ""]
    bids = ", ".join(c for c in counts if c)
    if stats["auctions_won"]:
        bids += f"\n{stats['auctions_won']} gagnée(s) pour {stats['spent_won']} WB"
    sales = [f"{stats['doublons_vendus']} doublon(s) vendu(s) pour {stats['doublons_recette']} WB"]
    if stats["listings_posted"] or stats["listings_scheduled"]:
        sales.append(f"{stats['listings_posted']} mise(s) en vente, {stats['listings_scheduled']} programmée(s)")
    if stats["discarded"]:
        sales.append(f"{stats['discarded']} carte(s) défaussée(s)")
    positions = [f"{stats['positions_held']} détenue(s), {stats['capital_in_positions']} WB immobilisés"]
    if stats["positions_sold"]:
        positions.append(f"{stats['positions_sold']} revendue(s), bénéfice {stats['profit']:+d} WB")
    if stats["profit_simulated"]:
        positions.append(f"Bénéfice simulé : {stats['profit_simulated']:+d} WB")
    return description, [("Paquets", packs), ("Sniper", bids), ("Ventes", "\n".join(sales)),
                         ("Positions", "\n".join(positions))]


def as_section(text: str) -> tuple[str, str]:
    """« **Titre** : texte » ou « **Titre**\ntexte » devient un bloc (titre, texte)."""
    if text.startswith("**"):
        head, _, rest = text[2:].partition("**")
        return head, rest.removeprefix(" :").strip() or "—"
    return "Divers", text


async def min_profit(ctx: Context) -> int:
    """Bénéfice exigé d'une position, d'après le solde actuel."""
    s = ctx.settings
    try:
        balance = await asyncio.wait_for(ctx.api.balance(), timeout=60)
    except Exception:  # noqa: BLE001
        return s.portfolio_min_profit
    budget = int(balance * s.portfolio_capital_ratio / max(1, s.portfolio_slots))
    return max(s.portfolio_min_profit, int(budget * s.portfolio_min_profit_ratio))


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
            removed = await ctx.db.purge(datetime.now(timezone.utc))
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
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    if send:
        title, summary, sections = await sniper_report(ctx.db, since, until, ctx.settings.sale_fee_pct)
        ctx.notifier.info(title, summary, topic=Topic.SNIPER, sections=sections, important=True)
    title, summary, sections = await sniper_report(ctx.db, since, until, ctx.settings.sale_fee_pct, limit=None)
    return "\n\n".join([f"{title}\n{summary}", *(f"== {name} ==\n{body}" for name, body in sections)])


def gif_text(rows) -> str:
    """Les cartes à image animée (GIF) se vendent-elles mieux ? Par rareté : ventes, prix médian, prix final /
    prix de départ, part des ventes sans surenchère ; GIF contre images fixes, depuis le début du suivi."""
    import statistics as st
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
            ratios = [s["price"] / s["base_amount"] for s in sales if s["base_amount"]]
            single = sum(1 for s in sales if s["base_amount"] and s["price"] <= s["base_amount"])
            cards = len({s["card_id"] for s in sales})
            lift = f", final/départ ×{st.median(ratios):.2f}, {single * 100 // max(1, len(ratios))} % sans " \
                   "surenchère" if ratios else ""
            lines.append(f"  {rarity:>2} {'GIF' if gif else 'img'} : {len(sales)} ventes ({cards} cartes), "
                         f"prix médian {st.median(s['price'] for s in sales):.0f}{lift}")
    return "\n".join(lines)


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
        workers: list[Worker] = []
        if settings.enable_pack_opener:
            workers.append(PackOpener(ctx))
        if settings.enable_sniper:
            workers.append(AuctionSniper(ctx))
        if settings.enable_inventory:
            workers.append(InventoryManager(ctx))
        if settings.enable_wiki_trends:
            workers.append(WikiTrends(ctx))
        if settings.enable_google_trends:
            workers.append(GoogleTrends(ctx))
        if settings.collection_sets or settings.collection_titles:
            workers.append(CollectionWorker(ctx))
        if settings.enable_achievements:
            workers.append(AchievementsWorker(ctx))

        mode = settings.mode_label
        names = ", ".join(w.name for w in workers) or "aucun"
        log.info("Démarrage en mode %s — workers : %s — joueur %s", mode, names, ctx.client.user_id or "?")
        ctx.notifier.info("Bot démarré", f"Mode {mode}\nWorkers : {names}\nSession : {ctx.client.session_status()}",
                          important=True)

        # Chaque tâche est supervisée à part : si l'une plante, elle est relancée et les autres continuent.
        started_at = datetime.now(timezone.utc)
        jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = [(w.name, w.run) for w in workers]
        jobs += [("report", lambda: report_loop(ctx)), ("heartbeat", lambda: heartbeat(ctx))]
        if settings.recap_interval_hours > 0:
            jobs.append(("recap", lambda: recap_loop(ctx, workers, started_at)))
        if settings.enable_sniper and settings.sniper_report_hours > 0:
            jobs.append(("sniper-report", lambda: sniper_report_loop(ctx)))
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


async def packs_status(settings: Settings, hours: int) -> None:
    """Paquets ouverts ces dernières heures (lecture de la base, aucune requête)."""
    async with build_context(settings) as ctx:
        rows = await ctx.db.recent_pulls(datetime.now(timezone.utc) - timedelta(hours=hours))
        if not settings.is_live("open_pack"):
            print("DRY_RUN : les paquets ne sont PAS ouverts. Ajoute LIVE_WRITES=open_pack pour les ouvrir quand même.")
        from src.workers.pack_opener import checks_report
        print("Vérifications humaines :")
        print("\n".join(await checks_report(ctx.db)))
        if not rows:
            print(f"Aucune carte tirée ces {hours} dernières heures.")
            return
        by_rarity: dict[str, int] = {}
        for r in rows:
            by_rarity[r["rarity"] or "?"] = by_rarity.get(r["rarity"] or "?", 0) + 1
        last = datetime.fromisoformat(rows[0]["opened_at"]).astimezone()
        print(f"{len(rows)} carte(s) tirée(s) en {hours} h, dernière ouverture le {last:%d/%m à %H:%M}")
        print("Par rareté : " + " ".join(f"{k}:{v}" for k, v in sorted(by_rarity.items())))
        for r in rows[:15]:
            when = datetime.fromisoformat(r["opened_at"]).astimezone()
            print(f"  {when:%d/%m %H:%M}  {r['rarity'] or '?':>2}{' ✨' if r['is_shiny'] else '  '} {r['title']}")


async def collection_status(settings: Settings) -> None:
    """Relit la collection à compléter et affiche possédées / manquantes."""
    async with build_context(settings) as ctx:
        if not (settings.collection_sets or settings.collection_titles):
            print("Aucune collection configurée : ajoute COLLECTION_SETS=jours (et COLLECTION_MAX_PRICE).")
            return
        print(f"COLLECTION_SETS = {settings.collection_sets}  COLLECTION_TITLES = {settings.collection_titles}")
        state = await refresh_collection(ctx)
        if state["unknown_sets"]:
            print(f"Ensemble(s) inconnu(s) : {state['unknown_sets']} — valeur attendue : jours")
        by_key = targets(ctx)
        from src.workers.collection import progress_lines
        print("\n".join(progress_lines(state, by_key, datetime.now(timezone.utc), days=30)))
        print("Manquantes : " + (", ".join(by_key.get(k, k) for k in state["missing"]) or "aucune"))
        if state["unmatched"]:
            print("Titres trouvés mais non reconnus (format différent ?) : " + ", ".join(state["unmatched"]))
        print(f"Mise maximale par carte manquante : {settings.collection_max_price} WB")
        if state.get("tag"):
            live = "" if settings.is_live("tag_card") else " (simulé : DRY_RUN)"
            print(f"Étiquette « {state['tag']} » : {state['tagged']} ajoutée(s){live}, {state['to_tag']} restante(s)")
        if state.get("tag_error"):
            print(f"Étiquettes : {state['tag_error']}")


async def market_status(settings: Settings, pages: int, sort: str, rarity: str) -> None:
    """Lit quelques pages du marché (50 enchères, 3 s d'écart) et montre où sont les fins imminentes,
    avec les en-têtes de cache de la réponse (une réponse en cache peut dater de plusieurs secondes)."""
    async with build_context(settings) as ctx:
        await ctx.client._maybe_refresh()
        for page in range(1, pages + 1):
            if page > 1:
                await asyncio.sleep(3)
            resp = await ctx.client._send(ctx.client.endpoints.build("market_page", page=page, sort=sort,
                                                                     rarity=rarity))
            now = datetime.now(timezone.utc)
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
            statuses: dict[str, int] = {}
            for a in auctions:
                statuses[a.status] = statuses.get(a.status, 0) + 1
            span = f"fins de {left[0]:+.0f} s à {left[-1]:+.0f} s" if left else "vide"
            print(f"page {page} : {len(auctions)} enchères, {sum(x <= 0 for x in left)} terminées, "
                  f"{sum(0 < x <= 60 for x in left)} finissent dans la minute, {span}, statuts {statuses}, "
                  f"hasMore={more}")
            for a in auctions[:3] + auctions[-2:]:
                print(f"    {a.seconds_left(now):+8.0f} s  {a.status:<8} {a.rarity or '?':<3} {a.title[:50]}")
            if not more:
                break


async def check_session(settings: Settings) -> None:
    async with build_context(settings) as ctx:
        balance = await asyncio.wait_for(ctx.api.balance(), timeout=120)
        stats = await ctx.api.collection_stats()
        print(f"Session OK — joueur {ctx.client.user_id or '?'} — solde {balance} WB — {stats.total} exemplaires")
        print(f"Session : {ctx.client.session_status()}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wikimastered")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="lance le bot")
    sub.add_parser("check", help="vérifie la session")
    sub.add_parser("report", help="envoie le bilan du jour")
    ins = sub.add_parser("inspect", help="capture les vraies réponses JSON (lecture seule)")
    ins.add_argument("--out", default=None)
    sub.add_parser("collection", help="collection à compléter : possédées / manquantes (lecture seule)")
    mk = sub.add_parser("market", help="où sont les fins imminentes dans le marché (lecture seule)")
    mk.add_argument("--pages", type=int, default=4)
    mk.add_argument("--sort", default="ending_soon")
    mk.add_argument("--rarity", default="", help="C, PC, R, SR, UR, L…")
    pk = sub.add_parser("packs", help="paquets ouverts récemment (lecture seule)")
    pk.add_argument("--hours", type=int, default=24)
    tr = sub.add_parser("trends", help="mesure des signaux de tendance (lecture seule)")
    tr.add_argument("--days", type=int, default=7)
    sim = sub.add_parser("reset-simulation", help="efface les mises et ventes simulées (sans --apply : compte)")
    sim.add_argument("--apply", action="store_true", help="efface vraiment")
    sub.add_parser("gifs", help="les cartes GIF se vendent-elles mieux ? (ventes observées, lecture seule)")
    snr = sub.add_parser("sniper-report", help="toutes les enchères misées sur les N dernières heures (lecture seule)")
    snr.add_argument("--hours", type=float, default=24)
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
    elif command == "check":
        asyncio.run(check_session(settings))
    elif command == "report":
        async def _report() -> None:
            async with build_context(settings) as ctx:
                print(await daily_report(ctx))
        asyncio.run(_report())
    elif command == "inspect":
        from src.tools.inspect_api import inspect
        async def _inspect() -> None:
            async with build_context(settings) as ctx:
                await inspect(ctx, args.out)
        asyncio.run(_inspect())
    elif command == "collection":
        asyncio.run(collection_status(settings))
    elif command == "market":
        asyncio.run(market_status(settings, max(1, min(args.pages, 10)), args.sort, args.rarity.upper()))
    elif command == "packs":
        asyncio.run(packs_status(settings, args.hours))
    elif command == "trends":
        async def _trends() -> None:
            async with build_context(settings) as ctx:
                print(await trends_text(ctx.db, args.days, await min_profit(ctx), datetime.now(timezone.utc)))
        asyncio.run(_trends())
    elif command == "reset-simulation":
        asyncio.run(reset_simulation(settings, args.apply))
    elif command == "gifs":
        async def _gifs() -> None:
            async with build_context(settings) as ctx:
                print(gif_text(await ctx.db.gif_stats()))
        asyncio.run(_gifs())
    elif command == "sniper-report":
        async def _sniper_report() -> None:
            async with build_context(settings) as ctx:
                print(await send_sniper_report(ctx, args.hours, send=False))
        asyncio.run(_sniper_report())
    elif command == "inventory":
        async def _inventory() -> None:
            async with build_context(settings) as ctx:
                await inventory_cli(ctx, args.action, args.apply, args.lookups)
        asyncio.run(_inventory())


if __name__ == "__main__":
    main()
