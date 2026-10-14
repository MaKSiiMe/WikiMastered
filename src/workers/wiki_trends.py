"""Module E : radar « news front-running » via l'API publique Wikimedia Pageviews.

1. Top des articles fr.wikipedia de la veille (les données du jour ne sont publiées
   qu'avec ~1 jour de retard) : /metrics/pageviews/top/fr.wikipedia/all-access/AAAA/MM/JJ
2. Pic = vues de la veille ≥ TRENDS_SPIKE_RATIO × médiane des 30 jours précédents
   (/metrics/pageviews/per-article/..., mis en cache SQLite).
3. Projection des vues sur 30 jours -> rareté probable (TRENDS_RARITY_THRESHOLDS,
   seul le palier Légendaire = 20 000 vues est connu).
4. Recherche de la carte au marché ; alerte si elle est encore vendue sous sa rareté
   probable et à petit prix.

⚠️ Les exemplaires possédés gardent une `snapshot_rarity` [WR content.js] : une carte
achetée Commune pourrait le rester même si le catalogue la passe Légendaire. Vérifie ce
point sur le site avant d'activer TRENDS_AUTO_BUY.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from src.config import RARITY_RANK, Settings
from src.core.database import utcnow
from src.core.text import normalize_title
from src.workers.base import Worker

WIKIMEDIA = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
PROJECT = "fr.wikipedia"
# Espaces de noms et pages techniques à ignorer.
EXCLUDED_PREFIXES = (
    "Spécial:", "Special:", "Wikipédia:", "Fichier:", "Portail:", "Catégorie:", "Modèle:", "Aide:",
    "Projet:", "Utilisateur:", "Discussion",
)
EXCLUDED_TITLES = {"Wikipédia:Accueil_principal", "Main_Page", "-"}


@dataclass(frozen=True)
class Trend:
    article: str
    views: int
    baseline: float
    ratio: float
    projected_30d: int

    @property
    def title(self) -> str:
        return self.article.replace("_", " ")


def expected_rarity(projected_views: int, thresholds: dict[str, int]) -> str | None:
    best = None
    for rarity, minimum in thresholds.items():
        if projected_views >= minimum and (best is None or RARITY_RANK[rarity] > RARITY_RANK[best]):
            best = rarity
    return best


def detect_spike(article: str, yesterday_views: int, history: list[int], s: Settings) -> Trend | None:
    if yesterday_views < s.trends_min_daily_views:
        return None
    baseline = statistics.median(history) if history else 0.0
    ratio = yesterday_views / max(baseline, 1.0)
    if ratio < s.trends_spike_ratio:
        return None
    # Projection prudente : 30 derniers jours glissants, la veille comprise.
    projected = int(sum(history[-29:]) + yesterday_views)
    return Trend(article, yesterday_views, baseline, ratio, projected)


class WikiTrends(Worker):
    name = "trends"

    async def setup(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": self.settings.wikimedia_user_agent, "Accept": "application/json"},
        )

    async def step(self) -> float:
        trends = await self.find_trends()
        self.log.info("%d article(s) en forte hausse", len(trends))
        for trend in trends[: self.settings.trends_search_limit]:
            await self.check_market(trend)
        await self.db.prune_auctions(utcnow() - timedelta(days=1))
        return self.settings.trends_interval_minutes * 60

    async def _wm_get(self, url: str) -> dict | None:
        resp = await self.http.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def _top(self) -> tuple[date, list[dict]]:
        for back in (1, 2):
            day = utcnow().date() - timedelta(days=back)
            data = await self._wm_get(f"{WIKIMEDIA}/top/{PROJECT}/all-access/{day:%Y/%m/%d}")
            if data and data.get("items"):
                return day, data["items"][0].get("articles", [])
        return utcnow().date(), []

    async def _history(self, article: str, end: date) -> list[int]:
        """Vues quotidiennes des 30 jours précédant `end` (exclu), avec cache SQLite."""
        days = [end - timedelta(days=i) for i in range(30, 0, -1)]
        keys = [f"{d:%Y%m%d}" for d in days]
        cached = await self.db.pageviews_get(article, keys)
        if len(cached) < len(keys):
            url = (f"{WIKIMEDIA}/per-article/{PROJECT}/all-access/user/{quote(article, safe='')}"
                   f"/daily/{keys[0]}00/{keys[-1]}00")
            data = await self._wm_get(url) or {}
            fetched = {str(item["timestamp"])[:8]: int(item["views"]) for item in data.get("items", [])}
            # Un jour absent de la réponse = 0 vue ; on le mémorise aussi.
            fetched = {k: fetched.get(k, 0) for k in keys}
            await self.db.pageviews_put(article, fetched)
            cached = fetched
        return [cached.get(k, 0) for k in keys]

    async def find_trends(self) -> list[Trend]:
        day, articles = await self._top()
        trends: list[Trend] = []
        for item in articles[: self.settings.trends_top_n]:
            article = str(item.get("article", ""))
            if not article or article in EXCLUDED_TITLES or article.startswith(EXCLUDED_PREFIXES):
                continue
            views = int(item.get("views", 0))
            if views < self.settings.trends_min_daily_views:
                break  # le top est trié par vues décroissantes
            trend = detect_spike(article, views, await self._history(article, day), self.settings)
            if trend:
                trends.append(trend)
        trends.sort(key=lambda t: t.ratio, reverse=True)
        return trends

    async def check_market(self, trend: Trend) -> None:
        s = self.settings
        target = normalize_title(trend.title)
        expected = expected_rarity(trend.projected_30d, s.trends_rarity_thresholds)
        auctions, _ = await self.api.market_page(sort="ending_soon", q=trend.title)
        now = utcnow()
        me = self.api.user_id
        for a in auctions:
            if normalize_title(a.title) != target or a.status != "active" or a.seconds_left(now) <= 0:
                continue
            if me and (a.seller_id or "").lower() == me:
                continue
            undervalued = expected is not None and a.rarity_rank < RARITY_RANK[expected]
            if not undervalued or a.min_bid > s.trends_max_price or await self.db.trend_alerted(a.id):
                continue
            await self.db.mark_trend_alert(a.id, trend.article)
            fields = dict(vues_hier=f"{trend.views:,}".replace(",", " "), pic=f"x{trend.ratio:.1f}",
                          projection_30j=f"{trend.projected_30d:,}".replace(",", " "),
                          rareté_actuelle=a.rarity, rareté_probable=expected, mise_min=a.min_bid,
                          fin=f"{a.end_at:%d/%m %H:%M} UTC")
            self.notifier.critical(f"Tendance : {trend.title}", "Carte en vente sous sa rareté probable.", **fields)
            if s.trends_auto_buy and a.min_bid <= s.trends_auto_buy_max:
                result = await self.api.place_bid(a.id, a.min_bid)
                await self.db.record_bid(
                    auction_id=a.id, card_id=a.catalog_id, title=a.title, rarity=a.rarity, is_shiny=a.shiny,
                    amount=a.min_bid, reference_price=None, reason=f"tendance x{trend.ratio:.1f}", night=False,
                    dry_run=result.dry_run, status="simulated" if result.dry_run else ("placed" if result.ok else "rejected"),
                    end_at=a.end_at, note=str(result.payload)[:300] if not result.ok else "",
                )
