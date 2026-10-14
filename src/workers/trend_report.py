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
from datetime import datetime, timedelta

from src.core.database import Database, parse_iso
from src.core.models import min_next_bid

HORIZON = timedelta(days=7)
SOURCES = {"google": "Google Trends", "wikipedia": "Wikipédia"}


@dataclass
class SourceStats:
    signals: int = 0
    pending: int = 0
    no_data: int = 0
    profits: list[int] = field(default_factory=list)
    examples: list[tuple[int, str, int, int]] = field(default_factory=list)  # (bénéfice, titre, achat, revente)

    def hits(self, min_profit: int) -> int:
        return sum(1 for p in self.profits if p >= min_profit)


async def measure_signals(db: Database, since: datetime, now: datetime) -> dict[str, SourceStats]:
    out: dict[str, SourceStats] = {}
    for row in await db.signals_since(since):
        stats = out.setdefault(row["source"], SourceStats())
        stats.signals += 1
        end = parse_iso(row["end_at"])
        if end is None or end > now:
            stats.pending += 1
            continue
        final = await db.observed_final(row["auction_id"])
        buy = min_next_bid(final, row["min_bid"]) if final else row["min_bid"]
        later = await db.observed_prices_between(row["card_id"], end, end + HORIZON, row["auction_id"])
        if not later:
            if now < end + HORIZON:
                stats.pending += 1
            else:
                stats.no_data += 1
            continue
        resale = int(statistics.median(later))
        stats.profits.append(resale - buy)
        stats.examples.append((resale - buy, row["title"] or row["card_id"], buy, resale))
    return out


async def trends_text(db: Database, days: int, min_profit: int, now: datetime) -> str:
    since = now - timedelta(days=days)
    measured = await measure_signals(db, since, now)
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
    return "\n".join(lines)
