"""Estimation de la valeur de revente d'une carte.

Le prix suit surtout les tendances (actualité, pop culture, mèmes) plus que la rareté, et
la moyenne du site peut être très datée (relevé réel : GMMTV vendue 157 WB, moyenne 391).
Sources, de la plus fiable à la moins fiable :
1. **Ventes observées** par le sniper lui-même (table observed_sales) : médiane des
   PRICE_RECENT_SALES dernières. Gratuit, sans requête, et à jour des tendances.
2. Historique du site /api/marketplace/cards/{id}/sales [WP market.js] — refusé (403)
   aux comptes non PRO, donc rarement disponible.
3. Moyenne ?scope=summary [KZ bridge/prices.js], sans nombre de ventes hors PRO.
4. Historique des prix /api/marketplace/cards/{id}/price-history (relevé du 21/11) : moyennes par semaine et par
   rareté. Seulement en secours, pour une carte gardée (souhaitée, de collection) sans autre cote : voir
   `history_quote`.

Estimation = médiane observée, plafonnée par la moyenne du site quand elle existe (prudence ; × PRICE_SITE_MARGIN :
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
HISTORY_WEEKS = 4  # semaines avec ventes retenues pour la cote de l'historique (les plus récentes)
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


def history_quotes(data: dict) -> dict[str, Quote]:
    """Cote par rareté tirée de l'historique des prix du site : moyenne des HISTORY_WEEKS dernières semaines avec
    des ventes, pondérée par leur nombre (le site affiche la moyenne de la dernière semaine). Format relevé le 21/11
    dans son code : {by_rarity: {"<rareté>": {weekly: [{start, avg, min, max, count}]}}}. Inattendu : aucune cote."""
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
                        key=lambda p: str(p.get("start", "")))[-HISTORY_WEEKS:]
        total = sum(p["count"] for p in points)
        if total:
            quotes[rarity] = Quote(sum(p["avg"] * p["count"] for p in points) / total, total)
    return quotes


def advised_bid(resale: float, fee: float) -> tuple[int, float]:
    """Mise conseillée pour revendre une carte (`deterrent_bid` : qui surenchérit paie plus que la revente) et bénéfice
    si elle gagne à ce prix (commandes /top et /prix)."""
    bid = deterrent_bid(resale)
    return bid, resale * (1 - fee) - bid


def recent_estimates(rows: list[SaleRow], recent_n: int) -> dict[str, tuple[float | None, int | None]]:
    """Médiane des `recent_n` dernières ventes, par rareté et toutes raretés confondues."""
    priced = [r for r in rows if r.final_price and r.final_price > 0]
    if any(r.settled_at for r in priced):
        priced.sort(key=lambda r: r.settled_at or _EPOCH, reverse=True)  # plus récentes d'abord
    groups: dict[str, list[int]] = {ANY: [r.final_price for r in priced]}  # type: ignore[misc]
    for r in priced:
        if r.snapshot_rarity in RARITIES:
            groups.setdefault(r.snapshot_rarity, []).append(r.final_price)  # type: ignore[arg-type]
    return {k: (float(statistics.median(v[:recent_n])), len(v)) for k, v in groups.items() if v}


class PriceService:
    def __init__(self, api: WikiMastersAPI, db: Database, default_ttl_minutes: float, recent_n: int = 5,
                 site_margin: float = 1.0):
        self.api = api
        self.db = db
        self.default_ttl = timedelta(minutes=default_ttl_minutes)
        self.recent_n = recent_n
        self.site_margin = site_margin  # le plafond de l'estimation est la moyenne du site × cette marge
        self.pro = False  # compte PRO, d'après la dernière cote lue (isPro)
        self._history: dict[str, tuple[datetime, dict[str, Quote]]] = {}  # historique du site, par carte
        self._history_off_until = _EPOCH

    async def _fetch(self, card_id: str, wait: bool,
                     background: bool = False) -> dict[str, tuple[float | None, int | None]]:
        # Historique des ventes : réservé aux comptes PRO (403 sinon, relevé du 14/10). Demandé seulement quand la
        # cote du site dit le compte PRO ; avant, chaque 403 coûtait en plus une vérification de session.
        if self.pro and self.api.client.endpoints.has("sales_history"):
            try:
                history = SalesHistory.model_validate(
                    await self.api.client.get("sales_history", card_id=card_id, wait=wait, background=background))
                data = recent_estimates(history.rows(), self.recent_n)
                if data:
                    return data
            except RateLimited:
                raise
            except (ApiError, ValueError) as exc:
                log.debug("historique indisponible pour %s (%s), repli sur la moyenne", card_id, exc)
        summary = await self.api.sales_summary(card_id, wait=wait, background=background)
        self.pro = summary.is_pro
        return {
            r: (s.average, s.count)
            for r, s in summary.summary.items()
            if r in RARITIES and (s.average is None or s.average >= 0)
        }

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
        if len(self._history) >= HISTORY_CACHE_MAX:
            self._history = {k: v for k, v in self._history.items() if now - v[0] <= HISTORY_TTL}
        self._history[card_id] = (now, quotes)
        log.debug("historique des prix de %s : %s", card_id,
                  ", ".join(f"{r} {q.average:.0f} ({q.count})" for r, q in quotes.items()) or "aucune vente")
        return quotes.get(rarity)

    async def quote(self, card_id: str, rarity: str | None, *, max_age: timedelta | None = None,
                    wait: bool = True, shiny: bool = False, fetch: bool = True) -> Quote:
        """Estimation pour cette rareté (et shiny ou non) ; jamais celle d'une autre rareté."""
        quotes = await self.quotes(card_id, max_age=max_age, wait=wait, shiny=shiny, fetch=fetch)
        return (quotes.get(rarity) if rarity else None) or quotes.get(ANY) or Quote(None, 0)
