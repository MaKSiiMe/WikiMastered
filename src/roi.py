"""Rendement des reventes du sniper, jour par jour (commande roi et /roi sur Discord)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from src.core import timeutils


def roi_text(rows, days: int, today: date, fee: float = 0.0, held: tuple[int, int] = (0, 0)) -> str:
    """Positions réelles closes sur les `days` derniers jours (aujourd'hui compris) : par jour, reventes, coût,
    recette nette, bénéfice et ROI (bénéfice / coût) ; les invendues gardées à part (leur coût n'est pas perdu, la
    carte reste). `held` : (positions encore détenues, WB immobilisés)."""
    by_day: dict[date, list] = {}
    for r in rows:
        by_day.setdefault(datetime.fromisoformat(r["sold_at"]).astimezone(timeutils.PARIS).date(), []).append(r)
    lines = ["jour    reventes   coût    recette  bénéfice    ROI   gardées"]
    total_cost = total_net = sold = kept = kept_cost = 0
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        here = by_day.get(day, [])
        resold = [r for r in here if r["sale_price"] is not None]
        unsold = [r for r in here if r["sale_price"] is None]
        cost = sum(r["amount"] for r in resold)
        net = round(sum(r["sale_price"] * (1 - fee) for r in resold))
        total_cost, total_net, sold = total_cost + cost, total_net + net, sold + len(resold)
        kept, kept_cost = kept + len(unsold), kept_cost + sum(r["amount"] for r in unsold)
        roi = f"{(net - cost) * 100 / cost:+.0f} %" if cost else "-"
        gardees = f"{len(unsold)} ({sum(r['amount'] for r in unsold)} WB)" if unsold else "-"
        lines.append(f"{day:%d/%m}   {len(resold):>8}   {cost:>5}   {net:>7}   {net - cost:>+7}   {roi:>6}   {gardees}")
    profit = total_net - total_cost
    summary = [f"Sur {days} jour(s) : {sold} revente(s), bénéfice {profit:+d} WB, soit {profit / days:+.0f} WB/jour"]
    if total_cost:
        summary[0] += f" · ROI {profit * 100 / total_cost:+.1f} % ({total_cost} WB misés sur les cartes revendues)"
    if kept:
        summary.append(f"Gardées invendues : {kept} carte(s), {kept_cost} WB de mises. En les comptant perdues : "
                       f"{profit - kept_cost:+d} WB.")
    if held[0]:
        summary.append(f"En cours : {held[0]} position(s) détenue(s), {held[1]} WB immobilisés.")
    return "\n".join(summary) + "\n\n" + "\n".join(lines)
