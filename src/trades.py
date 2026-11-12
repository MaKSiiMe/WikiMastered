"""Échanges entre joueurs : valeur de chaque côté, verdict, contre-offre à envoyer.

Sert au worker « echanges » (chaque échange qu'on te propose, évalué une fois) et à /echange (un échange que tu
envisages). Valeur d'une carte : médiane de ses ventes observées des TRADE_DAYS derniers jours (même rareté, shiny à
part, ventes suspectes écartées) ; moins de TRADE_MIN_SALES ventes : l'estimation du bot, sans requête. Le nombre de
ventes dit si elle part vite : le 28/10, un joueur refusait Jeffrey Epstein (L, ~12 000) « qui se vend pas trop » contre
Emma Watson + Zendaya (~5 500 chacune), alors que le bot en voyait autant de ventes que des deux autres.
Aucune route connue ne propose ni ne contre un échange : le bot dit quoi envoyer, tu l'envoies sur le site.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations

from src.config import RARITIES
from src.core.client import ApiError, RateLimited
from src.core.models import Trade, TradeItem
from src.core.pricing import robust_median
from src.core.timeutils import utcnow
from src.core.notifier import Level
from src.roi import plural, wb
from src.workers.base import Context

log = logging.getLogger(__name__)

# Couleur d'un échange (src/core/colors.py), sur /echange comme dans le salon des ventes.
LEVELS = {"equitable": Level.INFO, "avantage": Level.SUCCESS, "defavorable": Level.WARNING,
          "incomplet": Level.WARNING}
TRADE_DAYS = 7
TRADE_MIN_SALES = 3  # en dessous, sur TRADE_DAYS jours : l'estimation du bot (5 dernières ventes observées)
SUGGESTED_CARDS = 4  # cartes proposées à demander en plus
SUGGESTED_MIN_SALES = 5  # … vendues au moins tant de fois sur TRADE_DAYS jours (elles partent vite)
_RARITY_PREFIX = re.compile(rf"^({'|'.join(RARITIES)})\s*·\s*(.+)$")
_RARITY_SUFFIX = re.compile(rf"^(.+?)\s*\(({'|'.join(RARITIES)})\)$")


@dataclass(frozen=True)
class Priced:
    title: str
    rarity: str | None
    shiny: bool = False
    value: float | None = None  # None : jamais vue vendue, valeur inconnue
    sales: int = 0  # ventes observées sur TRADE_DAYS jours

    @property
    def label(self) -> str:
        return f"{self.rarity or '?'} · {self.title}{' ✨' if self.shiny else ''}"

    @property
    def line(self) -> str:
        """Une carte dans une colonne de l'échange : son nom, puis sa valeur et ses ventes."""
        if self.value is None:
            return f"**{self.label}**\n⚠️ valeur inconnue"
        return f"**{self.label}**\n~{wb(self.value)} WB · {plural(self.sales, 'vente')} en {TRADE_DAYS} j"


@dataclass
class Evaluation:
    given: list[Priced]
    received: list[Priced]
    wb_given: int = 0
    wb_received: int = 0
    margin: float = 0.1
    offers: list[str] = field(default_factory=list)  # contre-offres à envoyer (counter_offers)

    @staticmethod
    def _total(cards: list[Priced], extra: int) -> float:
        return sum(c.value or 0 for c in cards) + extra

    @property
    def given_total(self) -> float:
        return self._total(self.given, self.wb_given)

    @property
    def received_total(self) -> float:
        return self._total(self.received, self.wb_received)

    @property
    def gap(self) -> float:
        """Ce que tu gagnes (négatif : ce que tu perds)."""
        return self.received_total - self.given_total

    def fair(self, given: float, received: float) -> bool:
        top = max(given, received)
        return top == 0 or min(given, received) >= (1 - self.margin) * top

    @property
    def verdict(self) -> str:
        """« equitable », « avantage » (tu gagnes plus que la marge), « defavorable », ou « incomplet » : une carte
        sans valeur connue, comptée 0, fausse le total. Le 30/10, jarry0 demandait 2 cartes illisibles (« ? · ? »)
        contre rien : annoncé « Équitable (écart ~0 WB) »."""
        if self.unknown:
            return "incomplet"
        if self.fair(self.given_total, self.received_total):
            return "equitable"
        return "avantage" if self.gap > 0 else "defavorable"

    @property
    def unknown(self) -> list[Priced]:
        return [c for c in self.given + self.received if c.value is None]

    @property
    def headline(self) -> str:
        if self.verdict == "incomplet":
            return f"❓ À vérifier : {plural(len(self.unknown), 'carte sans valeur connue', 'cartes sans valeur connue')}"
        if self.verdict == "equitable":
            return f"🤝 Équitable (écart ~{wb(abs(self.gap))} WB)"
        if self.verdict == "avantage":
            return f"✅ À ton avantage : {wb(self.gap, True)} WB"
        return f"❌ Défavorable : {wb(self.gap, True)} WB"

    def side(self, cards: list[Priced], extra: int) -> str:
        lines = [c.line for c in cards] + ([f"**+ {wb(extra)} WB**"] if extra else [])
        return "\n".join(lines) or "rien"

    @property
    def columns(self) -> list[tuple[str, str]]:
        """Les deux côtés de l'échange, l'un à côté de l'autre."""
        return [(f"Tu donnes ~{wb(self.given_total)} WB", self.side(self.given, self.wb_given)),
                (f"Tu reçois ~{wb(self.received_total)} WB", self.side(self.received, self.wb_received))]

    @property
    def notes(self) -> str:
        """Sous les deux colonnes : le rapport, les cartes sans valeur, la contre-offre."""
        parts = []
        if self.given_total and self.received_total:
            parts.append(f"Tu reçois {self.received_total * 100 / self.given_total:.0f} % de ce que tu donnes.")
        if self.unknown:
            parts.append("⚠️ Verdict incomplet : " + ", ".join(c.label for c in self.unknown)
                         + (" sans valeur connue, comptée 0." if len(self.unknown) == 1
                            else " sans valeur connue, comptées 0."))
        if self.offers:
            parts.append("**Contre-offre à envoyer**\n" + "\n".join(f"• {o}" for o in self.offers))
        return "\n\n".join(parts)

    @property
    def text(self) -> str:
        return "\n\n".join([*(f"**{name}**\n{value}" for name, value in self.columns), self.notes]).strip()


async def card_value(ctx: Context, card_id: str | None, title: str, rarity: str | None, shiny: bool,
                     at: datetime | None = None) -> Priced:
    """Médiane des ventes observées des TRADE_DAYS derniers jours ; trop peu de ventes : l'estimation du bot.
    Sans rareté : valeur inconnue, plutôt qu'une médiane de toutes ses raretés. `at` : valeur à cette date (ancien
    échange), d'après les ventes des TRADE_DAYS jours d'avant ; sinon l'estimation d'aujourd'hui."""
    if not card_id or not rarity:
        return Priced(title, rarity, shiny)
    prices = await ctx.db.observed_prices_since(card_id, (at or utcnow()) - timedelta(days=TRADE_DAYS), 1000, rarity,
                                                shiny, until=at)
    if len(prices) >= TRADE_MIN_SALES:
        return Priced(title, rarity, shiny, robust_median(prices), len(prices))
    quote = await ctx.prices.quote(card_id, rarity, shiny=shiny, fetch=False)
    return Priced(title, rarity, shiny, quote.average, len(prices))


def parse_card(text: str) -> tuple[str, str | None, bool]:
    """« Emma Watson », « L · Emma Watson », « Emma Watson (L) », « Emma Watson ✨ » -> (titre, rareté, shiny)."""
    text = text.strip()
    shiny = "✨" in text or text.casefold().endswith(" shiny")
    text = text.replace("✨", "").strip()
    if text.casefold().endswith(" shiny"):
        text = text[:-6].strip()
    rarity = None
    if match := _RARITY_PREFIX.match(text):
        rarity, text = match.group(1), match.group(2)
    elif match := _RARITY_SUFFIX.match(text):
        text, rarity = match.group(1), match.group(2)
    return text.strip(), rarity, shiny


async def value_of_title(ctx: Context, text: str) -> Priced:
    """Carte saisie dans /echange : sans rareté précisée, la plus vendue de ce titre."""
    title, rarity, shiny = parse_card(text)
    rows = [r for r in await ctx.db.observed_for_title(title) if bool(r["is_shiny"]) == shiny]
    if rarity:
        rows = [r for r in rows if r["rarity"] == rarity]
    if not rows:
        return Priced(title, rarity, shiny)
    rarity = rarity or Counter(r["rarity"] for r in rows).most_common(1)[0][0]
    card_id = Counter(r["card_id"] for r in rows if r["rarity"] == rarity).most_common(1)[0][0]
    return await card_value(ctx, card_id, rows[0]["title"] or title, rarity, shiny)


async def counter_offers(ctx: Context, e: Evaluation) -> list[str]:
    """Défavorable : ce que tu peux demander en plus (des WB, une carte qui part vite), ou retirer de ton côté."""
    if e.verdict != "defavorable":
        return []
    need = e.given_total - e.received_total
    theirs = " + ".join(c.label for c in e.received) or "rien"
    offers = [f"{theirs} + **{wb(math.ceil(need / 100) * 100)} WB**"]
    taken = {c.title.casefold() for c in e.given + e.received}
    rows = await ctx.db.cards_priced_between(utcnow() - timedelta(days=TRADE_DAYS), need * 0.9, need * 1.6,
                                             SUGGESTED_CARDS + len(taken), SUGGESTED_MIN_SALES)
    cards = [f"{r['rarity']} · {r['title']} (~{wb(r['average'])}, {r['sales']} ventes en {TRADE_DAYS} j)"
             for r in rows if r["title"].casefold() not in taken][:SUGGESTED_CARDS]
    if cards:
        offers.append(f"ou {theirs} + **une carte vers {wb(need)} WB** s'il en a une : " + ", ".join(cards))
    # Plusieurs cartes de ton côté : en garder une partie, au plus près de ce qu'il donne sans redevenir défavorable.
    fair_subsets = [kept for n in range(1, len(e.given)) for kept in combinations(e.given, n)
                    if e.fair(Evaluation._total(list(kept), e.wb_given), e.received_total)]
    if fair_subsets:
        best = max(fair_subsets, key=lambda kept: Evaluation._total(list(kept), e.wb_given))
        removed = [c.label for c in e.given if c not in best]
        offers.append(f"ou retire **{' + '.join(removed)}** de ton côté : tu donnes "
                      f"{' + '.join(c.label for c in best)} (~{wb(Evaluation._total(list(best), e.wb_given))} WB)")
    return offers


async def evaluate(ctx: Context, given: list[Priced], received: list[Priced], wb_given: int = 0,
                   wb_received: int = 0, offers: bool = True) -> Evaluation:
    e = Evaluation(given, received, wb_given, wb_received, ctx.settings.trade_fair_margin)
    if offers:
        e.offers = await counter_offers(ctx, e)
    return e


async def item_details(ctx: Context, item: TradeItem, me: str, owned: bool = False) -> tuple[str, str | None, bool]:
    """(titre, rareté, shiny) d'une carte d'échange. Le 01/11, le site n'en donnait que les identifiants (« ? · ? » pour
    les 7 cartes demandées par Lpancho) : titre lu sur la route des ventes de la carte, rareté de ton exemplaire lue
    dans ta collection. Rareté d'une carte de l'autre joueur : inconnue."""
    title, rarity, shiny = item.title or "?", item.rarity, item.shiny
    if item.card or not item.card_id:
        return title, rarity, shiny
    # Exemplaire que le bot a déjà mis en vente : sa rareté est connue, même s'il a quitté la collection.
    known = await ctx.db.known_copy(item.user_card_id) if item.user_card_id else None
    try:
        title = (await ctx.api.sales_summary(item.card_id, background=True)).wikipedia_title or "?"
        if known:
            return title, known[0], known[1]
        # Le tien, ou celui que tu as reçu d'un échange terminé : cherché dans ta collection.
        if title != "?" and ((item.offered_by or "").lower() == me or owned):
            async for copy in ctx.api.iter_collection(q=title, background=True):
                if copy.id == item.user_card_id:
                    return title, copy.rarity, copy.is_shiny
    except (ApiError, RateLimited) as exc:
        log.debug("carte d'échange %s illisible (%s)", item.card_id, exc)
    return title, rarity, shiny


async def evaluate_trade(ctx: Context, trade: Trade, me: str, offers: bool = True,
                         at: datetime | None = None) -> Evaluation:
    """Échange du site, vu de ton côté : tes cartes (offered_by = toi) et tes WB contre les siens. `offers=False` :
    sans chercher de contre-offre (échange que tu proposes toi-même). `at` : ancien échange, cartes à leur valeur
    de cette date."""
    given, received = [], []
    for item in trade.items:
        title, rarity, shiny = await item_details(ctx, item, me, owned=at is not None)
        priced = await card_value(ctx, item.card_id or (item.card.id if item.card else None), title, rarity, shiny,
                                  at)
        (given if (item.offered_by or "").lower() == me else received).append(priced)
    mine_first = (trade.initiator_id or "").lower() == me
    wb_initiator, wb_recipient = trade.initiator_wikibidous or 0, trade.recipient_wikibidous or 0
    return await evaluate(ctx, given, received, *((wb_initiator, wb_recipient) if mine_first
                                                  else (wb_recipient, wb_initiator)), offers=offers)


def snapshot(trade: Trade, e: Evaluation, me: str, at: str, status: str = "pending") -> dict:
    """Un échange tel qu'il a été vu, pour le rendement des échanges (kv « trades_log ») : cartes et WB de chaque
    côté, à leur valeur du moment."""
    mine_first = (trade.initiator_id or "").lower() == me
    other = (trade.recipient if mine_first else trade.initiator) or {}

    def cards(side: list[Priced]) -> list[dict]:
        return [{"title": c.title, "rarity": c.rarity, "shiny": c.shiny, "value": c.value} for c in side]

    return {"id": trade.id, "seen": at, "with": other.get("username") or "?", "mine": mine_first,
            "given": cards(e.given), "received": cards(e.received), "wb_given": e.wb_given,
            "wb_received": e.wb_received, "given_total": round(e.given_total),
            "received_total": round(e.received_total), "unknown": len(e.unknown), "status": status}


CONCLUDED = ("accept", "complet", "done", "conclu")  # statuts d'un échange fait (champs réels à confirmer)
HISTORY_SHOWN = 15


def concluded(status: str) -> bool:
    return any(word in status.lower() for word in CONCLUDED)


def history_text(history: dict[str, dict], since: str) -> tuple[str, list[str]]:
    """Rendement des échanges vus depuis `since` (kv « trades_log ») : (résumé, une ligne par échange, les plus
    récents d'abord). Seuls les échanges conclus comptent dans le résultat ; un échange terminé sans statut lu sur le
    site est donné à part : il a pu être refusé."""
    rows = sorted((t for t in history.values() if t["seen"] >= since), key=lambda t: t["seen"], reverse=True)
    if not rows:
        return "Aucun échange vu sur la période.", []
    done = [t for t in rows if concluded(t["status"])]
    unsure = [t for t in rows if t["status"] == "ended"]
    pending = [t for t in rows if t["status"] == "pending"]

    def gap(group: list[dict]) -> int:
        return sum(t["received_total"] - t["given_total"] for t in group)

    head = [f"**{plural(len(done), 'échange conclu', 'échanges conclus')}** : "
            f"donné ~{wb(sum(t['given_total'] for t in done))} WB, "
            f"reçu ~{wb(sum(t['received_total'] for t in done))} WB, soit **{wb(gap(done), True)} WB**"
            if done else "Aucun échange conclu sur la période."]
    if unsure:
        head.append(f"{plural(len(unsure), 'terminé', 'terminés')} sans issue connue : {wb(gap(unsure), True)} WB "
                    "s'ils ont été acceptés")
    if pending:
        head.append(f"{len(pending)} en attente")
    marks = {"pending": "⏳ en attente", "ended": "❔ terminé, issue inconnue"}

    def cards(t: dict, side: str, extra: int) -> str:
        names = [f"{c.get('rarity') or '?'} · {c['title']}{' ✨' if c.get('shiny') else ''}" for c in t[side]]
        return " + ".join(names + ([f"{wb(extra)} WB"] if extra else [])) or "rien"

    lines = []
    for t in rows[:HISTORY_SHOWN]:
        state = "✅ conclu" if concluded(t["status"]) else marks.get(t["status"], f"✖️ {t['status']}")
        unknown = " · ⚠️ carte sans valeur connue" if t.get("unknown") else ""
        unknown += " · ↩︎" if t.get("retro") else ""
        lines.append(f"**{t['seen'][8:10]}/{t['seen'][5:7]} · {t['with']}** · {state} · "
                     f"**{wb(t['received_total'] - t['given_total'], True)} WB**{unknown}\n"
                     f"donné : {cards(t, 'given', t['wb_given'])} (~{wb(t['given_total'])})\n"
                     f"reçu : {cards(t, 'received', t['wb_received'])} (~{wb(t['received_total'])})")
    return "\n".join(head), lines

