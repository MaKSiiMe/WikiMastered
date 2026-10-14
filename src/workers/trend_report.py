"""Mesure des signaux de tendance : auraient-ils rapporté de l'argent ?

Pour chaque signal (carte en vente au moment d'une tendance) :
- achat : il aurait fallu surenchérir sur le prix final observé de cette vente
  (max(ceil(1,1 × prix), prix + 1)), ou payer la mise minimale si personne n'a misé ;
- revente : médiane des ventes de la même carte observées dans les 7 jours suivants.
Un signal est « gagnant » si le bénéfice atteint le bénéfice exigé d'une position.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from src.core import timeutils
from src.core.database import Database, parse_iso
from src.core.models import min_next_bid

HORIZON = timedelta(days=7)
SOURCES = {"google": "Google Trends", "wikipedia": "Wikipédia"}


@dataclass(frozen=True)
class SignalResult:
    source: str
    day: date              # jour du signal, heure de Paris
    card_id: str
    title: str
    status: str            # measured | pending | no_data
    profit: int | None = None
    buy: int | None = None
    resale: int | None = None


@dataclass
class SourceStats:
    signals: int = 0
    pending: int = 0
    no_data: int = 0
    profits: list[int] = field(default_factory=list)
    examples: list[tuple[int, str, int, int]] = field(default_factory=list)  # (bénéfice, titre, achat, revente)
    cards: set[str] = field(default_factory=set)

    def hits(self, min_profit: int) -> int:
        return sum(1 for p in self.profits if p >= min_profit)

    def add(self, r: SignalResult) -> None:
        self.signals += 1
        self.cards.add(r.card_id)
        if r.status == "pending":
            self.pending += 1
        elif r.status == "no_data":
            self.no_data += 1
        else:
            self.profits.append(r.profit)  # type: ignore[arg-type]
            self.examples.append((r.profit, r.title, r.buy, r.resale))  # type: ignore[arg-type]


async def evaluate_signals(db: Database, since: datetime, now: datetime) -> list[SignalResult]:
    out: list[SignalResult] = []
    for row in await db.signals_since(since):
        day = parse_iso(row["signal_at"]).astimezone(timeutils.PARIS).date()  # type: ignore[union-attr]
        base = dict(source=row["source"], day=day, card_id=row["card_id"], title=row["title"] or row["card_id"])
        end = parse_iso(row["end_at"])
        if end is None or end > now:
            out.append(SignalResult(**base, status="pending"))
            continue
        final = await db.observed_final(row["auction_id"])
        buy = min_next_bid(final, row["min_bid"]) if final else row["min_bid"]
        later = await db.observed_prices_between(row["card_id"], end, end + HORIZON, row["auction_id"])
        if not later:
            out.append(SignalResult(**base, status="pending" if now < end + HORIZON else "no_data"))
            continue
        resale = int(statistics.median(later))
        out.append(SignalResult(**base, status="measured", profit=resale - buy, buy=buy, resale=resale))
    return out


async def measure_signals(db: Database, since: datetime, now: datetime) -> dict[str, SourceStats]:
    out: dict[str, SourceStats] = {}
    for r in await evaluate_signals(db, since, now):
        out.setdefault(r.source, SourceStats()).add(r)
    return out


def _cell(stats: SourceStats | None, min_profit: int) -> str:
    if stats is None:
        return "—"
    n = len(stats.profits)
    profit = f"{statistics.median(stats.profits):+.0f}" if n else "—"
    return f"{len(stats.cards)} · {stats.signals} · {n} · {profit} · {stats.hits(min_profit) if n else '—'}"


def common_cards(results: list[SignalResult]) -> list[str]:
    """Cartes signalées par les deux sources (Google et Wikipédia)."""
    by_card: dict[str, tuple[str, set[str]]] = {}
    for r in results:
        by_card.setdefault(r.card_id, (r.title, set()))[1].add(r.source)
    return sorted(title for title, sources in by_card.values() if len(sources) > 1)


async def day_line(db: Database, now: datetime) -> str:
    """Ligne quotidienne du bilan : ce que chaque source a trouvé aujourd'hui."""
    today = now.astimezone(timeutils.PARIS).date()
    since = timeutils.local_dt(today, time(0, 0))
    results = [r for r in await evaluate_signals(db, since, now) if r.day == today]
    terms = await db.terms_since("google", since)
    parts = []
    for source, label in SOURCES.items():
        mine = [r for r in results if r.source == source]
        cards = len({r.card_id for r in mine})
        head = f"{len(terms)} termes → " if source == "google" else ""
        parts.append(f"{label} {head}{cards} carte(s) ({len(mine)} ventes)")
    common = common_cards(results)
    return ("**Tendances du jour** : " + " ; ".join(parts) + " ; en commun : "
            + (", ".join(common[:5]) if common else "aucune") + ".")


async def trends_text(db: Database, days: int, min_profit: int, now: datetime, *, detailed: bool = True) -> str:
    """Les tendances auraient-elles rapporté ? Sans `detailed` : une ligne par source (bilan Discord)."""
    since = now - timedelta(days=days)
    measured = await measure_signals(db, since, now)
    if not detailed:
        return "\n".join(_short_line(label, measured.get(source, SourceStats()), min_profit)
                         for source, label in SOURCES.items())
    lines = [f"**Mesure des tendances ({days} derniers jours)**",
             f"Gagnant = bénéfice ≥ {min_profit} WB (achat en surenchérissant, revente au prix observé ensuite)."]
    for source, label in SOURCES.items():
        stats = measured.get(source, SourceStats())
        head = f"**{label}** : "
        if source == "google":
            terms, matched = await db.terms_stats("google", since)
            head += f"{terms} terme(s), {matched} avec une carte en vente ; "
        n = len(stats.profits)
        head += (f"{stats.signals} signal(aux), {n} mesuré(s), {stats.pending} en attente, "
                 f"{stats.no_data} sans revente observée.")
        lines.append(head)
        if not n:
            continue
        lines.append(f"  Bénéfice médian {statistics.median(stats.profits):+.0f} WB, "
                     f"{stats.hits(min_profit)}/{n} gagnant(s).")
        best = sorted(stats.examples, reverse=True)[:3]
        lines.append("  Meilleurs : " + " ; ".join(f"{t} ({b} → {r}, {p:+d})" for p, t, b, r in best))
        if n < 10:
            lines.append("  Moins de 10 signaux mesurés : trop tôt pour conclure.")
        elif stats.hits(min_profit) * 2 >= n and statistics.median(stats.profits) > 0:
            lines.append("  Signal prometteur : on peut envisager d'en faire une règle d'achat.")
        else:
            lines.append("  Signal pas rentable pour l'instant.")
    lines += await _by_day(db, since, now, min_profit)
    return "\n".join(lines)


def _short_line(label: str, stats: SourceStats, min_profit: int) -> str:
    n = len(stats.profits)
    if not n:
        return f"{label} : aucun signal mesuré."
    hits = stats.hits(min_profit)
    if n < 10:
        verdict = "trop tôt pour conclure"
    elif hits * 2 >= n and statistics.median(stats.profits) > 0:
        verdict = "prometteur"
    else:
        verdict = "pas rentable"
    return f"{label} : {n} mesuré(s), médiane {statistics.median(stats.profits):+.0f} WB, {hits} gagnant(s) → {verdict}"


async def _by_day(db: Database, since: datetime, now: datetime, min_profit: int) -> list[str]:
    results = await evaluate_signals(db, since, now)
    if not results:
        return []
    terms: dict[date, int] = {}
    for row in await db.terms_since("google", since):
        day = parse_iso(row["first_seen"]).astimezone(timeutils.PARIS).date()  # type: ignore[union-attr]
        terms[day] = terms.get(day, 0) + 1
    lines = ["", "**Jour par jour** (cartes · signaux · mesurés · bénéfice médian · gagnants)"]
    for day in sorted({r.day for r in results}):
        cells = []
        for source, label in SOURCES.items():
            stats = None
            for r in results:
                if r.day == day and r.source == source:
                    stats = stats or SourceStats()
                    stats.add(r)
            extra = f" ({terms.get(day, 0)} termes)" if source == "google" else ""
            cells.append(f"{label}{extra} {_cell(stats, min_profit)}")
        common = common_cards([r for r in results if r.day == day])
        lines.append(f"{day:%d/%m} — " + " | ".join(cells) + f" | en commun : {len(common)}")
    common = common_cards(results)
    if common:
        lines.append("Cartes signalées par les deux sources : " + ", ".join(common[:10]))
    return lines
