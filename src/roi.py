"""Rendement des reventes du sniper, jour par jour (commande roi et /roi sur Discord)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.core import timeutils


@dataclass
class Day:
    day: date
    sold: int = 0
    cost: int = 0  # mises des cartes revendues
    net: int = 0  # recette, commission déduite
    kept: int = 0  # invendues gardées
    kept_cost: int = 0

    @property
    def profit(self) -> int:
        return self.net - self.cost

    @property
    def roi(self) -> float | None:
        return self.profit * 100 / self.cost if self.cost else None


@dataclass
class Roi:
    days: list[Day]
    held: tuple[int, int] = (0, 0)  # (positions encore détenues, WB immobilisés)
    totals: Day = field(init=False)

    def __post_init__(self) -> None:
        self.totals = Day(self.days[-1].day if self.days else date.min)
        for d in self.days:
            t = self.totals
            t.sold, t.cost, t.net = t.sold + d.sold, t.cost + d.cost, t.net + d.net
            t.kept, t.kept_cost = t.kept + d.kept, t.kept_cost + d.kept_cost

    @property
    def per_day(self) -> float:
        return self.totals.profit / max(1, len(self.days))


def roi(rows, days: int, today: date, fee: float = 0.0, held: tuple[int, int] = (0, 0)) -> Roi:
    """Positions réelles closes sur les `days` derniers jours (aujourd'hui compris), par jour de Paris : reventes
    (coût, recette nette) et invendues gardées à part (leur coût n'est pas perdu, la carte reste)."""
    by_day = {today - timedelta(days=offset): Day(today - timedelta(days=offset)) for offset in range(days)}
    for r in rows:
        d = by_day.get(datetime.fromisoformat(r["sold_at"]).astimezone(timeutils.PARIS).date())
        if d is None:
            continue
        if r["sale_price"] is None:
            d.kept, d.kept_cost = d.kept + 1, d.kept_cost + r["amount"]
        else:
            d.sold, d.cost, d.net = d.sold + 1, d.cost + r["amount"], d.net + round(r["sale_price"] * (1 - fee))
    return Roi(sorted(by_day.values(), key=lambda d: d.day), held)


def wb(value: float, sign: bool = False) -> str:
    """12 345 (espace fine insécable entre les milliers), signé au besoin."""
    return f"{value:{'+' if sign else ''},.0f}".replace(",", " ")


def plural(n: float, one: str, many: str | None = None) -> str:
    """« 1 mise placée », « 43 mises placées » (`many` : forme plurielle, sinon `one` + s) ; 0 et 1 au singulier."""
    return f"{wb(n)} {one if abs(n) < 2 else many or one + 's'}"


def pct(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value:+.{digits}f}".replace(".", ",") + " %"


def roi_text(r: Roi) -> str:
    """Version console : le résumé, puis un tableau par jour."""
    t = r.totals
    summary = [f"Sur {len(r.days)} jour(s) : {t.sold} revente(s), bénéfice {wb(t.profit, True)} WB, soit "
               f"{wb(r.per_day, True)} WB/jour"]
    if t.cost:
        summary[0] += f" · ROI {pct(t.roi, 1)} ({wb(t.cost)} WB misés sur les cartes revendues)"
    if t.kept:
        summary.append(f"Gardées invendues : {t.kept} carte(s), {wb(t.kept_cost)} WB de mises. En les comptant "
                       f"perdues : {wb(t.profit - t.kept_cost, True)} WB.")
    if r.held[0]:
        summary.append(f"En cours : {r.held[0]} position(s) détenue(s), {wb(r.held[1])} WB immobilisés.")
    lines = ["jour    reventes     coût   recette  bénéfice     ROI   gardées"]
    for d in r.days:
        kept = f"{d.kept} ({wb(d.kept_cost)} WB)" if d.kept else "-"
        lines.append(f"{d.day:%d/%m}   {d.sold:>8}  {wb(d.cost):>7}  {wb(d.net):>8}  {wb(d.profit, True):>8}  "
                     f"{pct(d.roi):>6}   {kept}")
    return "\n".join(summary) + "\n\n" + "\n".join(lines)


BAR_WIDTH = 8


def bar(value: float, top: float) -> str:
    """Barre de `value` rapportée au plus grand écart de la période (graphique sans image)."""
    full = round(BAR_WIDTH * abs(value) / top) if top else 0
    return "█" * full + "·" * (BAR_WIDTH - full)


def roi_discord(r: Roi) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Version Discord : le bénéfice en gros et ses réserves, trois chiffres côte à côte, puis une ligne par jour,
    lisibles sur un téléphone."""
    t = r.totals
    head = [f"## {wb(t.profit, True)} WB"]
    if t.kept:
        head.append(f"⚠️ {plural(t.kept, 'gardée invendue', 'gardées invendues')} ({wb(t.kept_cost)} WB) · en les "
                    f"comptant perdues : {wb(t.profit - t.kept_cost, True)} WB")
    if r.held[0]:
        head.append(f"💼 En cours : {plural(r.held[0], 'position', 'positions')}, {wb(r.held[1])} WB immobilisés")
    columns = [("Par jour", f"{wb(r.per_day, True)} WB"), ("ROI", pct(t.roi, 1)),
               ("Reventes", f"{wb(t.sold)} · {wb(t.cost)} WB misés")]
    days = []
    top = max((abs(d.profit) for d in r.days), default=0)
    for d in reversed(r.days):  # le plus récent en haut
        if not d.sold and not d.kept:
            days.append(f"`{d.day:%d/%m} {bar(0, top)}` aucune revente")
            continue
        mark = "🟢" if d.profit > 0 else "🔴" if d.profit < 0 else "⚪"
        line = f"`{d.day:%d/%m} {bar(d.profit, top)}` {mark} **{wb(d.profit, True)} WB** · {pct(d.roi)} · " \
               f"{plural(d.sold, 'vente')}"
        if d.kept:
            line += f" · {plural(d.kept, 'gardée')}"
        days.append(line)
    return "\n".join(head), columns, days