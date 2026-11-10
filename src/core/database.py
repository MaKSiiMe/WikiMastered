"""Stockage SQLite local : cache et historique des cotes, tirages, enchères, ventes."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.timeutils import utcnow

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
-- Cotes figées par l'analyse de l'inventaire (sous VALUE_SCAN_FREEZE_BELOW) : jamais redemandées, jamais purgées.
CREATE TABLE IF NOT EXISTS frozen_values (
    card_id TEXT NOT NULL, rarity TEXT NOT NULL, value REAL NOT NULL, frozen_at TEXT NOT NULL,
    PRIMARY KEY (card_id, rarity)
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
    auction_id TEXT PRIMARY KEY, card_id TEXT, title TEXT, rarity TEXT,
    is_shiny INTEGER NOT NULL DEFAULT 0, current_bid INTEGER, base_amount INTEGER,
    min_bid INTEGER, end_at TEXT, seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observed_sales (
    auction_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, title TEXT, rarity TEXT,
    is_shiny INTEGER NOT NULL DEFAULT 0, price INTEGER NOT NULL, ended_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observed_card ON observed_sales (card_id, ended_at);
-- Plus haut prix observé de chaque carte parmi les ventes purgées (OBSERVED_KEEP_DAYS) : la défausse ne doit pas
-- oublier qu'une carte s'est déjà vendue cher.
CREATE TABLE IF NOT EXISTS observed_max (card_id TEXT PRIMARY KEY, price INTEGER NOT NULL);
-- Ce que Wikidata dit de l'article de chaque carte (étiquettes de thème) ; `rules` : empreinte des classes cherchées
-- (relu quand config/etiquettes.yaml change) ; `facts` NULL : pas d'élément Wikidata.
CREATE TABLE IF NOT EXISTS card_facts (
    title TEXT PRIMARY KEY, facts TEXT, rules TEXT NOT NULL, fetched_at TEXT NOT NULL
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
    ("auctions_seen", "seller_id", "TEXT"),
    ("auctions_seen", "bidder_id", "TEXT"),
    ("observed_sales", "seller_id", "TEXT"),
    ("observed_sales", "buyer_id", "TEXT"),
    ("observed_sales", "base_amount", "INTEGER"),
    # Récap de nuit du sniper : plafond de la mise, prix final payé par le gagnant.
    ("bids", "ceiling", "INTEGER"),
    ("bids", "final_price", "INTEGER"),
    # Image animée (GIF) : se vend-elle mieux ? (commande gifs)
    ("auctions_seen", "is_gif", "INTEGER NOT NULL DEFAULT 0"),
    ("observed_sales", "is_gif", "INTEGER NOT NULL DEFAULT 0"),
    # Ventilation du sniper (sniper-report --breakdown) : l'enchère telle que vue au moment de notre mise.
    ("bids", "min_bid", "INTEGER"),
    ("bids", "start_price", "INTEGER"),
    ("bids", "seconds_left", "INTEGER"),
    # Secondes entre la dernière lecture d'une enchère et sa fin (négatif : lue après sa fin, prix final sûr).
    ("observed_sales", "seen_gap", "INTEGER"),
]
# Vestiges d'anciennes versions, supprimés au démarrage d'une base existante.
# Paquets (ouverts par l'extension depuis le 16/10) et tendances (arrêtées le 19/10) : retirés le 25/10. Ventes en
# cours des cartes de valeur (surveillance du marché) : retirées le 26/10.
OBSOLETE = ["DROP TABLE IF EXISTS trend_alerts", "DROP INDEX IF EXISTS idx_auctions_title",
            *(f"DROP TABLE IF EXISTS {t}" for t in ("pulls", "pack_opens", "pack_checks", "trend_signals", "trend_terms",
                                                     "pageviews", "value_sightings"))]
# Victoire remisée à la main avant la correction du 27/10 : le prix payé est le prix final, pas la mise du bot.
# Ventes observées hors L jusqu'au 29/10 : prix faux (UR et SR surtout vues les 14 et 15/10, au lancement, le sniper ne
# suivant plus que les L depuis), effacées ; leur plus haut prix passe d'abord dans observed_max, comme à la purge, pour
# que la défausse n'oublie pas qu'une carte s'est vendue. Date fixe : rejouées à chaque démarrage, elles n'effacent
# pas les ventes vues depuis.
NON_L_PURGE_BEFORE = "2026-10-29T00:00:00+00:00"
REPAIRS = ["UPDATE bids SET amount = final_price WHERE status = 'won' AND dry_run = 0 AND final_price > amount",
           "INSERT INTO observed_max (card_id, price) SELECT card_id, MAX(price) FROM observed_sales "
           f"WHERE rarity <> 'L' AND ended_at < '{NON_L_PURGE_BEFORE}' GROUP BY card_id "
           "ON CONFLICT(card_id) DO UPDATE SET price = MAX(observed_max.price, excluded.price)",
           f"DELETE FROM observed_sales WHERE rarity <> 'L' AND ended_at < '{NON_L_PURGE_BEFORE}'"]
# Ventes observées gardées N jours (estimations, ventes factices, commandes hours et gifs).
OBSERVED_KEEP_DAYS = 60
# Une paire vendeur -> acheteur vue au moins autant de fois est suspecte (ventes factices entre comptes).
WASH_PAIR_MIN = 2

HELD = "status IN ('won', 'sim_won') AND closed = 0"
# Tes propres achats repris par le bot pour la revente (MANUAL_RESALE_TAG) : des positions comme les autres, mais
# hors des chiffres du sniper (mises, gagnées, dépenses du jour).
MANUAL_REASON = "achat manuel"
NOT_MANUAL = f"reason NOT LIKE '{MANUAL_REASON}%'"
# Revente à 1 WB d'une position invendue POSITION_FIRE_SALE_AFTER fois (motif de la vente).
FIRE_SALE_REASON = "liquidation"
# Vente demandée sur Discord (/vendre) : publiée à la première place libre, avant les autres.
MANUAL_SALE_REASON = "vente manuelle"
# Vente observée suspecte : vendeur = acheteur, ou paire vendeur -> acheteur déjà vue (alias o).
SUSPECT_SALE = ("(o.seller_id IS NOT NULL AND o.buyer_id IS NOT NULL AND (o.seller_id = o.buyer_id OR "
                "(SELECT COUNT(*) FROM observed_sales p WHERE p.seller_id = o.seller_id "
                f"AND p.buyer_id = o.buyer_id) >= {WASH_PAIR_MIN}))")
# Une vente « incertaine » a peut-être été créée : elle reste ouverte jusqu'à sa vérification. « waiting » : revente
# d'une position gardée hors des places de vente face à une concurrente sous son prix d'achat (jusqu'au 20/10 ; plus
# créée, une ancienne est publiée comme une vente programmée).
LISTING_OPEN = "('scheduled', 'waiting', 'posted', 'simulated', 'uncertain')"


def _observed_filter(card_id: str, rarity: str | None, shiny: bool | None) -> tuple[str, tuple]:
    where, params = "card_id = ?", [card_id]
    if rarity is not None:
        where += " AND rarity = ?"
        params.append(rarity)
    if shiny is not None:
        where += " AND is_shiny = ?"
        params.append(int(shiny))
    return where + f" AND NOT {SUSPECT_SALE}", tuple(params)


# Lecture d'une enchère au moins aussi récente que celle gardée : une fin ne recule pas, une mise ne baisse pas. Le
# 25/10, après la relecture de Tom Holland surenchérie et prolongée, la liste du marché l'a montrée deux fois encore
# en tête à l'ancienne fin.
FRESHER = ("(julianday(excluded.end_at) >= julianday(auctions_seen.end_at) "
           "AND COALESCE(excluded.current_bid, 0) >= COALESCE(auctions_seen.current_bid, 0))")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = aiosqlite.connect(self.path)
        conn.daemon = True  # une base non fermée ne bloque jamais l'arrêt du processus
        self._db = await conn
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            if column not in existing:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_observed_pair ON observed_sales (seller_id, buyer_id)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_seen_card ON auctions_seen (card_id, end_at)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_seen_end ON auctions_seen (end_at)")
        for statement in (*OBSOLETE, *REPAIRS):
            await self._db.execute(statement)
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

    # ------------------------------------------------------- faits Wikidata
    async def card_facts(self) -> dict[str, tuple[dict | None, str, datetime]]:
        """Titre -> (faits Wikidata ou None, empreinte des règles, date de lecture)."""
        return {r["title"]: (json.loads(r["facts"]) if r["facts"] else None, r["rules"], parse_iso(r["fetched_at"]))
                for r in await self._all("SELECT title, facts, rules, fetched_at FROM card_facts")}  # type: ignore[misc]

    async def card_facts_put(self, facts: dict[str, dict | None], rules: str) -> None:
        now = iso(utcnow())
        await self.db.executemany(
            "INSERT INTO card_facts (title, facts, rules, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(title) DO UPDATE "
            "SET facts = excluded.facts, rules = excluded.rules, fetched_at = excluded.fetched_at",
            [(t, json.dumps(f, ensure_ascii=False) if f is not None else None, rules, now) for t, f in facts.items()])
        await self.db.commit()

    # ---------------------------------------------------------------- cotes
    async def price_get(self, card_id: str) -> tuple[datetime, dict[str, tuple[float | None, int | None]]] | None:
        rows = await self._all(
            "SELECT rarity, average, sales_count, fetched_at FROM price_cache WHERE card_id = ?", (card_id,)
        )
        if not rows:
            return None
        fetched = min(parse_iso(r["fetched_at"]) for r in rows)  # type: ignore[type-var]
        return fetched, {r["rarity"]: (r["average"], r["sales_count"]) for r in rows}

    async def price_cache_state(self) -> dict[str, tuple[datetime, bool]]:
        """Cote en cache de chaque carte : (date, la plus ancienne de ses raretés ; au moins une vente connue)."""
        rows = await self._all("SELECT card_id, MIN(fetched_at) AS fetched, MAX(average IS NOT NULL) AS priced "
                               "FROM price_cache GROUP BY card_id")
        return {r["card_id"]: (parse_iso(r["fetched"]), bool(r["priced"])) for r in rows}  # type: ignore[misc]

    async def known_values(self) -> dict[str, float | None]:
        """Plus haute valeur connue de chaque carte déjà cotée (cote du site en cache ou figée, toutes raretés), ventes
        observées comprises (même suspectes : par prudence) ; None : cotée, mais aucune vente connue. Une carte jamais
        cotée est absente."""
        out: dict[str, float | None] = {}
        for r in await self._all("SELECT card_id, MAX(average) AS v FROM price_cache GROUP BY card_id "
                                 "UNION ALL SELECT card_id, MAX(value) FROM frozen_values GROUP BY card_id"):
            out[r["card_id"]] = max((x for x in (out.get(r["card_id"]), r["v"]) if x is not None), default=None)
        for r in await self._all("SELECT card_id, MAX(price) AS v FROM observed_sales GROUP BY card_id "
                                 "UNION ALL SELECT card_id, price FROM observed_max"):
            if r["card_id"] in out:
                out[r["card_id"]] = max((x for x in (out[r["card_id"]], r["v"]) if x is not None), default=None)
        return out

    async def frozen_values(self) -> dict[str, dict[str, float]]:
        """Cotes figées : {carte: {rareté: cote}}."""
        out: dict[str, dict[str, float]] = {}
        for r in await self._all("SELECT card_id, rarity, value FROM frozen_values"):
            out.setdefault(r["card_id"], {})[r["rarity"]] = r["value"]
        return out

    async def freeze_values(self, card_id: str, values: dict[str, float]) -> None:
        now = iso(utcnow())
        await self.db.executemany(
            "INSERT INTO frozen_values (card_id, rarity, value, frozen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(card_id, rarity) DO NOTHING", [(card_id, r, v, now) for r, v in values.items()])
        await self.db.commit()

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

    # ------------------------------------------------------------- enchères
    async def record_bid(self, *, auction_id: str, card_id: str, title: str, rarity: str | None,
                         is_shiny: bool, amount: int, reference_price: float | None, reason: str,
                         night: bool, dry_run: bool, status: str, end_at: datetime, note: str = "",
                         ceiling: int | None = None, min_bid: int | None = None,
                         start_price: int | None = None, seconds_left: int | None = None) -> int:
        return await self._exec(
            "INSERT INTO bids (auction_id, card_id, title, rarity, is_shiny, amount, reference_price, reason, "
            "night, dry_run, status, end_at, placed_at, note, ceiling, min_bid, start_price, seconds_left) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (auction_id, card_id, title, rarity, int(is_shiny), amount, reference_price, reason,
             int(night), int(dry_run), status, iso(end_at), iso(utcnow()), note, ceiling, min_bid, start_price,
             seconds_left),
        )

    async def bids_since(self, since: datetime) -> list[aiosqlite.Row]:
        """Dernière mise de chaque enchère misée depuis `since`, avec son prix final (relu au règlement, sinon
        vu au marché), la dernière revente de la carte gagnée et la première mise (`first_*`, pour la ventilation)."""
        return await self._all(
            "SELECT b.*, (SELECT COUNT(*) FROM bids x WHERE x.auction_id = b.auction_id) AS n_bids, "
            "COALESCE(b.final_price, (SELECT price FROM observed_sales o WHERE o.auction_id = b.auction_id)) AS final, "
            "(SELECT current_bid FROM auctions_seen s WHERE s.auction_id = b.auction_id) AS last_seen, "
            "l.status AS listing_status, l.base_amount AS list_price, l.post_at AS list_post_at, "
            "f.amount AS first_amount, f.ceiling AS first_ceiling, f.min_bid AS first_min, "
            "f.start_price AS first_start, f.seconds_left AS first_seconds_left, f.placed_at AS first_placed_at, "
            # Enchère terminée : sa fiche passe des enchères vues aux ventes observées (harvest_ended_auctions).
            "COALESCE((SELECT base_amount FROM auctions_seen s WHERE s.auction_id = b.auction_id), "
            "(SELECT base_amount FROM observed_sales o WHERE o.auction_id = b.auction_id)) AS seen_start "
            "FROM bids b LEFT JOIN listings l ON l.id = (SELECT MAX(id) FROM listings WHERE bid_id = b.id) "
            "LEFT JOIN bids f ON f.id = (SELECT MIN(id) FROM bids WHERE auction_id = b.auction_id) "
            f"WHERE b.id = (SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id) AND b.{NOT_MANUAL} "
            "AND (SELECT MIN(placed_at) FROM bids WHERE auction_id = b.auction_id) >= ? ORDER BY b.placed_at",
            (iso(since),),
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

    async def bids_to_settle(self, before: datetime, limit: int = 3,
                             outbid_since: datetime | None = None) -> list[aiosqlite.Row]:
        """Dernière mise confirmée de chaque enchère échue, à vérifier sur le serveur. Une mise surenchérie
        l'est aussi (fin après `outbid_since`) : le 17/10, des L « perdues » à notre propre prix avaient été
        marquées surenchéries sur une lecture du marché antérieure à notre mise, sans être relues à la fin."""
        return await self._all(
            "SELECT * FROM bids b WHERE (status IN ('placed', 'simulated') OR (status = 'outbid' AND end_at >= ?)) "
            "AND end_at < ? AND id = (SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id) "
            "ORDER BY end_at LIMIT ?",
            (iso(outbid_since or before), iso(before), limit),
        )

    async def vanished_bids(self, since: datetime, note: str) -> list[aiosqlite.Row]:
        """Mises réelles closes perdues car leur enchère était introuvable, finies depuis `since`."""
        return await self._all("SELECT * FROM bids WHERE status = 'lost' AND dry_run = 0 AND note = ? AND end_at >= ? "
                               "ORDER BY end_at", (note, iso(since)))

    async def collection_cards(self, statuses: tuple[str, ...]) -> list[tuple[str, str | None]]:
        """(titre, rareté) des achats « collection » du bot ayant ce statut."""
        marks = ",".join("?" * len(statuses))
        rows = await self._all(f"SELECT DISTINCT title, rarity FROM bids WHERE reason LIKE 'collection%' "
                               f"AND status IN ({marks})", statuses)
        return [(r["title"], r["rarity"]) for r in rows if r["title"]]

    async def collection_auctions_engaged(self, card_id: str) -> set[str]:
        """Enchères « collection » en cours du bot pour cette carte (dernière mise en tête ou simulée)."""
        rows = await self._all(
            "SELECT auction_id FROM bids b WHERE card_id = ? AND reason LIKE 'collection%' "
            "AND status IN ('placed', 'simulated', 'uncertain') AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)", (card_id,))
        return {r["auction_id"] for r in rows}

    async def committed_since(self, since: datetime, dry_run: bool = False) -> int:
        """WikiBidous engagés : enchères en tête non réglées + enchères gagnées depuis `since`.

        En DRY_RUN, ce sont les mises simulées (en cours ou remportées) qui comptent."""
        row = await self._one(
            f"SELECT COALESCE(SUM(amount), 0) AS s FROM bids b WHERE dry_run = ? AND placed_at >= ? AND {NOT_MANUAL} "
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
        out = {"count": 0, "real_count": 0, "real_cost": 0, "sim_cost": 0}
        for r in rows:
            out["count"] += r["n"]
            out["real_count"] += 0 if r["dry_run"] else r["n"]
            out["sim_cost" if r["dry_run"] else "real_cost"] += r["cost"]
        return out

    async def position_results(self, since: datetime) -> list[aiosqlite.Row]:
        """Positions réelles du sniper closes depuis `since` (commande roi) : revendues (sale_price), ou gardées
        invendues (sale_price NULL). Hors cartes de collection et achats manuels repris."""
        return await self._all(
            f"SELECT amount, sale_price, sold_at FROM bids WHERE closed = 1 AND dry_run = 0 AND sold_at >= ? "
            f"AND reason NOT LIKE 'collection%' AND {NOT_MANUAL} ORDER BY sold_at", (iso(since),))

    async def held_positions(self) -> list[aiosqlite.Row]:
        """Positions détenues (achetées, pas encore revendues), avec leur vente en cours s'il y en a une."""
        return await self._all(
            "SELECT b.*, l.status AS listing_status, l.base_amount AS listing_price, l.target_end AS listing_end "
            f"FROM bids b LEFT JOIN listings l ON l.bid_id = b.id AND l.status IN {LISTING_OPEN} "
            "WHERE b.status IN ('won', 'sim_won') AND b.closed = 0 ORDER BY b.dry_run, b.settled_at")

    async def open_listings(self) -> list[aiosqlite.Row]:
        """Ventes programmées ou en cours (commande Discord /ventes), les plus proches de la fin d'abord."""
        return await self._all(f"SELECT * FROM listings WHERE status IN {LISTING_OPEN} "
                               "ORDER BY COALESCE(target_end, post_at)")

    async def known_titles(self, text: str, limit: int = 25) -> list[str]:
        """Titres de cartes vues sur le marché contenant `text` (autocomplétion de /snipe et /prix)."""
        rows = await self._all("SELECT DISTINCT title FROM auctions_seen WHERE title LIKE ? ORDER BY length(title) "
                               "LIMIT ?", (f"%{text}%", limit))
        return [r["title"] for r in rows if r["title"]]

    async def bought_card_ids(self) -> set[str]:
        """Cartes achetées pour de vrai par le bot (positions et collection), revendues ou non."""
        rows = await self._all("SELECT DISTINCT card_id FROM bids WHERE status = 'won' AND dry_run = 0")
        return {r["card_id"] for r in rows if r["card_id"]}

    async def positions_to_list(self) -> list[aiosqlite.Row]:
        return await self._all(
            f"SELECT * FROM bids b WHERE {HELD} AND NOT EXISTS (SELECT 1 FROM listings l "
            f"WHERE l.bid_id = b.id AND l.status IN {LISTING_OPEN}) ORDER BY settled_at"
        )

    async def waiting_sales(self, dry_run: bool) -> list[aiosqlite.Row]:
        """Ventes qui attendent une place de vente : positions détenues pas encore en vente et mises de positions pas
        encore réglées (`amount`, `reference_price` ; `reason` de leur vente programmée s'il y en a une), puis doublons
        programmés (`amount` NULL, `base_amount`). `dry_run` : simulées ou réelles."""
        return await self._all(
            "SELECT b.amount, b.reference_price, l.reason, NULL AS base_amount FROM bids b "
            "LEFT JOIN listings l ON l.bid_id = b.id AND l.status IN ('scheduled', 'waiting') "
            "WHERE b.reason NOT LIKE 'collection%' AND b.dry_run = ? "
            "AND ((b.status IN ('won', 'sim_won') AND b.closed = 0) "
            "OR (b.status IN ('placed', 'simulated', 'uncertain') "
            "AND b.id = (SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id))) "
            "AND NOT EXISTS (SELECT 1 FROM listings p WHERE p.bid_id = b.id "
            "AND p.status IN ('posted', 'simulated', 'uncertain')) "
            "UNION ALL SELECT NULL, NULL, reason, base_amount FROM listings "
            "WHERE bid_id IS NULL AND status IN ('scheduled', 'waiting') AND dry_run = ?",
            (int(dry_run), int(dry_run)))

    async def close_position(self, bid_id: int, sale_price: int | None) -> None:
        await self.update_bid(bid_id, closed=1, sale_price=sale_price, sold_at=utcnow())

    async def pending_bids(self) -> dict[str, int]:
        """Mises du bot pas encore réglées, enchère finie ou non : elles comptent tant que leur résultat
        n'est pas relu (sinon, entre la fin de l'enchère et ce relevé, elles échappent aux plafonds).

        `positions` : mises de positions (hors collection), `real_positions` les réelles seulement ;
        `sim_cost` : WikiBidous des mises simulées."""
        row = await self._one(
            "SELECT COALESCE(SUM(reason NOT LIKE 'collection%'), 0) AS positions, "
            "COALESCE(SUM(reason NOT LIKE 'collection%' AND dry_run = 0), 0) AS real_positions, "
            "COALESCE(SUM(CASE WHEN status = 'simulated' THEN amount ELSE 0 END), 0) AS sim_cost "
            "FROM bids b WHERE status IN ('placed', 'simulated', 'uncertain') "
            "AND id = (SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)")
        return {"positions": int(row["positions"]), "real_positions": int(row["real_positions"]),
                "sim_cost": int(row["sim_cost"])} if row else {"positions": 0, "real_positions": 0, "sim_cost": 0}

    async def my_purchase(self, card_id: str, rarity: str | None, shiny: bool, buyer_id: str,
                          since: datetime, until: datetime) -> aiosqlite.Row | None:
        """Ta dernière vente observée de cette carte (même rareté, shiny ou non), finie entre `since` et `until`, et
        pas déjà une position (du bot ou un achat manuel repris)."""
        return await self._one(
            "SELECT auction_id, title, price, ended_at FROM observed_sales WHERE card_id = ? AND rarity IS ? "
            "AND is_shiny = ? AND lower(buyer_id) = ? AND ended_at >= ? AND ended_at <= ? AND auction_id NOT IN "
            "(SELECT auction_id FROM bids WHERE status = 'won') ORDER BY ended_at DESC LIMIT 1",
            (card_id, rarity, int(shiny), buyer_id.lower(), iso(since), iso(until)))

    async def holds_position(self, card_id: str, rarity: str | None, shiny: bool) -> bool:
        """Une position réelle de cette carte (même rareté, shiny ou non) attend déjà sa revente."""
        return await self._one(f"SELECT 1 FROM bids WHERE {HELD} AND dry_run = 0 AND card_id = ? AND rarity IS ? "
                               "AND is_shiny = ?", (card_id, rarity, int(shiny))) is not None

    async def record_manual_position(self, *, auction_id: str, card_id: str, title: str, rarity: str | None,
                                     is_shiny: bool, amount: int, reference_price: float | None,
                                     bought_at: datetime) -> int:
        """Ton achat repris comme une position du bot, gagnée à `bought_at` (sa revente cherche un exemplaire
        arrivé depuis)."""
        return await self._exec(
            "INSERT INTO bids (auction_id, card_id, title, rarity, is_shiny, amount, reference_price, reason, "
            "night, dry_run, status, end_at, placed_at, settled_at, final_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'won', ?, ?, ?, ?)",
            (auction_id, card_id, title, rarity, int(is_shiny), amount, reference_price, MANUAL_REASON,
             iso(bought_at), iso(bought_at), iso(utcnow()), amount))

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
        """Ventes à publier, la plus rentable d'abord : une place libérée va à la revente à 1 WB (heure de fin choisie),
        puis au plus grand gain au prix demandé (prix de départ − prix d'achat ; tout le prix pour un doublon ou une
        carte de paquet), puis à la plus ancienne. Le 26/10, les 5 places prises par les reventes du sniper,
        Gabby Williams (L d'un paquet, 1407 WB) attendait depuis 2 jours derrière des reventes au prix d'achat."""
        return await self._all(
            "SELECT l.* FROM listings l LEFT JOIN bids b ON b.id = l.bid_id "
            "WHERE l.status IN ('scheduled', 'waiting') AND l.post_at <= ? "
            f"ORDER BY l.reason LIKE '{FIRE_SALE_REASON}%' DESC, l.reason LIKE '{MANUAL_SALE_REASON}%' DESC, "
            "l.base_amount - COALESCE(b.amount, 0) DESC, "
            "l.post_at", (iso(now),)
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

    async def last_failed_relist(self, bid_id: int) -> aiosqlite.Row | None:
        """Dernière remise en vente (après une vente sans acheteur) de cette position, si elle a échoué."""
        row = await self._one("SELECT status, base_amount, relist_count, reason, duration_minutes FROM listings "
                              "WHERE bid_id = ? ORDER BY id DESC LIMIT 1", (bid_id,))
        return row if row and row["status"] in ("failed", "antibot") and row["relist_count"] else None

    async def unsold_durations(self, bid_id: int) -> list[int]:
        """Durées (minutes) des ventes sans acheteur de cette position, de la plus ancienne à la plus récente."""
        rows = await self._all("SELECT duration_minutes FROM listings WHERE bid_id = ? "
                               "AND status IN ('unsold', 'sim_unsold') ORDER BY id", (bid_id,))
        return [int(r["duration_minutes"] or 0) for r in rows]

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

    async def own_listing_end(self, card_id: str, exclude: int) -> datetime | None:
        """Fin de la vente réelle du bot déjà en cours pour cette carte (un autre exemplaire) : le site refuse d'en
        publier une deuxième, avec « Cette carte est déjà engagée dans un échange en attente » (29/10, Zidane)."""
        row = await self._one("SELECT MAX(target_end) AS end_at FROM listings WHERE status = 'posted' AND dry_run = 0 "
                              "AND card_id = ? AND id != ?", (card_id, exclude))
        return datetime.fromisoformat(row["end_at"]) if row and row["end_at"] else None

    async def simulated_active_listings(self, now: datetime) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE status = 'simulated' AND target_end > ?",
                              (iso(now),))
        return int(row["n"]) if row else 0

    async def reopen_favorite_positions(self) -> int:
        """Positions closes faute de revente parce que la carte était en favori (16/10 : Place publique) : rouvertes,
        et leurs refus « en favori » ne comptent plus comme des échecs. Renvoie leur nombre."""
        favorite = "status = 'failed' AND error LIKE '%favori%'"
        rows = await self._all(f"SELECT id FROM bids WHERE closed = 1 AND sale_price IS NULL AND id IN "
                               f"(SELECT bid_id FROM listings WHERE {favorite})")
        for r in rows:
            await self.update_bid(r["id"], closed=0, sold_at=None)
        await self._exec(f"UPDATE listings SET status = 'favorite' WHERE {favorite}")
        return len(rows)

    async def reopen_antibot_positions(self) -> int:
        """Positions closes après des refus « Vérification anti-bot requise » (01/11 : 16 cartes, 17 162 WB, abandonnées
        de 07:04 à 07:59) : rouvertes, et ces refus ne comptent plus comme des échecs. Renvoie leur nombre."""
        antibot = "status = 'failed' AND error LIKE '%anti-bot%'"
        rows = await self._all(f"SELECT id FROM bids WHERE closed = 1 AND sale_price IS NULL AND id IN "
                               f"(SELECT bid_id FROM listings WHERE {antibot})")
        for r in rows:
            await self.update_bid(r["id"], closed=0, sold_at=None)
        await self._exec(f"UPDATE listings SET status = 'antibot' WHERE {antibot}")
        return len(rows)

    async def reopen_same_card_positions(self, since: datetime) -> int:
        """Positions closes après des refus « déjà engagée dans un échange » depuis `since`, pendant qu'un autre
        exemplaire de la même carte était en vente par le bot (29/10 : 2e Zidane, abandonné en 2 min pendant que le
        1er se vendait) : rouvertes, et ces refus ne comptent plus comme des échecs. Plus anciennes (17/10 : Les Feux
        de l'amour), l'exemplaire a pu partir depuis : laissées closes. Renvoie leur nombre."""
        same_card = (f"status = 'failed' AND created_at >= '{iso(since)}' AND error LIKE '%engagée dans un échange%' "
                     "AND EXISTS (SELECT 1 FROM listings o "
                     "WHERE o.card_id = listings.card_id AND o.id != listings.id AND o.dry_run = 0 AND o.posted_at "
                     "IS NOT NULL AND o.posted_at <= listings.created_at AND (o.settled_at IS NULL OR o.settled_at >= "
                     "listings.created_at))")
        rows = await self._all(f"SELECT id FROM bids WHERE closed = 1 AND sale_price IS NULL AND id IN "
                               f"(SELECT bid_id FROM listings WHERE {same_card})")
        for r in rows:
            await self.update_bid(r["id"], closed=0, sold_at=None)
        await self._exec(f"UPDATE listings SET status = 'same_card' WHERE {same_card}")
        return len(rows)

    async def advance_position_listings(self, now: datetime) -> int:
        """Reventes de positions programmées plus tard : avancées à maintenant (même durée). Renvoie leur nombre."""
        rows = await self._all("SELECT id, duration_minutes FROM listings WHERE status = 'scheduled' "
                               "AND bid_id IS NOT NULL AND post_at > ?", (iso(now),))
        for r in rows:
            await self.update_listing(r["id"], post_at=now,
                                      target_end=now + timedelta(minutes=r["duration_minutes"] or 0))
        return len(rows)

    async def get_listing(self, listing_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM listings WHERE id = ?", (listing_id,))

    async def cancel_scheduled(self, bid_id: int) -> int:
        """Annule les ventes pas encore publiées de cette position (/garder, bouton « Garder »)."""
        cur = await self.db.execute("UPDATE listings SET status = 'cancelled', settled_at = ? WHERE bid_id = ? "
                                    "AND status IN ('scheduled', 'waiting')", (iso(utcnow()), bid_id))
        await self.db.commit()
        return cur.rowcount

    async def open_listings_for_bid(self, bid_id: int) -> list[aiosqlite.Row]:
        """Ventes programmées ou en cours de cette position."""
        return await self._all(f"SELECT * FROM listings WHERE bid_id = ? AND status IN {LISTING_OPEN} ORDER BY id",
                               (bid_id,))

    async def held_for_title(self, title: str) -> list[aiosqlite.Row]:
        """Positions détenues de cette carte (titre exact, casse ignorée)."""
        return await self._all(f"SELECT * FROM bids WHERE {HELD} AND lower(title) = lower(?)", (title,))

    async def update_listing(self, listing_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        await self._exec(f"UPDATE listings SET {cols} WHERE id = ?", (*values, listing_id))

    async def pending_listings_count(self, real_only: bool = False) -> int:
        """Ventes programmées, qui réservent une place de vente (pas celles en attente d'un meilleur marché)."""
        extra = " AND owned_card_id NOT LIKE 'sim-%'" if real_only else ""
        row = await self._one(f"SELECT COUNT(*) AS n FROM listings WHERE status = 'scheduled'{extra}")
        return int(row["n"]) if row else 0

    async def cancel_scheduled_duplicates(self) -> int:
        """Annule les doublons programmés pas encore publiés (/doublons désactiver) ; pas les ventes manuelles."""
        cur = await self.db.execute(
            "UPDATE listings SET status = 'cancelled', settled_at = ? WHERE bid_id IS NULL "
            f"AND status IN ('scheduled', 'waiting') AND reason NOT LIKE '{MANUAL_SALE_REASON}%'", (iso(utcnow()),))
        await self.db.commit()
        return cur.rowcount

    async def waiting_duplicates_count(self) -> int:
        """Doublons programmés, pas encore publiés (ils attendent une place de vente)."""
        row = await self._one("SELECT COUNT(*) AS n FROM listings WHERE bid_id IS NULL AND status = 'scheduled'")
        return int(row["n"]) if row else 0

    async def scheduled_owned_ids(self) -> set[str]:
        rows = await self._all("SELECT owned_card_id FROM listings WHERE status IN ('scheduled', 'waiting', 'posted', "
                               "'uncertain')")
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
        """Enchères vues ; une lecture plus ancienne (fin plus tôt ou mise plus basse : la liste du marché peut avoir
        une lecture de retard sur la fiche de l'enchère) ne remplace pas une plus récente."""
        if not rows:
            return
        rows = [{"seller_id": None, "bidder_id": None, "is_gif": 0, **r} for r in rows]
        await self.db.executemany(
            "INSERT INTO auctions_seen (auction_id, card_id, title, rarity, is_shiny, current_bid, "
            "base_amount, min_bid, end_at, seen_at, seller_id, bidder_id, is_gif) VALUES (:auction_id, :card_id, "
            ":title, :rarity, :is_shiny, :current_bid, :base_amount, :min_bid, :end_at, :seen_at, "
            ":seller_id, :bidder_id, :is_gif) ON CONFLICT(auction_id) DO UPDATE SET "
            + ", ".join(f"{col} = CASE WHEN {FRESHER} THEN excluded.{col} ELSE {col} END"
                        for col in ("current_bid", "min_bid", "end_at", "seen_at", "bidder_id")),
            rows,
        )
        await self.db.commit()

    async def harvest_ended_auctions(self, before: datetime, max_gap_seconds: float = 120) -> int:
        """Transforme les enchères vues juste avant leur fin en ventes observées.

        Le dernier `current_bid` vu par le sniper (qui scrute les fins imminentes toutes
        les ~3 s) approche le prix final. Une enchère sans mise n'est pas une vente. `seen_gap` garde l'écart entre
        cette lecture et la fin, pour mesurer les prix notés trop tôt (voir HARVEST_DELAY du sniper)."""
        rows = await self._all("SELECT * FROM auctions_seen WHERE end_at < ?", (iso(before),))
        done, sales = [], []
        for r in rows:
            end, seen = parse_iso(r["end_at"]), parse_iso(r["seen_at"])
            if end is None or end >= before:
                continue
            done.append((r["auction_id"],))
            if r["current_bid"] and seen and (end - seen).total_seconds() <= max_gap_seconds:
                sales.append((r["auction_id"], r["card_id"], r["title"], r["rarity"], r["is_shiny"],
                              r["current_bid"], iso(end), r["seller_id"], r["bidder_id"], r["base_amount"],
                              r["is_gif"], round((end - seen).total_seconds())))
        await self.db.executemany(
            "INSERT OR IGNORE INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at, "
            "seller_id, buyer_id, base_amount, is_gif, seen_gap) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", sales)
        await self.db.executemany("DELETE FROM auctions_seen WHERE auction_id = ?", done)
        await self.db.commit()
        return len(sales)

    async def correct_observed_sale(self, auction_id: str, price: int, buyer_id: str | None) -> None:
        """Prix final et gagnant relus au règlement d'une de nos mises : ils remplacent la dernière lecture."""
        await self.db.execute("UPDATE observed_sales SET price = ?, buyer_id = COALESCE(?, buyer_id) "
                              "WHERE auction_id = ?", (price, buyer_id, auction_id))
        await self.db.commit()

    async def gif_stats(self) -> list[aiosqlite.Row]:
        """Ventes observées par rareté, GIF ou non : nombre, cartes, prix et rapport prix final / départ."""
        return await self._all(
            "SELECT rarity, is_gif, price, base_amount, card_id FROM observed_sales WHERE is_shiny = 0 "
            "AND rarity IS NOT NULL AND ended_at >= (SELECT COALESCE(MIN(ended_at), '') FROM observed_sales "
            "WHERE is_gif = 1)")

    async def hour_stats(self, rarities: list[str]) -> list[aiosqlite.Row]:
        """Ventes observées de ces raretés (sans shiny ni ventes suspectes) : prix, départ et heure de fin."""
        marks = ", ".join("?" for _ in rarities)
        return await self._all(
            "SELECT rarity, price, base_amount, ended_at FROM observed_sales o WHERE is_shiny = 0 "
            f"AND rarity IN ({marks}) AND NOT {SUSPECT_SALE} ORDER BY ended_at", tuple(rarities))

    async def backtest_sales(self, rarity: str) -> list[aiosqlite.Row]:
        """Ventes observées d'une rareté (sans shiny ni ventes suspectes), des plus anciennes aux plus récentes."""
        return await self._all(
            "SELECT card_id, price, base_amount, ended_at FROM observed_sales o WHERE is_shiny = 0 AND rarity = ? "
            f"AND NOT {SUSPECT_SALE} ORDER BY ended_at", (rarity,))

    async def site_average_history(self, rarity: str) -> list[aiosqlite.Row]:
        """Cotes du site relevées pour une rareté, des plus anciennes aux plus récentes."""
        return await self._all(
            "SELECT card_id, average, observed_at FROM price_history WHERE rarity = ? AND average IS NOT NULL "
            "ORDER BY observed_at", (rarity,))

    async def bid_outcomes(self, since: datetime) -> list[aiosqlite.Row]:
        """Mises de positions réelles depuis `since`, pour la valeur d'une mise (core/winmodel.py) : acceptées par le
        site (gagnées ou perdues), et refusées par la vérification anti-bot (des occasions manquées)."""
        return await self._all(
            "SELECT auction_id, amount, reference_price, is_shiny, status, sale_price, reason FROM bids "
            "WHERE dry_run = 0 AND reason LIKE 'revente%' AND placed_at >= ? AND (status IN ('won', 'lost', 'outbid') "
            "OR (status = 'rejected' AND note LIKE '%anti-bot%')) ORDER BY id", (iso(since),))

    async def position_bids_outcome(self, since: datetime, rarity: str) -> aiosqlite.Row:
        """Enchères de positions misées depuis `since` (dernière mise de chacune) : nombre, gagnées (réelles ou
        simulées), mises réelles parties (refusées exclues)."""
        rows = await self._all(
            "SELECT COUNT(*) AS bids, COALESCE(SUM(status IN ('won', 'sim_won')), 0) AS wins, "
            "COALESCE(SUM(dry_run = 0 AND status != 'rejected'), 0) AS real_bids FROM bids b "
            f"WHERE rarity = ? AND reason NOT LIKE 'collection%' AND {NOT_MANUAL} AND placed_at >= ? "
            "AND id = (SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)", (rarity, iso(since)))
        return rows[0]

    async def observed_prices(self, card_id: str, limit: int, rarity: str | None = None,
                              shiny: bool | None = None) -> tuple[list[int], int]:
        """(prix des `limit` dernières ventes observées, plus récentes d'abord ; nombre total).

        Une même carte vaut très différemment selon sa rareté et si elle est shiny : `rarity`
        et `shiny` limitent aux ventes comparables (None = sans filtre)."""
        where, params = _observed_filter(card_id, rarity, shiny)
        rows = await self._all(f"SELECT price FROM observed_sales o WHERE {where} ORDER BY ended_at DESC", params)
        return [r["price"] for r in rows[:limit]], len(rows)

    async def observed_by_rarity(self, card_id: str, limit: int,
                                 shiny: bool = False) -> dict[str, tuple[list[tuple[int, bool]], int]]:
        """Ventes observées d'une carte, par rareté : {rareté: (`limit` dernières (prix, sans concurrence), nombre)}.

        « Sans concurrence » : une seule mise, au prix de départ (prix final = prix de départ)."""
        rows = await self._all("SELECT rarity, price, base_amount FROM observed_sales o WHERE card_id = ? "
                               f"AND is_shiny = ? AND rarity IS NOT NULL AND NOT {SUSPECT_SALE} "
                               "ORDER BY ended_at DESC", (card_id, int(shiny)))
        groups: dict[str, list[tuple[int, bool]]] = {}
        for r in rows:
            single = r["base_amount"] is not None and r["price"] <= r["base_amount"]
            groups.setdefault(r["rarity"], []).append((r["price"], single))
        return {rarity: (sales[:limit], len(sales)) for rarity, sales in groups.items()}

    async def observed_for_cards(self, card_ids: Iterable[str], limit: int
                                 ) -> dict[tuple[str, bool], dict[str, tuple[list[tuple[int, bool]], int]]]:
        """`observed_by_rarity` de beaucoup de cartes à la fois (valeur de la collection, /valeur) : par (carte,
        shiny), puis par rareté. Requêtes groupées par 500 cartes."""
        ids = sorted(set(card_ids))
        groups: dict[tuple[str, bool], dict[str, list[tuple[int, bool]]]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = await self._all(
                "SELECT card_id, is_shiny, rarity, price, base_amount FROM observed_sales o WHERE card_id IN "
                f"({', '.join('?' * len(chunk))}) AND rarity IS NOT NULL AND NOT {SUSPECT_SALE} "
                "ORDER BY ended_at DESC", chunk)
            for r in rows:
                single = r["base_amount"] is not None and r["price"] <= r["base_amount"]
                groups.setdefault((r["card_id"], bool(r["is_shiny"])), {}).setdefault(r["rarity"], []).append(
                    (r["price"], single))
        return {key: {rarity: (sales[:limit], len(sales)) for rarity, sales in by_rarity.items()}
                for key, by_rarity in groups.items()}

    async def site_averages(self, card_ids: Iterable[str], since: datetime) -> dict[str, dict[str, float]]:
        """Cote du site en cache depuis `since`, de beaucoup de cartes à la fois : {carte: {rareté: moyenne}}."""
        ids = sorted(set(card_ids))
        out: dict[str, dict[str, float]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for r in await self._all(f"SELECT card_id, rarity, average FROM price_cache WHERE card_id IN "
                                     f"({', '.join('?' * len(chunk))}) AND average IS NOT NULL AND fetched_at >= ?",
                                     [*chunk, iso(since)]):
                out.setdefault(r["card_id"], {})[r["rarity"]] = r["average"]
        return out

    async def top_prices(self, since: datetime, shiny: bool, limit: int, min_sales: int) -> list[aiosqlite.Row]:
        """Cartes les plus chères en moyenne (commande /top) : ventes observées depuis `since`, par carte et rareté,
        au moins `min_sales` ventes (une vente isolée ne fait pas un prix), ventes suspectes exclues."""
        return await self._all(
            "SELECT card_id, MAX(title) AS title, rarity, COUNT(*) AS sales, AVG(price) AS average, "
            "MIN(price) AS low, MAX(price) AS high FROM observed_sales o WHERE is_shiny = ? AND rarity IS NOT NULL AND ended_at >= ? "
            f"AND NOT {SUSPECT_SALE} GROUP BY card_id, rarity HAVING COUNT(*) >= ? ORDER BY average DESC LIMIT ?",
            (int(shiny), iso(since), min_sales, limit))

    async def shiny_on_sale(self, now: datetime, sort: str = "end", max_price: int | None = None) -> list[aiosqlite.Row]:
        """Shiny en vente vus par le sniper (commande /shiny), `min_price` : mise minimale à la dernière lecture. `sort` :
        « end » (fin la plus proche), « low » ou « high » (mise minimale)."""
        order = {"end": "end_at", "low": "min_price, end_at", "high": "min_price DESC, end_at"}[sort]
        rows = await self._all(
            "SELECT auction_id, card_id, title, rarity, current_bid, end_at, seen_at, COALESCE(min_bid, base_amount) "
            f"AS min_price FROM auctions_seen WHERE is_shiny = 1 AND end_at > ? ORDER BY {order}", (iso(now),))
        return [r for r in rows if max_price is None or (r["min_price"] or 0) <= max_price]

    async def cards_priced_between(self, since: datetime, low: float, high: float, limit: int,
                                   min_sales: int) -> list[aiosqlite.Row]:
        """Cartes normales vendues en moyenne entre `low` et `high` depuis `since` (contre-offre d'un échange), les
        plus vendues d'abord : des cartes qui partent vite, à demander en plus."""
        return await self._all(
            "SELECT MAX(title) AS title, rarity, COUNT(*) AS sales, AVG(price) AS average FROM observed_sales o "
            f"WHERE is_shiny = 0 AND rarity IS NOT NULL AND ended_at >= ? AND NOT {SUSPECT_SALE} "
            "GROUP BY card_id, rarity HAVING COUNT(*) >= ? AND AVG(price) BETWEEN ? AND ? "
            "ORDER BY sales DESC LIMIT ?", (iso(since), min_sales, low, high, limit))

    async def observed_for_title(self, title: str) -> list[aiosqlite.Row]:
        """Ventes observées d'une carte, par son titre (commande prices) : plus récentes d'abord, suspectes marquées."""
        return await self._all(
            f"SELECT card_id, title, rarity, is_shiny, price, base_amount, ended_at, {SUSPECT_SALE} AS suspect "
            "FROM observed_sales o WHERE title = ? COLLATE NOCASE ORDER BY ended_at DESC", (title,))

    async def bids_for_title(self, title: str) -> list[aiosqlite.Row]:
        """Achats de positions du bot sur cette carte (commande prices) : mise, estimation à l'achat, issue."""
        return await self._all(
            "SELECT card_id, rarity, is_shiny, amount, reference_price, status, dry_run, placed_at, sale_price FROM bids "
            "WHERE title = ? COLLATE NOCASE AND reason NOT LIKE 'collection%' AND status IN ('won', 'sim_won') "
            "ORDER BY placed_at DESC", (title,))

    async def observed_prices_since(self, card_id: str, since: datetime, limit: int, rarity: str | None = None,
                                    shiny: bool | None = None) -> list[int]:
        where, params = _observed_filter(card_id, rarity, shiny)
        rows = await self._all(f"SELECT price FROM observed_sales o WHERE {where} AND ended_at >= ? "
                               "ORDER BY ended_at DESC LIMIT ?", (*params, iso(since), limit))
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

    async def recap_stats(self, since: datetime) -> dict[str, int]:
        """Activité depuis `since`, pour le récap périodique."""
        s = iso(since)

        async def one(sql: str, params: tuple = (s,)) -> int:
            row = await self._one(sql, params)
            return int(row[0] or 0) if row else 0

        position = "reason NOT LIKE 'collection%'"
        sniper = f"{position} AND {NOT_MANUAL}"
        return {
            "observed": await one("SELECT COUNT(*) FROM observed_sales WHERE ended_at >= ?"),
            "bids_real": await one(f"SELECT COUNT(*) FROM bids WHERE placed_at >= ? AND dry_run = 0 AND {sniper}"),
            "bids_sim": await one(f"SELECT COUNT(*) FROM bids WHERE placed_at >= ? AND dry_run = 1 AND {sniper}"),
            "bids_collection": await one("SELECT COUNT(*) FROM bids WHERE placed_at >= ? "
                                         "AND reason LIKE 'collection%'"),
            "won": await one(f"SELECT COUNT(*) FROM bids WHERE settled_at >= ? AND status IN ('won', 'sim_won') "
                             f"AND {sniper}"),
            "won_collection": await one("SELECT COUNT(*) FROM bids WHERE settled_at >= ? "
                                        "AND status IN ('won', 'sim_won') AND reason LIKE 'collection%'"),
            "held": await one(f"SELECT COUNT(*) FROM bids WHERE {HELD} AND {position}", ()),
            "held_real": await one(f"SELECT COUNT(*) FROM bids WHERE {HELD} AND {position} AND dry_run = 0", ()),
            "posted": await one("SELECT COUNT(*) FROM listings WHERE posted_at >= ? AND status <> 'simulated' "
                                "AND dry_run = 0"),
            "sold": await one("SELECT COUNT(*) FROM listings WHERE settled_at >= ? AND status = 'sold'"),
            "sold_wb": await one("SELECT COALESCE(SUM(sale_price), 0) FROM listings WHERE settled_at >= ? "
                                 "AND status = 'sold'"),
            "unsold": await one("SELECT COUNT(*) FROM listings WHERE settled_at >= ? AND status = 'unsold'"),
            "sim_sold": await one("SELECT COUNT(*) FROM listings WHERE settled_at >= ? AND status = 'sim_sold'"),
            "sim_unsold": await one("SELECT COUNT(*) FROM listings WHERE settled_at >= ? AND status = 'sim_unsold'"),
        }

    async def reset_simulation(self, *, apply: bool) -> dict[str, int]:
        """Mises et ventes simulées (positions, reventes, collection en simulation) : compte, et efface avec
        `apply`. Les mises et ventes réelles, les ventes observées et le reste de l'historique sont gardés."""
        listings = ("owned_card_id LIKE 'sim-%' OR status IN ('simulated', 'sim_sold', 'sim_unsold') "
                    "OR bid_id IN (SELECT id FROM bids WHERE dry_run = 1)")
        counts = {
            "mises": int((await self._one("SELECT COUNT(*) AS n FROM bids WHERE dry_run = 1"))["n"]),
            "ventes": int((await self._one(f"SELECT COUNT(*) AS n FROM listings WHERE {listings}"))["n"]),
        }
        if apply:
            await self.db.execute(f"DELETE FROM listings WHERE {listings}")
            await self.db.execute("DELETE FROM bids WHERE dry_run = 1")
            await self.db.commit()
        return counts

    async def cheapest_ask(self, card_id: str, rarity: str | None, shiny: bool, after: datetime,
                           exclude: str, me: str | None, lowest: float = 0) -> int | None:
        """Mise minimale la plus basse des autres ventes de cette carte (même rareté, shiny ou non) encore en cours
        après `after` : la concurrence d'une revente. Lue sans requête, dans les ventes déjà vues. Celles sous
        `lowest` sont écartées (un prix de départ, pas un prix de vente)."""
        row = await self._one(
            "SELECT MIN(COALESCE(min_bid, base_amount)) AS ask FROM auctions_seen WHERE card_id = ? AND rarity IS ? "
            "AND is_shiny = ? AND end_at > ? AND auction_id != ? AND (seller_id IS NULL OR lower(seller_id) != ?) "
            "AND COALESCE(min_bid, base_amount) >= ?",
            (card_id, rarity, int(shiny), iso(after), exclude, (me or "").lower(), lowest))
        return int(row["ask"]) if row and row["ask"] is not None else None

    # ----------------------------------------------------------- entretien
    async def purge(self, now: datetime) -> dict[str, int]:
        """Supprime les données qui ne servent plus, pour un fonctionnement sans entretien.

        Enchères, ventes et défausses sont gardés (comptabilité). Les ventes observées récentes suffisent à
        l'estimation (médiane des 5 dernières) : ~26 000 par jour le 24/10, 1,2 Go en 120 jours. Celles de plus de
        OBSERVED_KEEP_DAYS partent, leur plus haut prix par carte est gardé (observed_max) pour la défausse."""
        def before(days: int) -> str | None:
            return iso(now - timedelta(days=days))

        await self.db.execute(
            "INSERT INTO observed_max (card_id, price) SELECT card_id, MAX(price) FROM observed_sales "
            "WHERE ended_at < ? GROUP BY card_id "
            "ON CONFLICT(card_id) DO UPDATE SET price = MAX(observed_max.price, excluded.price)",
            (before(OBSERVED_KEEP_DAYS),))

        rules = {
            "price_history": ("observed_at < ?", before(60)),
            "price_cache": ("fetched_at < ?", before(30)),
            "observed_sales": ("ended_at < ?", before(OBSERVED_KEEP_DAYS)),
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
        bids = await self._all(
            f"SELECT status, amount, dry_run, reason FROM bids b WHERE placed_at >= ? AND {NOT_MANUAL} AND id = "
            "(SELECT MAX(id) FROM bids WHERE auction_id = b.auction_id)", (s,)
        )
        listings = await self._all("SELECT status, base_amount FROM listings WHERE created_at >= ?", (s,))
        discards = await self._one("SELECT COUNT(*) AS n FROM discards WHERE discarded_at >= ? AND dry_run = 0", (s,))
        closed = await self._all("SELECT dry_run, amount, sale_price FROM bids WHERE closed = 1 AND sold_at >= ? "
                                 "AND sale_price IS NOT NULL", (s,))
        held = await self.positions_summary()
        won = [b for b in bids if b["status"] == "won"]
        _collection = lambda b: (b["reason"] or "").startswith("collection")  # noqa: E731
        dup = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(sale_price), 0) AS wb FROM listings "
                              "WHERE bid_id IS NULL AND status = 'sold' AND settled_at >= ?", (s,))
        return {
            # Bilan du 16/10 : « 585 mise(s) placée(s) » comptait les refus (vérification, mise trop basse), et
            # « 25 gagnée(s) » mêlait positions et cartes de collection.
            "bids_placed": sum(1 for b in bids if not b["dry_run"] and b["status"] != "rejected"),
            "bids_rejected": sum(1 for b in bids if not b["dry_run"] and b["status"] == "rejected"),
            "bids_simulated": sum(1 for b in bids if b["dry_run"]),
            "auctions_won": sum(1 for b in won if not _collection(b)),
            "spent_won": sum(b["amount"] for b in won if not _collection(b)),
            "won_collection": sum(1 for b in won if _collection(b)),
            "spent_collection": sum(b["amount"] for b in won if _collection(b)),
            "listings_posted": sum(1 for x in listings if x["status"] == "posted"),
            "doublons_vendus": dup["n"] if dup else 0,
            "doublons_recette": dup["wb"] if dup else 0,
            "listings_scheduled": sum(1 for x in listings if x["status"] == "scheduled"),
            "discarded": discards["n"] if discards else 0,
            "positions_sold": sum(1 for c in closed if not c["dry_run"]),
            "profit": sum(c["sale_price"] - c["amount"] for c in closed if not c["dry_run"]),
            "profit_simulated": sum(c["sale_price"] - c["amount"] for c in closed if c["dry_run"]),
            # Positions réelles seulement : les simulées à part, sans WB réels immobilisés.
            "positions_held": held["real_count"],
            "capital_in_positions": held["real_cost"],
            "positions_held_sim": held["count"] - held["real_count"],
            "capital_simulated": held["sim_cost"],
        }
