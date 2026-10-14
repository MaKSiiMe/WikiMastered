"""Point d'entrée : orchestre les workers et expose quelques commandes CLI.

    python -m src.main run                      # le bot (commande par défaut du conteneur)
    python -m src.main check                    # vérifie la session et affiche le solde
    python -m src.main inspect                  # capture et compare les vraies réponses JSON
    python -m src.main inventory scan|list|discard [--apply]
    python -m src.main report                   # envoie le bilan du jour sur Discord
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import time
from logging.handlers import RotatingFileHandler

from src.config import Settings, get_settings
from src.core.api import WikiMastersAPI
from src.core.client import WikiMastersClient
from src.core.database import Database
from src.core.endpoints import Endpoints
from src.core.notifier import DiscordNotifier
from src.core.pricing import PriceService
from src.core.timeutils import local_dt, next_local, now_local, set_timezone, sleep_until
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context, Worker
from src.workers.inventory_manager import InventoryManager
from src.workers.inventory_manager import cli as inventory_cli
from src.workers.pack_opener import PackOpener
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
                               settings.discord_mention, settings.dry_run)
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
    rarities = " ".join(f"{r}:{n}" for r, n in sorted(stats.pop("pulls_by_rarity").items())) or "—"
    text = f"Tirages par rareté : {rarities}"
    if ctx.settings.dry_run:
        text += "\n\n" + validation_text(await ctx.db.validation_stats(since, ctx.settings.price_min_sales))
    if send:
        ctx.notifier.info(f"Bilan du {now_local():%d/%m}", text, **stats)
    return stats


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
    while True:
        await sleep_until(next_local(ctx.settings.daily_report_time))
        await daily_report(ctx)


async def heartbeat(ctx: Context) -> None:
    path = ctx.settings.data_dir / "heartbeat"
    while True:
        path.write_text(now_local().isoformat(), encoding="utf-8")
        ctx.client.check_cookie_drop()
        await asyncio.sleep(30)


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

        mode = "DRY RUN (aucune écriture)" if settings.dry_run else "RÉEL"
        names = ", ".join(w.name for w in workers) or "aucun"
        log.info("Démarrage en mode %s — workers : %s — joueur %s", mode, names, ctx.client.user_id or "?")
        ctx.notifier.info("Bot démarré", f"Mode {mode}\nWorkers : {names}")

        tasks = [asyncio.create_task(w.run(), name=w.name) for w in workers]
        tasks += [asyncio.create_task(report_loop(ctx), name="report"),
                  asyncio.create_task(heartbeat(ctx), name="heartbeat")]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper and task.exception():
                log.error("La tâche %s s'est arrêtée", task.get_name(), exc_info=task.exception())
        log.info("Arrêt en cours…")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ctx.notifier.warning("Bot arrêté")


async def check_session(settings: Settings) -> None:
    async with build_context(settings) as ctx:
        balance = await asyncio.wait_for(ctx.api.balance(), timeout=120)
        stats = await ctx.api.collection_stats()
        print(f"Session OK — joueur {ctx.client.user_id or '?'} — solde {balance} WB — {stats.total} exemplaires")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wikimastered")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="lance le bot")
    sub.add_parser("check", help="vérifie la session")
    sub.add_parser("report", help="envoie le bilan du jour")
    ins = sub.add_parser("inspect", help="capture les vraies réponses JSON (lecture seule)")
    ins.add_argument("--out", default=None)
    inv = sub.add_parser("inventory", help="analyse / vente / défausse")
    inv.add_argument("action", choices=["scan", "list", "discard"])
    inv.add_argument("--apply", action="store_true", help="exécute (simulé si DRY_RUN=true)")
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
    elif command == "inventory":
        async def _inventory() -> None:
            async with build_context(settings) as ctx:
                await inventory_cli(ctx, args.action, args.apply)
        asyncio.run(_inventory())


if __name__ == "__main__":
    main()
