"""Récap périodique (RECAP_INTERVAL_HOURS) : ce qui cloche, puis les achats et les ventes de la période.

Il sert à repérer vite un problème (worker arrêté, erreurs, plafond dépassé, sniper qui ne trouve rien) sans attendre
le bilan du soir. Sur Discord : seulement ce qui s'est passé. Le détail de chaque worker (repérage, issues des
enchères, requêtes au site) est écrit dans les logs, lignes « détail · … ».
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from src.core import timeutils
from src.core.notifier import Level, Topic
from src.core.timeutils import now_local, sleep_until, utcnow
from src.roi import plural, wb
from src.workers.base import Context, Worker
from src.workers.collection import progress_lines, targets

log = logging.getLogger(__name__)

# Un worker qui n'a pas repris son passage prévu depuis ce délai est signalé.
STALE_AFTER = timedelta(minutes=15)

# Blocs du récap, dans l'ordre : (titre, workers concernés)
BLOCKS: list[tuple[str, tuple[str, ...]]] = [
    (Topic.SNIPER.value, ("sniper",)),
    (Topic.SALES.value, ("inventory", "cotes", "valeur")),
    (Topic.COLLECTION.value, ("collection", "souhaits")),
    (Topic.ACHIEVEMENTS.value, ("succes",)),
    (Topic.TRADES.value, ("echanges",)),
    ("🛡️ Garde", ("garde",)),
]


# Workers dont les lignes d'activité vont sur Discord quand ils ont fait quelque chose (succès réclamés).
SHOWN = {"succes"}


def health(worker: Worker, now: datetime, session_ok: bool) -> tuple[str, str | None]:
    """(ligne d'état, anomalie éventuelle) ; remet à zéro les erreurs comptées depuis le dernier récap."""
    errors, worker.recent_errors = worker.recent_errors, 0
    if paused := getattr(worker, "paused", None):
        return f"⏸️ {paused}", None  # voulue (/pause) : pas une anomalie
    blocked = getattr(worker, "blocked", None)
    if blocked:
        return f"⏸️ {blocked}", f"{worker.name} : {blocked}"
    if worker.last_ok is None and worker.next_due is None:
        return "pas encore passé", None
    late = session_ok and worker.next_due is not None and now - worker.next_due > STALE_AFTER
    if late:
        since = worker.next_due.astimezone(timeutils.PARIS) if worker.next_due else None
        return "❌ arrêté", f"{worker.name} : aucun passage depuis {since:%d/%m %H:%M}" if since else None
    last = f"dernier passage {worker.last_ok.astimezone(timeutils.PARIS):%H:%M}" if worker.last_ok else "aucun passage réussi"
    if errors:
        return f"⚠️ {errors} erreur(s), {last}", f"{worker.name} : {errors} erreur(s), dernière : {worker.last_error}"
    return f"✅ {last}", None


def activity(name: str, stats: dict[str, int]) -> list[str]:
    """Ce que montre la base pour ce worker depuis le dernier récap : le résultat d'abord, sans les compteurs à
    zéro. Rien à dire : liste vide, le bloc n'est pas affiché."""
    if name == "sniper":
        rejected = stats.get("bids_rejected", 0)
        bought = f"**{plural(stats['won'], 'achat')}**" + (f" ({wb(stats.get('won_wb', 0))} WB)" if stats["won"] else "")
        parts = [bought, plural(stats["bids_real"] - rejected, "mise")]
        if rejected:
            parts.append(plural(rejected, "refusée", "refusées") + " par le site")
        if stats["bids_sim"]:
            parts.append(plural(stats["bids_sim"], "mise simulée", "mises simulées"))
        held = f"{plural(stats['held'], 'position')} en stock"
        if stats["held_real"] != stats["held"]:
            held += f" (dont {plural(stats['held_real'], 'réelle')})"
        return [" · ".join(parts), held]
    if name == "inventory":
        lines = []
        if stats["posted"] or stats["sold"] or stats["unsold"]:
            sold = f"**{plural(stats['sold'], 'vendue')}**"
            if stats["sold"]:
                sold += f" ({wb(stats['sold_wb'])} WB, bénéfice {wb(stats.get('sold_profit', 0), True)})"
            lines.append(f"{sold} · {plural(stats['posted'], 'mise en vente', 'mises en vente')} · "
                         f"{stats['unsold']} sans acheteur")
        if stats["sim_sold"] or stats["sim_unsold"]:
            # « Sans acheteur » : une vente terminée sans mise, remise en vente ou carte gardée (le bilan ne compte
            # que les cartes gardées, d'où « 8 invendue(s) » au récap et « 0 gardées invendues » au bilan, le 16/10).
            lines.append(f"Reventes simulées : {stats['sim_sold']} vendue(s), {stats['sim_unsold']} vente(s) sans "
                         "acheteur (remise en vente ou carte gardée)")
        return lines
    if name == "collection":
        if not (stats["won_collection"] or stats["bids_collection"]):
            return []
        return [f"**{plural(stats['won_collection'], 'carte gagnée', 'cartes gagnées')}** sur "
                f"{plural(stats['bids_collection'], 'mise')}", *stats["collection_progress"][:1]]
    return []


# Récap trop proche du démarrage pour conclure à un marché qui n'est plus lu : le 12/11, bot redémarré à 19:59,
# « aucune vente observée » au récap de 20:00.
OBSERVED_MIN_WINDOW = timedelta(minutes=30)


def anomalies(ctx: Context, stats: dict[str, int], pending: dict[str, int],
              window: timedelta = OBSERVED_MIN_WINDOW) -> list[str]:
    """Incohérences de la base, à corriger. `window` : durée couverte par le récap."""
    found = []
    s = ctx.settings
    if s.enable_sniper:
        # Comme le plafond du sniper : en réel, seules les positions réelles comptent. Les positions simulées
        # d'avant le passage en réel déclenchaient « 11 positions ouvertes, plafond 5 » au récap de 16 h le 16/10.
        live = s.is_live("place_bid")
        open_positions = (stats["held_real"] + pending["real_positions"]) if live else \
            (stats["held"] + pending["positions"])
        if 0 < s.portfolio_max_positions < open_positions:
            found.append(f"sniper : {open_positions} positions {'réelles ' if live else ''}ouvertes, "
                         f"plafond {s.portfolio_max_positions}")
        if pending["positions"] > 20:
            found.append(f"sniper : {pending['positions']} mises en attente de relecture")
        if not stats["observed"] and window >= OBSERVED_MIN_WINDOW:
            found.append("sniper : aucune vente observée, le marché n'est peut-être plus lu")
    return found


def traffic_line(traffic: Counter[str], limited: int, seconds: float) -> str:
    """Requêtes au site depuis le dernier récap, par worker (les plus gourmands d'abord), et limitations du site."""
    reads = sum(n for lane, n in traffic.items() if lane != "écritures")
    parts = [f"{lane} {n}" for lane, n in traffic.most_common() if lane != "écritures"]
    line = f"Lectures : {reads} ({reads / max(seconds, 1):.2f}/s)" + (" · " + " · ".join(parts) if parts else "")
    return line + f"\nÉcritures : {traffic['écritures']} · limitées par le site : {limited}"


async def build_recap(ctx: Context, workers: list[Worker], since: datetime,
                      started_at: datetime) -> tuple[bool, str, list[tuple[str, str]]]:
    """(anomalie trouvée, description, blocs)."""
    now = utcnow()
    stats = await ctx.db.recap_stats(since)
    collection = await ctx.db.kv_get("collection")
    stats["collection_progress"] = progress_lines(collection, targets(ctx) if collection else {}, now)
    pending = await ctx.db.pending_bids()
    by_name = {w.name: w for w in workers}
    problems = anomalies(ctx, stats, pending, now - since)
    if getattr(ctx.client, "halted", None):
        problems.append(f"bot arrêté (sanction) : {ctx.client.halted} ; `rm data/HALT` pour reprendre")
    if not ctx.client.session_ok:
        problems.append("session en pause : les workers attendent un nouveau cookie")
    sections, running, paused = [], 0, []
    for title, names in BLOCKS:
        present = [by_name[n] for n in names if n in by_name]
        if not present:
            continue
        lines = activity(names[0], stats)
        for worker in present:
            try:
                detail = worker.recap()
            except Exception:  # noqa: BLE001 - pas encore initialisé, ou bogue : le récap des autres part quand même
                log.exception("récap de %s impossible", worker.name)
                detail = []
            # Le détail (repérage, issues des enchères, pity…) reste dans les logs : il sert aux revues, pas à Discord.
            for line in detail:
                log.info("détail · %s : %s", worker.name, line)
            lines += [line for line in detail if worker.name in SHOWN and not line.startswith("Aucun")]
            state, problem = health(worker, now, ctx.client.session_ok)
            if problem:
                problems.append(problem)
            if state.startswith("✅"):
                running += 1
            elif getattr(worker, "paused", None):
                paused.append(worker.name)
            else:
                lines.append(state if len(present) == 1 else f"{worker.name} : {state}")
        if lines:
            sections.append((title, "\n".join(lines)))
    if take_traffic := getattr(ctx.client, "take_traffic", None):
        log.info("détail · réseau : %s", traffic_line(*take_traffic(), (now - since).total_seconds())
                 .replace("\n", " · "))
    if problems:
        sections.insert(0, ("À vérifier", "\n".join(f"• {p}" for p in problems)))
    description = (f"Depuis {since.astimezone(timeutils.PARIS):%H:%M} · ✅ {plural(running, 'worker actif', 'workers actifs')}"
                   + (f" · ⏸️ en pause : {', '.join(paused)}" if paused else ""))
    return bool(problems), description, sections


async def recap_loop(ctx: Context, workers: list[Worker], started_at: datetime) -> None:
    """Un récap toutes les RECAP_INTERVAL_HOURS heures, aux heures rondes (0 h, 4 h, 8 h… pour 4)."""
    hours = ctx.settings.recap_interval_hours
    since = utcnow()
    while True:
        await sleep_until(next_recap(now_local(), hours))
        try:
            problem, description, sections = await build_recap(ctx, workers, since, started_at)
            level = Level.WARNING if problem else Level.INFO
            ctx.notifier.send(level, f"Récap des {hours} dernières heures", description, topic=Topic.RECAP,
                              sections=sections)
        except Exception:  # noqa: BLE001 - le bot continue sans récap
            log.exception("Récap impossible")
        since = utcnow()


def next_recap(now: datetime, hours: int) -> datetime:
    """Prochaine heure ronde multiple de `hours` depuis minuit (heure de Paris)."""
    base = now.replace(minute=0, second=0, microsecond=0)
    step = base + timedelta(hours=hours - base.hour % hours)
    return step if step > now else step + timedelta(hours=hours)
