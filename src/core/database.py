"""Stockage SQLite local : cache et historique des cotes, tirages, enchères, ventes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_cache (
    card_id TEXT NOT NULL, rarity TEXT NOT NULL, average REAL, sales_count INTEGER,
    fetched_at TEXT NOT NULL, PRIMARY KEY (card_id, rarity)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, rarity TEXT NOT NULL,
    average REAL, sales_count INTEGER, observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_card ON price_history (card_id, rarity, observed_at);

CREATE TABLE IF NOT EXISTS pulls (
    id INTEGER PRIMARY KEY, opened_at TEXT NOT NULL, card_id TEXT, owned_card_id TEXT,
    title TEXT, rarity TEXT, is_shiny INTEGER NOT NULL DEFAULT 0, pageviews INTEGER
);

CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY, auction_id TEXT NOT NULL, card_id TEXT, title TEXT, rarity TEXT,
    is_shiny INTEGER NOT NULL DEFAULT 0, amount INTEGER NOT NULL, reference_price REAL,
    reason TEXT, night INTEGER NOT NULL DEFAULT 0, dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, end_at TEXT, placed_at TEXT NOT NULL, settled_at TEXT,
    relisted INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids (auction_id);
CREATE INDEX IF NOT EXISTS idx_bids_status ON bids (status, end_at);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY, owned_card_id TEXT NOT NULL, card_id TEXT, title TEXT, rarity TEXT,
    base_amount INTEGER NOT NULL, duration_minutes INTEGER NOT NULL, post_at TEXT NOT NULL,
    target_end TEXT, reason TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
    posted_at TEXT, error TEXT, dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings (status, post_at);

CREATE TABLE IF NOT EXISTS discards (
    id INTEGER PRIMARY KEY, owned_card_id TEXT NOT NULL, card_id TEXT, title TEXT, rarity TEXT,
    discarded_at TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS auctions_seen (
    auction_id TEXT PRIMARY KEY, card_id TEXT, title TEXT, norm_title TEXT, rarity TEXT,
    is_shiny INTEGER NOT NULL DEFAULT 0, current_bid INTEGER, base_amount INTEGER,
    min_bid INTEGER, end_at TEXT, seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auctions_title ON auctions_seen (norm_title);

CREATE TABLE IF NOT EXISTS observed_sales (
    auction_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, title TEXT, rarity TEXT,
    is_shiny INTEGER NOT NULL DEFAULT 0, price INTEGER NOT NULL, ended_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observed_card ON observed_sales (card_id, ended_at);

CREATE TABLE IF NOT EXISTS trend_alerts (
    auction_id TEXT PRIMARY KEY, article TEXT, alerted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pageviews (
    article TEXT NOT NULL, day TEXT NOT NULL, views INTEGER NOT NULL, PRIMARY KEY (article, day)
);
"""


# Colonnes ajoutées après la première version : migrées au démarrage sur une base existante.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("bids", "closed", "INTEGER NOT NULL DEFAULT 0"),
    ("bids", "sale_price", "INTEGER"),
    ("bids", "sold_at", "TEXT"),
    ("listings", "bid_id", "INTEGER"),
    ("listings", "auction_id", "TEXT"),
    ("listings", "sale_price", "INTEGER"),
    ("listings", "settled_at", "TEXT"),
    ("listings", "relist_count", "INTEGER NOT NULL DEFAULT 0"),
]

HELD = "status IN ('won', 'sim_won') AND closed = 0"
# Une vente « incertaine » a peut-être été créée : elle reste ouverte jusqu'à sa vérification.
LISTING_OPEN = "('scheduled', 'posted', 'simulated', 'uncertain')"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            if column not in existing:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("base non connectée")
        return self._db

    async def _exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = await self.db.execute(sql, tuple(params))
        await self.db.commit()
        return cur.lastrowid or 0

    async def _all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    # ------------------------------------------------------------------- kv
    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), iso(utcnow())),
        )

    # ---------------------------------------------------------------- cotes
    async def price_get(self, card_id: str) -> tuple[datetime, dict[str, tuple[float | None, int | None]]] | None:
        rows = await self._all(
            "SELECT rarity, average, sales_count, fetched_at FROM price_cache WHERE card_id = ?", (card_id,)
        )
        if not rows:
            return None
        fetched = min(parse_iso(r["fetched_at"]) for r in rows)  # type: ignore[type-var]
        return fetched, {r["rarity"]: (r["average"], r["sales_count"]) for r in rows}

    async def price_put(self, card_id: str, summary: dict[str, tuple[float | None, int | None]]) -> None:
        now = iso(utcnow())
        # Ligne sentinelle "_" : une carte sans aucune vente reste en cache.
        rows = summary or {"_": (None, None)}
        await self.db.execute("DELETE FROM price_cache WHERE card_id = ?", (card_id,))
        await self.db.executemany(
            "INSERT INTO price_cache (card_id, rarity, average, sales_count, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(card_id, r, avg, cnt, now) for r, (avg, cnt) in rows.items()],
        )
        await self.db.executemany(
            "INSERT INTO price_history (card_id, rarity, average, sales_count, observed_at) VALUES (?, ?, ?, ?, ?)",
            [(card_id, r, avg, cnt, now) for r, (avg, cnt) in summary.items() if avg is not None],
        )
        await self.db.commit()

    # ------------------------------------------------------------- tirages
    async def record_pull(self, *, card_id: str, owned_card_id: str | None, title: str,
                          rarity: str | None, is_shiny: bool, pageviews: int | None) -> None:
        await self._exec(
            "INSERT INTO pulls (opened_at, card_id, owned_card_id, title, rarity, is_shiny, pageviews) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso(utcnow()), card_id, owned_card_id, title, rarity, int(is_shiny), pageviews),
        )

    # ------------------------------------------------------------- enchères
    async def record_bid(self, *, auction_id: str, card_id: str, title: str, rarity: str | None,
                         is_shiny: bool, amount: int, reference_price: float | None, reason: str,
                         night: bool, dry_run: bool, status: str, end_at: datetime, note: str = "") -> int:
        return await self._exec(
            "INSERT INTO bids (auction_id, card_id, title, rarity, is_shiny, amount, reference_price, reason, "
            "night, dry_run, status, end_at, placed_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (auction_id, card_id, title, rarity, int(is_shiny), amount, reference_price, reason,
             int(night), int(dry_run), status, iso(end_at), iso(utcnow()), note),
        )

    async def update_bid(self, bid_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        await self._exec(f"UPDATE bids SET {cols} WHERE id = ?", (*values, bid_id))

    async def get_bid(self, bid_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM bids WHERE id = ?", (bid_id,))

    async def placed_bids(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT id, auction_id, amount FROM bids WHERE status = 'placed'")

    async def uncertain_bids(self, before: datetime, limit: int = 2) -> list[aiosqlite.Row]:
        """Mises réelles au résultat incertain, à relire sur le serveur (jamais rejouées)."""
        return await self._all(
            "SELECT * FROM bids b WHERE status = 'uncertain' AND dry_run = 0 AND placed_at < ? AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id) ORDER BY id LIMIT ?",
            (iso(before), limit),
        )

    async def mark_outbid(self, auction_id: str) -> None:
        await self._exec("UPDATE bids SET status = 'outbid' WHERE auction_id = ? AND status = 'placed'", (auction_id,))

    async def last_bid(self, auction_id: str) -> aiosqlite.Row | None:
        return await self._one(
            "SELECT * FROM bids WHERE auction_id = ? ORDER BY id DESC LIMIT 1", (auction_id,)
        )

    async def bids_to_settle(self, before: datetime, limit: int = 3) -> list[aiosqlite.Row]:
        """Dernière mise confirmée de chaque enchère échue, à vérifier sur le serveur."""
        return await self._all(
            "SELECT * FROM bids b WHERE status IN ('placed', 'simulated') AND end_at < ? AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id) ORDER BY end_at LIMIT ?",
            (iso(before), limit),
        )

    async def committed_since(self, since: datetime, dry_run: bool = False) -> int:
        """WikiBidous engagés : enchères en tête non réglées + enchères gagnées depuis `since`.

        En DRY_RUN, ce sont les mises simulées (en cours ou remportées) qui comptent."""
        row = await self._one(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM bids b WHERE dry_run = ? AND placed_at >= ? "
            "AND status IN ('placed', 'won', 'uncertain', 'simulated', 'sim_won') AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)",
            (int(dry_run), iso(since)),
        )
        return int(row["s"]) if row else 0

    # ------------------------------------------------------------ positions
    async def positions_summary(self) -> dict[str, int]:
        """Positions détenues (achetées, pas encore revendues) : nombre et coût, réelles / simulées."""
        rows = await self._all(f"SELECT dry_run, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS cost "
                               f"FROM bids WHERE {HELD} GROUP BY dry_run")
        out = {"count": 0, "real_cost": 0, "sim_cost": 0}
        for r in rows:
            out["count"] += r["n"]
            out["sim_cost" if r["dry_run"] else "real_cost"] += r["cost"]
        return out

    async def positions_to_list(self) -> list[aiosqlite.Row]:
        return await self._all(
            f"SELECT * FROM bids b WHERE {HELD} AND NOT EXISTS (SELECT 1 FROM listings l "
            f"WHERE l.bid_id = b.id AND l.status IN {LISTING_OPEN}) ORDER BY settled_at"
        )

    async def close_position(self, bid_id: int, sale_price: int | None) -> None:
        await self.update_bid(bid_id, closed=1, sale_price=sale_price, sold_at=utcnow())

    async def simulated_engaged_cost(self, now: datetime) -> int:
        row = await self._one("SELECT COALESCE(SUM(amount), 0) AS s FROM bids WHERE status = 'simulated' "
                              "AND end_at > ?", (iso(now),))
        return int(row["s"]) if row else 0

    # --------------------------------------------------------------- ventes
    async def schedule_listing(self, *, owned_card_id: str, card_id: str, title: str, rarity: str | None,
                               base_amount: int, duration_minutes: int, post_at: datetime,
                               target_end: datetime, reason: str, bid_id: int | None = None,
                               relist_count: int = 0) -> int | None:
        existing = await self._one(
            f"SELECT id FROM listings WHERE owned_card_id = ? AND status IN {LISTING_OPEN}", (owned_card_id,),
        )
        if existing:
            return None
        return await self._exec(
            "INSERT INTO listings (owned_card_id, card_id, title, rarity, base_amount, duration_minutes, "
            "post_at, target_end, reason, status, created_at, bid_id, relist_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
            (owned_card_id, card_id, title, rarity, base_amount, duration_minutes, iso(post_at),
             iso(target_end), reason, iso(utcnow()), bid_id, relist_count),
        )

    async def due_listings(self, now: datetime) -> list[aiosqlite.Row]:
        # Les positions (cartes achetées pour revente) passent avant les doublons.
        return await self._all(
            "SELECT * FROM listings WHERE status = 'scheduled' AND post_at <= ? "
            "ORDER BY bid_id IS NULL, post_at", (iso(now),)
        )

    async def listings_to_settle(self, before: datetime) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM listings WHERE status IN ('posted', 'simulated') AND target_end < ? ORDER BY target_end",
            (iso(before),),
        )

    async def uncertain_listings(self, before: datetime) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM listings WHERE status = 'uncertain' AND COALESCE(posted_at, created_at) < ? ORDER BY id",
            (iso(before),),
        )

    async def failed_listings_count(self, bid_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE bid_id = ? AND status = 'failed'", (bid_id,))
        return int(row["n"]) if row else 0

    async def listed_auction_ids(self) -> set[str]:
        rows = await self._all("SELECT auction_id FROM listings WHERE auction_id IS NOT NULL")
        return {r["auction_id"] for r in rows}

    async def posted_listings_count(self) -> int:
        """Ventes réelles du bot en cours sur le site."""
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE status = 'posted'")
        return int(row["n"]) if row else 0

    async def simulated_active_listings(self, now: datetime) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE status = 'simulated' AND target_end > ?",
                              (iso(now),))
        return int(row["n"]) if row else 0

    async def update_listing(self, listing_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        await self._exec(f"UPDATE listings SET {cols} WHERE id = ?", (*values, listing_id))

    async def pending_listings_count(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE status = 'scheduled'")
        return int(row["n"]) if row else 0

    async def scheduled_owned_ids(self) -> set[str]:
        rows = await self._all("SELECT owned_card_id FROM listings WHERE status IN ('scheduled', 'posted', 'uncertain')")
        return {r["owned_card_id"] for r in rows}

    async def record_discards(self, rows: list[tuple[str, str, str, str | None]], dry_run: bool) -> None:
        now = iso(utcnow())
        await self.db.executemany(
            "INSERT INTO discards (owned_card_id, card_id, title, rarity, discarded_at, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(o, c, t, r, now, int(dry_run)) for o, c, t, r in rows],
        )
        await self.db.commit()

    # ------------------------------------------------------- marché observé
    async def upsert_auctions(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO auctions_seen (auction_id, card_id, title, norm_title, rarity, is_shiny, current_bid, "
            "base_amount, min_bid, end_at, seen_at) VALUES (:auction_id, :card_id, :title, :norm_title, :rarity, "
            ":is_shiny, :current_bid, :base_amount, :min_bid, :end_at, :seen_at) "
            "ON CONFLICT(auction_id) DO UPDATE SET current_bid = excluded.current_bid, "
            "min_bid = excluded.min_bid, end_at = excluded.end_at, seen_at = excluded.seen_at",
            rows,
        )
        await self.db.commit()

    async def auctions_by_title(self, norm_title: str, now: datetime) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM auctions_seen WHERE norm_title = ? AND end_at > ?", (norm_title, iso(now))
        )

    async def harvest_ended_auctions(self, before: datetime, max_gap_seconds: float = 120) -> int:
        """Transforme les enchères vues juste avant leur fin en ventes observées.

        Le dernier `current_bid` vu par le sniper (qui scrute les fins imminentes toutes
        les ~3 s) approche le prix final. Une enchère sans mise n'est pas une vente."""
        rows = await self._all("SELECT * FROM auctions_seen")
        done, sales = [], []
        for r in rows:
            end, seen = parse_iso(r["end_at"]), parse_iso(r["seen_at"])
            if end is None or end >= before:
                continue
            done.append((r["auction_id"],))
            if r["current_bid"] and seen and (end - seen).total_seconds() <= max_gap_seconds:
                sales.append((r["auction_id"], r["card_id"], r["title"], r["rarity"], r["is_shiny"],
                              r["current_bid"], iso(end)))
        await self.db.executemany(
            "INSERT OR IGNORE INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", sales)
        await self.db.executemany("DELETE FROM auctions_seen WHERE auction_id = ?", done)
        await self.db.commit()
        return len(sales)

    async def observed_prices(self, card_id: str, limit: int) -> tuple[list[int], int]:
        """(prix des `limit` dernières ventes observées, plus récentes d'abord ; nombre total)."""
        rows = await self._all("SELECT price FROM observed_sales WHERE card_id = ? ORDER BY ended_at DESC",
                               (card_id,))
        return [r["price"] for r in rows[:limit]], len(rows)

    async def observed_prices_since(self, card_id: str, since: datetime, limit: int) -> list[int]:
        rows = await self._all("SELECT price FROM observed_sales WHERE card_id = ? AND ended_at >= ? "
                               "ORDER BY ended_at DESC LIMIT ?", (card_id, iso(since), limit))
        return [r["price"] for r in rows]

    async def validation_stats(self, since: datetime, min_sales: int) -> dict[str, int]:
        """Chiffres pour juger la simulation : marché observé et résultat des positions depuis le début."""
        async def one(sql: str, params: Iterable[Any] = ()) -> int:
            row = await self._one(sql, params)
            return int(row[0] or 0) if row else 0

        positions = "status IN ('won', 'sim_won') AND reason NOT LIKE 'collection%'"
        return {
            "observed_today": await one("SELECT COUNT(*) FROM observed_sales WHERE ended_at >= ?", (iso(since),)),
            "observed_total": await one("SELECT COUNT(*) FROM observed_sales"),
            "cards_priced": await one("SELECT COUNT(*) FROM (SELECT card_id FROM observed_sales GROUP BY card_id "
                                      "HAVING COUNT(*) >= ?)", (min_sales,)),
            "sim_bought": await one(f"SELECT COUNT(*) FROM bids WHERE dry_run = 1 AND {positions}"),
            "sim_sold": await one(f"SELECT COUNT(*) FROM bids WHERE dry_run = 1 AND {positions} AND closed = 1 "
                                  "AND sale_price IS NOT NULL"),
            "sim_profit": await one(f"SELECT SUM(sale_price - amount) FROM bids WHERE dry_run = 1 AND {positions} "
                                    "AND closed = 1 AND sale_price IS NOT NULL"),
            "sim_kept": await one(f"SELECT COUNT(*) FROM bids WHERE dry_run = 1 AND {positions} AND closed = 1 "
                                  "AND sale_price IS NULL"),
            "sim_kept_cost": await one(f"SELECT SUM(amount) FROM bids WHERE dry_run = 1 AND {positions} "
                                       "AND closed = 1 AND sale_price IS NULL"),
            "sim_held": await one(f"SELECT COUNT(*) FROM bids WHERE dry_run = 1 AND {positions} AND closed = 0"),
            "uncertain": await one("SELECT (SELECT COUNT(*) FROM bids WHERE status = 'uncertain') + "
                                   "(SELECT COUNT(*) FROM listings WHERE status = 'uncertain')"),
        }

    async def prune_auctions(self, before: datetime) -> None:
        await self._exec("DELETE FROM auctions_seen WHERE end_at < ?", (iso(before),))

    async def trend_alerted(self, auction_id: str) -> bool:
        return await self._one("SELECT 1 FROM trend_alerts WHERE auction_id = ?", (auction_id,)) is not None

    async def mark_trend_alert(self, auction_id: str, article: str) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO trend_alerts (auction_id, article, alerted_at) VALUES (?, ?, ?)",
            (auction_id, article, iso(utcnow())),
        )

    async def pageviews_get(self, article: str, days: list[str]) -> dict[str, int]:
        marks = ",".join("?" * len(days))
        rows = await self._all(
            f"SELECT day, views FROM pageviews WHERE article = ? AND day IN ({marks})", (article, *days)
        )
        return {r["day"]: r["views"] for r in rows}

    async def pageviews_put(self, article: str, views: dict[str, int]) -> None:
        await self.db.executemany(
            "INSERT OR REPLACE INTO pageviews (article, day, views) VALUES (?, ?, ?)",
            [(article, d, v) for d, v in views.items()],
        )
        await self.db.commit()

    # ----------------------------------------------------------- entretien
    async def purge(self, now: datetime) -> dict[str, int]:
        """Supprime les données qui ne servent plus, pour un fonctionnement sans entretien.

        Enchères, ventes, tirages et défausses sont gardés (comptabilité). Les ventes observées
        récentes suffisent à l'estimation (médiane des 5 dernières) : les plus vieilles sont datées."""
        def before(days: int) -> str | None:
            return iso(now - timedelta(days=days))

        rules = {
            "price_history": ("observed_at < ?", before(60)),
            "price_cache": ("fetched_at < ?", before(30)),
            "observed_sales": ("ended_at < ?", before(120)),
            "trend_alerts": ("alerted_at < ?", before(30)),
            "pageviews": ("day < ?", f"{now - timedelta(days=60):%Y%m%d}"),
        }
        removed: dict[str, int] = {}
        for table, (where, value) in rules.items():
            cur = await self.db.execute(f"DELETE FROM {table} WHERE {where}", (value,))
            removed[table] = cur.rowcount
        await self.db.commit()
        return removed

    # -------------------------------------------------------------- bilan
    async def report(self, since: datetime) -> dict[str, Any]:
        s = iso(since)
        pulls = await self._all("SELECT rarity, is_shiny FROM pulls WHERE opened_at >= ?", (s,))
        bids = await self._all(
            "SELECT status, amount, dry_run FROM bids b WHERE placed_at >= ? AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)", (s,)
        )
        listings = await self._all("SELECT status, base_amount FROM listings WHERE created_at >= ?", (s,))
        discards = await self._one("SELECT COUNT(*) AS n FROM discards WHERE discarded_at >= ? AND dry_run = 0", (s,))
        closed = await self._all("SELECT dry_run, amount, sale_price FROM bids WHERE closed = 1 AND sold_at >= ? "
                                 "AND sale_price IS NOT NULL", (s,))
        held = await self.positions_summary()
        by_rarity: dict[str, int] = {}
        for p in pulls:
            by_rarity[p["rarity"] or "?"] = by_rarity.get(p["rarity"] or "?", 0) + 1
        return {
            "cards_pulled": len(pulls),
            "pulls_by_rarity": by_rarity,
            "shiny_pulled": sum(p["is_shiny"] for p in pulls),
            "bids_placed": sum(1 for b in bids if not b["dry_run"]),
            "bids_simulated": sum(1 for b in bids if b["dry_run"]),
            "auctions_won": sum(1 for b in bids if b["status"] == "won"),
            "spent_won": sum(b["amount"] for b in bids if b["status"] == "won"),
            "listings_posted": sum(1 for x in listings if x["status"] == "posted"),
            "listings_scheduled": sum(1 for x in listings if x["status"] == "scheduled"),
            "discarded": discards["n"] if discards else 0,
            "positions_sold": sum(1 for c in closed if not c["dry_run"]),
            "profit": sum(c["sale_price"] - c["amount"] for c in closed if not c["dry_run"]),
            "profit_simulated": sum(c["sale_price"] - c["amount"] for c in closed if c["dry_run"]),
            "positions_held": held["count"],
            "capital_in_positions": held["real_cost"] + held["sim_cost"],
        }
