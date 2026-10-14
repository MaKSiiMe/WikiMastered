"""Partie commune aux sources de tendances (Wikipédia, Google Trends).

Un « signal » = une carte en vente au moment où son sujet est en tendance. Chaque signal
est enregistré (table trend_signals) pour mesurer ensuite si le prix de la carte a monté.
Discord ne reçoit que les cartes sous le budget d'une position, en un seul message par passage.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from src.core import timeutils
from src.core.models import Auction
from src.core.text import normalize_title
from src.workers.base import Context


def title_matches(card_title: str, term: str, *, exact: bool = False) -> bool:
    """Même titre, ou (hors mode exact) l'un contient l'autre en mots entiers, sur 2 mots au moins.

    « Squid Game » ↔ « squid game saison 3 » : oui. « décharge » ↔ « Décharge publique » : non
    (un seul mot rapprocherait trop de cartes sans rapport)."""
    title, wanted = normalize_title(card_title), normalize_title(term)
    if title == wanted:
        return True
    if exact:
        return False
    shorter, longer = sorted((title, wanted), key=len)
    return len(shorter.split()) >= 2 and f" {shorter} " in f" {longer} "


async def position_budget(ctx: Context) -> int:
    """TRENDS_MAX_PRICE, ou par défaut le budget d'une position (solde × ratio / places)."""
    s = ctx.settings
    if s.trends_max_price > 0:
        return s.trends_max_price
    return int(await ctx.api.balance() * s.portfolio_capital_ratio / max(1, s.portfolio_slots))


async def scan_market(ctx: Context, *, source: str, term: str, detail: str, max_price: int,
                      exact: bool = False, extra=None) -> list[str]:
    """Cherche la carte au marché : enregistre chaque vente trouvée comme signal, renvoie une ligne
    Discord par carte ayant de nouvelles ventes sous `max_price`. `extra(auction)` ajoute un détail."""
    auctions, _ = await ctx.api.market_page(sort="ending_soon", q=term)
    now = datetime.now(timeutils.PARIS)
    me = ctx.api.user_id
    by_card: dict[str, list[Auction]] = {}
    for a in auctions:
        if not title_matches(a.title, term, exact=exact) or a.status != "active" or a.seconds_left(now) <= 0:
            continue
        if me and (a.seller_id or "").lower() == me:
            continue
        if not await ctx.db.record_signal(source=source, term=term, card_id=a.catalog_id, title=a.title,
                                          auction_id=a.id, min_bid=a.min_bid, end_at=a.end_at):
            continue  # déjà signalée
        await ctx.db.mark_term_matched(source, term)
        if a.min_bid > max_price:
            continue
        by_card.setdefault(a.catalog_id, []).append(a)
    return [await alert_line(ctx, sales, detail, extra(sales[0]) if extra else "") for sales in by_card.values()]


async def alert_line(ctx: Context, sales: list[Auction], detail: str, extra: str = "") -> str:
    """Une ligne par carte, quel que soit le nombre de ses ventes en cours."""
    first = min(sales, key=lambda a: a.end_at)
    observed, n = await ctx.db.observed_prices(first.catalog_id, ctx.settings.price_recent_sales)
    estimate = (f"estimation ~{statistics.median(observed):.0f} WB ({n} vente(s) vue(s))" if observed
                else "aucune vente observée")
    rarities = "/".join(sorted({a.rarity or "?" for a in sales}))
    bids = sorted(a.min_bid for a in sales)
    price = f"mise min {bids[0]} WB" if len(bids) == 1 else f"{len(bids)} ventes, mise min {bids[0]} à {bids[-1]} WB"
    end = first.end_at.astimezone(timeutils.PARIS)
    when = "fin" if len(sales) == 1 else "première fin"
    return f"**{first.title}** {rarities}{extra} — {detail} — {price}, {estimate} — {when} {end:%d/%m %H:%M}"


def flush(ctx: Context, title: str, lines: list[str]) -> None:
    """Un seul message par passage ; `lines` contient une ligne par carte."""
    if lines:
        shown = "\n".join(lines[:15]) + (f"\n… et {len(lines) - 15} autre(s)" if len(lines) > 15 else "")
        ctx.notifier.info(f"{title} : {len(lines)} carte(s) en vente", shown)
