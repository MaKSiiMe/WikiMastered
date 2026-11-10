"""Rejeu du sniper (commande sniper-backtest) : qu'auraient donné d'autres réglages sur les ventes observées ?

Chaque vente observée de la période est rejouée avec l'estimation que le bot avait à ce moment-là (médiane des
PRICE_RECENT_SALES ventes précédentes de la carte, plafonnée par la cote du site déjà relevée, mêmes filtres de
ventes disputées et d'écart), sans regarder les ventes suivantes. Pour un réglage :
- mise : le prix de départ est sous notre plafond, et la mise poussée (SNIPER_BID_PUSH) atteint SNIPER_MIN_BID_SHARE ;
- gagnée : notre plafond couvre la mise qui bat le prix final (+10 %, règle du site). On paie cette mise poussée
  vers le plafond, et on revend à l'estimation (× --resale).
Le modèle suppose que le gagnant ne surenchérit pas au-delà du prix final : il est optimiste. Ses victoires sont
corrigées par le rapport réel / modèle du réglage actuel sur la même période (mises réelles et simulées).
« À quota égal » : bénéfice si le nombre de mises est borné par celui des mises réelles de la période (la
vérification humaine limite les mises, pas le solde).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from src.config import Settings
from src.core import timeutils
from src.core.database import parse_iso
from src.core.models import min_next_bid
from src.core.pricing import robust_median, trusted_prices

DAY_RATIOS = (0.65, 0.70, 0.75, 0.80)
NIGHT_OFFSETS = (0.0, 0.10)          # ratio de nuit = ratio de jour + écart
PROFIT_RATIOS = (0.0, 0.05, 0.10, 0.15, 0.25)
BID_SHARES = (0.0, 0.40)


@dataclass(frozen=True)
class Case:
    estimate: float
    final: int
    start: int | None
    night: bool


@dataclass(frozen=True)
class Combo:
    day: float
    night: float
    profit_ratio: float
    min_share: float


@dataclass(frozen=True)
class Outcome:
    combo: Combo
    bids: int
    wins: int
    gain: float


def cases_from(sales: Iterable, site: Iterable, since: datetime, s: Settings) -> list[Case]:
    """Ventes de la période (depuis `since`) que le sniper aurait évaluées, avec l'estimation qu'il avait alors."""
    averages: dict[str, tuple[list[datetime], list[float]]] = {}
    for r in site:
        when, values = averages.setdefault(r["card_id"], ([], []))
        when.append(parse_iso(r["observed_at"]))  # type: ignore[arg-type]
        values.append(r["average"])
    history: dict[str, list[tuple[int, bool]]] = {}
    cases = []
    for r in sales:
        end, past = parse_iso(r["ended_at"]), history.setdefault(r["card_id"], [])
        last = past[-s.price_recent_sales:][::-1]  # plus récentes d'abord, comme observed_by_rarity
        if end >= since and len(past) >= s.price_min_sales and last:  # type: ignore[operator]
            estimate = robust_median(trusted_prices(last))
            when, values = averages.get(r["card_id"], ([], []))
            if i := bisect_right(when, end):  # type: ignore[arg-type]
                estimate = min(estimate, values[i - 1] * s.price_site_margin)
            prices = [p for p, _ in last if p > 0]
            contested = sum(not single for _, single in last)
            spread = max(prices) / min(prices) if prices else None
            if contested >= s.sniper_min_contested_sales and not (
                    s.sniper_max_price_spread > 0 and spread is not None and spread > s.sniper_max_price_spread):
                cases.append(Case(estimate, r["price"], r["base_amount"],
                                  timeutils.in_window(end, s.night_start, s.night_end)))  # type: ignore[arg-type]
        past.append((r["price"], r["base_amount"] is not None and r["price"] <= r["base_amount"]))
    return cases


def replay(cases: list[Case], combo: Combo, s: Settings, budget: int, resale: float = 1.0) -> Outcome:
    need = max(s.portfolio_min_profit, int(budget * combo.profit_ratio))
    bids = wins = 0
    gain = 0.0
    for c in cases:
        ceiling = min(math.floor(c.estimate * (combo.night if c.night else combo.day)),
                      math.floor(c.estimate - need), budget)
        if s.sniper_max_bid:
            ceiling = min(ceiling, s.sniper_max_bid)
        start = c.start or 0
        if start > ceiling or start + s.sniper_bid_push * (ceiling - start) < combo.min_share * c.estimate:
            continue  # pas de mise : trop chère dès le départ, ou mise sous SNIPER_MIN_BID_SHARE
        bids += 1
        beat = min_next_bid(c.final, 0)
        if ceiling >= beat:
            wins += 1
            gain += c.estimate * resale - (beat + s.sniper_bid_push * (ceiling - beat))
    return Outcome(combo, bids, wins, gain)


def grid(current: Combo) -> list[Combo]:
    combos = [Combo(d, round(d + o, 2), p, b)
              for d in DAY_RATIOS for o in NIGHT_OFFSETS for p in PROFIT_RATIOS for b in BID_SHARES]
    return combos if current in combos else [*combos, current]


def current_combo(s: Settings) -> Combo:
    return Combo(s.sniper_deal_ratio_max, s.night_deal_ratio_max, s.portfolio_min_profit_ratio, s.sniper_min_bid_share)


def render(cases: list[Case], actual, s: Settings, budget: int, days: float, resale: float = 1.0,
           top: int = 15) -> str:
    """Tableau des réglages, du meilleur bénéfice à quota égal au moins bon ; le réglage actuel toujours affiché."""
    current = current_combo(s)
    base = replay(cases, current, s, budget, resale)
    factor = actual["wins"] / base.wins if base.wins else 1.0
    quota = max(1, actual["real_bids"])
    rows = []
    for o in (replay(cases, c, s, budget, resale) for c in grid(current)):
        gain = o.gain * factor
        rows.append((gain * min(1.0, quota / max(o.bids, 1)), gain, o))
    rows.sort(key=lambda r: -r[0])
    shown = rows[:top] + [r for r in rows[top:] if r[2].combo == current]
    lines = [
        f"Rejeu du sniper : {len(cases)} ventes évaluables sur {days:g} j · budget d'une position {budget} WB · "
        f"revente = estimation ×{resale:g}",
        f"Réglage actuel : modèle {base.bids} mises, {base.wins} gagnées · réel {actual['bids']} mises, "
        f"{actual['wins']} gagnées → victoires du modèle ×{factor:.2f} · quota : {actual['real_bids']} mises réelles",
        "",
        f"{'jour':>5} {'nuit':>5} {'rentab.':>7} {'min':>5} {'mises':>6} {'gagnées':>8} {'bénéfice':>9} "
        f"{'/mise':>6} {'à quota égal':>13}",
    ]
    for at_quota, gain, o in shown:
        c = o.combo
        mark = "  ← actuel" if c == current else ""
        lines.append(f"{c.day:5.2f} {c.night:5.2f} {c.profit_ratio:7.2f} {c.min_share:5.2f} {o.bids:6d} "
                     f"{o.wins * factor:8.1f} {gain:9.0f} {gain / max(o.bids, 1):6.1f} {at_quota:13.0f}{mark}")
    lines += ["", "jour/nuit : SNIPER_DEAL_RATIO_MAX / NIGHT_DEAL_RATIO_MAX · rentab. : PORTFOLIO_MIN_PROFIT_RATIO · "
                  "min : SNIPER_MIN_BID_SHARE"]
    return "\n".join(lines)
