"""Estimation de la valeur de revente d'une carte.

Le prix suit surtout les tendances (actualité, pop culture, mèmes) plus que la rareté, et
la moyenne du site peut être très datée (relevé réel : GMMTV vendue 157 WB, moyenne 391).
Sources, de la plus fiable à la moins fiable :
1. **Ventes observées** par le sniper lui-même (table observed_sales) : médiane des
   QUOTE_RECENT_SALES dernières. Gratuit, sans requête, et à jour des tendances.
2. Cote du site : historique des prix /api/marketplace/cards/{id}/price-history (onglet « Marché » de la fiche) :
   moyennes par semaine et par rareté, une requête pour toutes les raretés (`history_quotes`). Depuis la mise à jour
   du 20/11 au soir, ?scope=summary ne renvoie plus de moyenne (seulement `recent`, les 5 dernières ventes) : sans ce
   changement, le bot n'avait plus de cote du site, ni plafond pour le sniper ni cote pour les collections.
3. Secours si l'historique est refusé : les 5 dernières ventes de /api/marketplace/cards/{id}/sales (`recent`,
   ouvertes à tous depuis le 20/11, avec leur rareté).

Estimation = médiane observée, plafonnée par la moyenne du site quand elle existe (prudence ; × QUOTE_SITE_MARGIN_RATIO :
la moyenne du site suit une hausse avec retard, voir `combine`), sauf pour un
shiny : la moyenne du site ne les distingue pas (elle vaut ~10 % de leurs ventes), ventes shiny observées seules.
Contre la manipulation des cotes (ventes factices entre comptes, relevé par la presse et JVFlux) :
une vente plus de 3 fois au-dessus ou au-dessous de la médiane est écartée, et les ventes entre
un vendeur et un acheteur qui ont déjà fait affaire ensemble ne comptent pas (core/database.py).
Une vente sans concurrence (une seule mise, au prix de départ : le schéma d'une vente factice, prix
de départ gonflé puis un complice qui mise une fois) ne peut pas faire monter l'estimation.
Le sniper n'achète que si les dernières ventes sont assez disputées et pas trop dispersées (`contested`,
`spread`) : le 18/10, l'éclipse XXIe était estimée 1197 sur 3 ventes au prix de départ et 2 disputées (970 et
1774), et revendue ensuite autour de 850.
`count` = nombre de ventes observées : sans observation, l'estimation n'est pas fiable.
Les ventes observées sont comparées à rareté égale, et shiny à part : un exemplaire Commun ne
s'estime pas sur les ventes de la même carte en UR ou en shiny.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.config import RARITIES
from src.core.api import WikiMastersAPI
from src.core.client import ApiError, RateLimited
from src.core.database import Database
from src.core.models import SaleRow, SalesHistory, deterrent_bid
from src.core.timeutils import utcnow

log = logging.getLogger(__name__)

ANY = "*"
OUTLIER_FACTOR = 3.0
HISTORY_WEEKS = 4  # semaines avec ventes retenues au plus pour la cote de l'historique (les plus récentes)
HISTORY_MIN_SALES = 10  # ventes suffisantes : les semaines plus anciennes ne comptent pas
HISTORY_TTL = timedelta(hours=6)
HISTORY_OFF = timedelta(hours=1)  # route refusée (compte, panne) : plus essayée pendant ce temps
HISTORY_CACHE_MAX = 500
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Quote:
    average: float | None  # estimation de revente (médiane récente ou moyenne)
    count: int | None      # ventes observées (0 = aucune) ; None = inconnu
    contested: int | None = None  # ventes disputées parmi les dernières observées ; None = inconnu
    spread: float | None = None   # plus haute / plus basse des dernières ventes observées ; None = inconnu

    def reliable(self, min_sales: int) -> bool:
        return self.average is not None and (self.count is None or self.count >= min_sales)


def robust_median(prices: list[int]) -> float:
    """Médiane sans les ventes aberrantes ; deux ventes trop différentes : la plus basse, par prudence."""
    if len(prices) >= 3:
        mid = statistics.median(prices)
        kept = [p for p in prices if mid / OUTLIER_FACTOR <= p <= mid * OUTLIER_FACTOR]
        return float(statistics.median(kept or prices))
    if len(prices) == 2 and max(prices) > OUTLIER_FACTOR * min(prices):
        return float(min(prices))
    return float(statistics.median(prices))


CONTESTED_FOR_MEDIAN = 3  # ventes disputées au moins pour que leur médiane plafonne les autres


def trusted_prices(sales: list[tuple[int, bool]]) -> list[int]:
    """Prix retenus : une vente sans concurrence est ramenée au plus à la médiane des ventes disputées (3 au
    moins), à la plus haute d'entre elles (1 ou 2), ou, sans vente disputée, à la plus basse des ventes.
    Le 18/10, l'éclipse XXIe vendue 900, 800 et 999 au prix de départ était ramenée à 584, la médiane de
    deux ventes disputées parties de 500 et 200 : son estimation tombait sous les vraies ventes."""
    if not sales:
        return []
    contested = [price for price, single in sales if not single]
    if len(contested) >= CONTESTED_FOR_MEDIAN:
        ceiling = statistics.median(contested)
    else:
        ceiling = max(contested) if contested else min(price for price, _ in sales)
    return [min(price, ceiling) if single else price for price, single in sales]


def trusted(observed: dict[str, tuple[list[tuple[int, bool]], int]]) -> dict[str, tuple[list[int], int]]:
    return {rarity: (trusted_prices(sales), n) for rarity, (sales, n) in observed.items()}


def steadiness(observed: dict[str, tuple[list[tuple[int, bool]], int]]) -> dict[str, tuple[int, float | None]]:
    """Par rareté : (ventes disputées, plus haute / plus basse) parmi les dernières ventes observées."""
    result = {}
    for rarity, (sales, _) in observed.items():
        prices = [price for price, _ in sales if price > 0]
        result[rarity] = (sum(1 for _, single in sales if not single),
                          max(prices) / min(prices) if prices else None)
    return result


def combine(observed: list[int], n_observed: int, site_average: float | None,
            contested: int | None = None, spread: float | None = None, site_margin: float = 1.0) -> Quote:
    """Médiane des ventes observées, plafonnée par la moyenne du site × `site_margin` ; sinon la moyenne seule.
    La moyenne du site suit les prix avec retard : quand ils montent, elle plafonne trop bas. Du 05 au 10/11 (prix
    des L +10 % en une semaine à carte égale, 40 % des estimations plafonnées), rejeu des ventes L : une marge de
    1,1 donnait 34 % d'achats et 21 % de gain en plus pour autant de pertes (3 %) ; sans plafond, 10 % de pertes
    (Ana de Armas L le 10/11 : vendue 11 000 à 20 000, moyenne du site 7707)."""
    if observed:
        estimate = robust_median(observed)
        if site_average is not None:
            estimate = min(estimate, site_average * site_margin)
        return Quote(estimate, n_observed, contested, spread)
    return Quote(site_average, 0)


def history_quotes(data: dict | None) -> dict[str, Quote]:
    """Cote par rareté tirée de l'historique des prix du site : moyenne des dernières semaines avec des ventes,
    pondérée par leur nombre, en remontant jusqu'à HISTORY_MIN_SALES ventes ou HISTORY_WEEKS semaines. Une carte
    très vendue a donc la moyenne de la semaine, comme le site (Ana de Armas L le 22/11 : 8845 sur 86 ventes, quand
    celle des 4 semaines valait ~8400, tirée vers le bas par la semaine du 26/10 à 6492) ; une carte rare prend plus
    de semaines. `count` : ventes retenues. Format relevé le 22/11 : {computed_at, by_rarity: {"<rareté>": {weekly: [{start, avg, min, max,
    count}], monthly: [...]}}}. Inattendu : aucune cote."""
    quotes: dict[str, Quote] = {}
    by_rarity = data.get("by_rarity") if isinstance(data, dict) else None
    if not isinstance(by_rarity, dict):
        return quotes
    for rarity, series in by_rarity.items():
        weekly = series.get("weekly") if isinstance(series, dict) else None
        if rarity not in RARITIES or not isinstance(weekly, list):
            continue
        points = sorted((p for p in weekly if isinstance(p, dict) and isinstance(p.get("avg"), int | float)
                         and isinstance(p.get("count"), int) and p["count"] > 0 and p["avg"] > 0),
                        key=lambda p: str(p.get("start", "")), reverse=True)
        kept: list[dict] = []
        for point in points:
            kept.append(point)
            if len(kept) >= HISTORY_WEEKS or sum(p["count"] for p in kept) >= HISTORY_MIN_SALES:
                break
        total = sum(p["count"] for p in kept)
        if total:
            quotes[rarity] = Quote(sum(p["avg"] * p["count"] for p in kept) / total, total)
    return quotes


def advised_bid(resale: float, fee: float) -> tuple[int, float]:
    """Mise conseillée pour revendre une carte (`deterrent_bid` : qui surenchérit paie plus que la revente) et bénéfice
    si elle gagne à ce prix (commandes /marche top et /marche prix)."""
    bid = deterrent_bid(resale)
    return bid, resale * (1 - fee) - bid


def recent_estimates(rows: list[SaleRow], recent_n: int) -> dict[str, tuple[float | None, int | None]]:
    """Médiane des `recent_n` dernières ventes, par rareté et toutes raretés confondues."""
    priced = [r for r in rows if r.final_price and r.final_price > 0]
    if any(r.settled_at for r in priced):
        priced.sort(key=lambda r: r.settled_at or _EPOCH, reverse=True)  # plus récentes d'abord
    groups: dict[str, list[int]] = {ANY: [r.final_price for r in priced]}  # type: ignore[misc]
    for r in priced:
        if r.sale_rarity in RARITIES:
            groups.setdefault(r.sale_rarity, []).append(r.final_price)  # type: ignore[arg-type]
    return {k: (float(statistics.median(v[:recent_n])), len(v)) for k, v in groups.items() if v}


class PriceService:
    def __init__(self, api: WikiMastersAPI, db: Database, default_ttl_minutes: float, recent_n: int = 5,
                 site_margin: float = 1.0):
        self.api = api
        self.db = db
        self.default_ttl = timedelta(minutes=default_ttl_minutes)
        self.recent_n = recent_n
        self.site_margin = site_margin  # le plafond de l'estimation est la moyenne du site × cette marge
        self._history: dict[str, tuple[datetime, dict[str, Quote]]] = {}  # historique du site, par carte
        self._history_off_until = _EPOCH
        self._history_format_warned = False  # format inattendu signalé une seule fois

    def _remember_history(self, card_id: str, quotes: dict[str, Quote]) -> None:
        now = utcnow()
        if len(self._history) >= HISTORY_CACHE_MAX:
            self._history = {k: v for k, v in self._history.items() if now - v[0] <= HISTORY_TTL}
        self._history[card_id] = (now, quotes)

    async def _fetch(self, card_id: str, wait: bool,
                     background: bool = False) -> dict[str, tuple[float | None, int | None]]:
        """Cote du site par rareté : historique des prix (une requête, toutes les raretés) ; refusé ou de format
        inattendu : médiane des 5 dernières ventes de la carte."""
        data = None
        try:
            data = await self.api.price_history(card_id, wait=wait, background=background)
        except RateLimited:
            raise
        except ApiError as exc:
            if exc.status not in (401, 403, 404):
                raise
            log.debug("historique des prix refusé pour %s (HTTP %s), repli sur les dernières ventes", card_id,
                      exc.status)
        if isinstance(data, dict) and isinstance(data.get("by_rarity"), dict):
            quotes = history_quotes(data)
            self._remember_history(card_id, quotes)
            return {r: (q.average, q.count) for r, q in quotes.items()}
        if data is not None and not self._history_format_warned:
            self._history_format_warned = True
            log.warning("historique des prix de %s : format inattendu, repli sur les dernières ventes", card_id)
        history = SalesHistory.model_validate(
            await self.api.client.get("sales_history", card_id=card_id, wait=wait, background=background))
        return {r: v for r, v in recent_estimates(history.rows(), self.recent_n).items() if r != ANY}

    async def _market(self, card_id: str, max_age: timedelta, wait: bool, fetch: bool = True,
                      background: bool = False) -> dict[str, tuple[float | None, int | None]]:
        cached = await self.db.price_get(card_id)
        if cached and utcnow() - cached[0] <= max_age:
            return cached[1]
        if not fetch:
            return {}
        try:
            data = await self._fetch(card_id, wait, background)
        except (RateLimited, ApiError):
            # Route refusée : l'ancienne cote du site plafonne encore l'estimation (le sniper restait sans plafond
            # pendant chaque pause de la route, que l'analyse de l'inventaire rend plus fréquentes).
            if cached:
                return cached[1]
            raise
        await self.db.price_put(card_id, data)
        return data

    async def quotes(self, card_id: str, *, max_age: timedelta | None = None, wait: bool = True,
                     shiny: bool = False, fetch: bool = True, background: bool = False) -> dict[str, Quote]:
        """Estimation par rareté (clé ANY : toutes raretés, seulement si le site ne les distingue pas).
        `fetch=False` : sans aucune requête (ventes observées et cote du site en cache) ; `background` : la
        requête passe après toutes les autres."""
        raw = await self.db.observed_by_rarity(card_id, self.recent_n, shiny)
        observed, steady = trusted(raw), steadiness(raw)
        if shiny:
            # La cote du site ne distingue que la rareté : celle d'un shiny est celle de la carte ordinaire. Le 25/10,
            # elle valait en médiane 10 % des ventes shiny (Mark Wahlberg ✨ ~7800, cote 675) et, en cache, faisait
            # refuser toutes les reventes shiny : ventes shiny observées seules, et aucune requête.
            market = {}
        else:
            try:
                market = await self._market(card_id, max_age or self.default_ttl, wait, fetch, background)
            except (RateLimited, ApiError):
                if not observed:
                    raise
                market = {}
        keys = {r for r in market if r in RARITIES} | set(observed)
        quotes = {r: combine(*observed.get(r, ([], 0)), market.get(r, (None, None))[0], *steady.get(r, (None, None)),
                             site_margin=self.site_margin)
                  for r in keys}
        if not quotes and ANY in market:
            quotes[ANY] = combine([], 0, market[ANY][0])
        return quotes

    async def observed_quote(self, card_id: str, shiny: bool = False) -> dict[str, Quote]:
        """Estimation sans aucune requête : ventes observées seules, par rareté (vide si aucune)."""
        return {r: combine(prices, n, None) for r, (prices, n) in
                trusted(await self.db.observed_by_rarity(card_id, self.recent_n, shiny)).items()}

    async def is_fresh(self, card_id: str, max_age: timedelta) -> bool:
        cached = await self.db.price_get(card_id)
        return bool(cached and utcnow() - cached[0] <= max_age)

    async def is_cached(self, card_id: str) -> bool:
        cached = await self.db.price_get(card_id)
        return bool(cached and utcnow() - cached[0] <= self.default_ttl)

    async def history_quote(self, card_id: str, rarity: str | None, *, shiny: bool = False, fetch: bool = True,
                            wait: bool = False) -> Quote | None:
        """Cote de secours d'après l'historique des prix du site (`history_quotes`), pour une carte gardée sans autre
        cote : le 20/11, 27 cartes souhaitées en vente ignorées faute de cote. Jamais pour un shiny (l'historique ne
        les distingue pas). Gardée HISTORY_TTL ; `fetch=False` : sans requête. Route refusée : coupée HISTORY_OFF."""
        if shiny or not rarity:
            return None
        now = utcnow()
        cached = self._history.get(card_id)
        if cached and now - cached[0] <= HISTORY_TTL:
            return cached[1].get(rarity)
        if not fetch or now < self._history_off_until:
            return None
        try:
            data = await self.api.price_history(card_id, wait=wait)
        except RateLimited:
            return None
        except ApiError as exc:
            if exc.status in (401, 403, 404):
                self._history_off_until = now + HISTORY_OFF
                log.info("historique des prix refusé (HTTP %s) : plus essayé pendant %d min", exc.status,
                         HISTORY_OFF.seconds // 60)
            return None
        quotes = history_quotes(data)
        self._remember_history(card_id, quotes)
        log.debug("historique des prix de %s : %s", card_id,
                  ", ".join(f"{r} {q.average:.0f} ({q.count})" for r, q in quotes.items()) or "aucune vente")
        return quotes.get(rarity)

    async def quote(self, card_id: str, rarity: str | None, *, max_age: timedelta | None = None,
                    wait: bool = True, shiny: bool = False, fetch: bool = True) -> Quote:
        """Estimation pour cette rareté (et shiny ou non) ; jamais celle d'une autre rareté."""
        quotes = await self.quotes(card_id, max_age=max_age, wait=wait, shiny=shiny, fetch=fetch)
        return (quotes.get(rarity) if rarity else None) or quotes.get(ANY) or Quote(None, 0)
