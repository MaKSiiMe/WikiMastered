"""Sauvegarde quotidienne de la base SQLite (BACKUP_TIME, 04:30) et à la demande (/sauvegarde sur Discord).

Toute la mémoire du bot tient dans data/wikimastered.db (≈ 180 Mo le 27/10) : ventes observées, cotes, positions,
réglages changés sur Discord. Perdue, le sniper n'estime plus rien pendant des jours.

- Copie à chaud par l'API de sauvegarde de SQLite, dans un fil à part et par petits paquets de pages : les écritures
  du bot continuent, la copie est cohérente.
- La copie est vérifiée (PRAGMA quick_check) avant d'être gardée : une sauvegarde abîmée ne remplace rien.
- Compressée (gzip, ≈ ÷ 4) dans BACKUP_DIR (un volume à part : le volume des données peut être perdu sans elle),
  nommée wikimastered-AAAA-MM-JJ-HHMM.db.gz ; les BACKUP_KEEP plus récentes sont gardées.
- Restauration : docs/INSTALLATION.md, « Sauvegardes ».
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.timeutils import next_local, now_local, sleep_until, utcnow
from src.workers.base import Context

log = logging.getLogger(__name__)

KV_KEY = "last_backup"
PAGES_PER_STEP = 2000  # pages copiées d'un coup (≈ 8 Mo) avant de laisser la main aux écritures du bot
PAUSE_SECONDS = 0.05
FREE_SPACE_FACTOR = 1.5  # place libre exigée : copie non compressée + archive, soit ≈ 1,3 × la base


@dataclass(frozen=True)
class Backup:
    path: Path
    size: int  # octets de l'archive
    db_size: int  # octets de la base
    seconds: float

    def info(self) -> dict:
        return {"at": utcnow().isoformat(), "path": self.path.name, "size": self.size, "db_size": self.db_size,
                "seconds": round(self.seconds, 1)}


def _copy(source: Path, target: Path) -> None:
    """Copie cohérente d'une base en cours d'écriture, vérifiée."""
    src = sqlite3.connect(source, timeout=30)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst, pages=PAGES_PER_STEP, sleep=PAUSE_SECONDS)
            result = dst.execute("PRAGMA quick_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"copie abîmée (quick_check : {result[0] if result else 'sans réponse'})")


def _compress(source: Path, target: Path) -> None:
    with source.open("rb") as raw, gzip.open(target, "wb", compresslevel=6) as out:
        shutil.copyfileobj(raw, out, length=1 << 20)


def prune(directory: Path, keep: int) -> list[Path]:
    """Supprime les sauvegardes au-delà des `keep` plus récentes ; renvoie celles supprimées."""
    found = sorted(directory.glob("wikimastered-*.db.gz"), reverse=True)  # le nom commence par la date
    for old in found[keep:]:
        old.unlink(missing_ok=True)
    return found[keep:]


def backup_sync(source: Path, directory: Path, keep: int) -> Backup:
    """Fait une sauvegarde (bloquant : à lancer dans un fil)."""
    started = utcnow()
    directory.mkdir(parents=True, exist_ok=True)
    db_size = source.stat().st_size
    free = shutil.disk_usage(directory).free
    if free < db_size * FREE_SPACE_FACTOR:
        raise RuntimeError(f"pas assez de place dans {directory} : {free >> 20} Mo libres, il en faut "
                           f"≈ {int(db_size * FREE_SPACE_FACTOR) >> 20}")
    stamp = now_local().strftime("%Y-%m-%d-%H%M")
    tmp = directory / f"wikimastered-{stamp}.db.tmp"
    target = directory / f"wikimastered-{stamp}.db.gz"
    partial = directory / f"{target.name}.part"
    try:
        _copy(source, tmp)
        _compress(tmp, partial)
        partial.replace(target)  # l'archive n'existe sous son vrai nom qu'une fois complète
    finally:
        tmp.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    prune(directory, keep)
    return Backup(target, target.stat().st_size, db_size, (utcnow() - started).total_seconds())


async def run_backup(ctx: Context) -> Backup:
    """Sauvegarde maintenant ; garde le résultat en base et dans le contexte (affiché par /etat)."""
    s = ctx.settings
    result = await asyncio.to_thread(backup_sync, ctx.db.path, s.backup_dir, s.backup_keep)
    ctx.last_backup = result.info()
    await ctx.db.kv_set(KV_KEY, ctx.last_backup)
    log.info("sauvegarde %s : %d Mo (base %d Mo) en %.0f s", result.path.name, result.size >> 20,
             result.db_size >> 20, result.seconds)
    return result


def describe(info: dict | None) -> str:
    """Ligne de /etat : dernière sauvegarde."""
    if not info:
        return "aucune sauvegarde faite depuis le démarrage"
    when = datetime.fromisoformat(info["at"]).astimezone(now_local().tzinfo)
    return f"{when:%d/%m %H:%M} · {info['size'] >> 20} Mo (base {info['db_size'] >> 20} Mo)"


async def backup_loop(ctx: Context) -> None:
    """Une sauvegarde par jour à BACKUP_TIME ; un échec prévient une fois par jour, jamais le bot."""
    ctx.last_backup = await ctx.db.kv_get(KV_KEY, {})
    while True:
        await sleep_until(next_local(ctx.settings.backup_time))
        try:
            result = await run_backup(ctx)
            ctx.notifier.info("Sauvegarde faite", f"{result.path.name} : {result.size >> 20} Mo en "
                              f"{result.seconds:.0f} s, {ctx.settings.backup_keep} gardées.", important=False)
        except Exception as exc:  # noqa: BLE001 - une sauvegarde ratée ne doit pas arrêter le bot
            log.exception("Sauvegarde impossible")
            ctx.notifier.warning("Sauvegarde impossible", f"{type(exc).__name__} : {exc}"[:500], important=True)
