"""Sauvegarde quotidienne de la base : copie cohérente, vérifiée, compressée, rotation."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import httpx
import pytest

from src import backup
from tests.test_integration import make_client, make_context


def make_db(path: Path, rows: int = 2000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, title TEXT, price INTEGER)")
    conn.executemany("INSERT INTO sales (title, price) VALUES (?, ?)", [(f"Carte {i}", i) for i in range(rows)])
    conn.commit()
    conn.close()


def test_backup_is_a_verified_compressed_copy_and_old_ones_are_pruned(tmp_path):
    source, target = tmp_path / "wikimastered.db", tmp_path / "backups"
    make_db(source)
    live = sqlite3.connect(source)  # le bot garde sa connexion ouverte et écrit pendant la sauvegarde
    live.execute("INSERT INTO sales (title, price) VALUES ('récent', 1)")
    live.commit()
    for day in range(1, 9):
        target.mkdir(exist_ok=True)
        (target / f"wikimastered-2026-10-0{day}-0430.db.gz").write_bytes(b"ancienne")
    result = backup.backup_sync(source, target, keep=3)
    live.close()
    assert result.path.name.startswith("wikimastered-") and result.path.suffix == ".gz"
    assert sorted(p.name for p in target.glob("*.gz"))[-1] == result.path.name
    assert len(list(target.glob("wikimastered-*.db.gz"))) == 3  # les 3 plus récentes
    assert not list(target.glob("*.tmp")) and not list(target.glob("*.part"))
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(result.path.read_bytes()))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2001  # écriture en cours comprise
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert result.size < result.db_size  # compressée


def test_backup_refuses_when_there_is_not_enough_room(tmp_path, monkeypatch):
    source = tmp_path / "wikimastered.db"
    make_db(source)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": 10})())
    with pytest.raises(RuntimeError, match="pas assez de place"):
        backup.backup_sync(source, tmp_path / "backups", keep=7)
    assert not list((tmp_path / "backups").glob("*"))  # rien de commencé


@pytest.mark.asyncio
async def test_backup_now_records_the_result_for_state(tmp_path):
    client, notifier = make_client(tmp_path, lambda r: httpx.Response(404), backup_dir=tmp_path / "sauvegardes",
                                   backup_keep=2)
    ctx = await make_context(tmp_path, client, notifier)
    assert "aucune sauvegarde" in backup.describe(ctx.last_backup)
    result = await backup.run_backup(ctx)
    assert (tmp_path / "sauvegardes" / result.path.name).exists()
    assert ctx.last_backup["path"] == result.path.name
    assert (await ctx.db.kv_get(backup.KV_KEY))["path"] == result.path.name
    assert "Mo" in backup.describe(ctx.last_backup)
    await ctx.db.close()
