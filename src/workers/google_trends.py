"""Module F : recherches en forte hausse sur Google en France (flux RSS public de Google Trends).

https://trends.google.com/trending/rss?geo=FR (relevé du 14/10/2026) : les 10 dernières
recherches en hausse, quelques minutes d'écart entre elles, avec un trafic approximatif
(`ht:approx_traffic`, ex. « 1000+ »). Le flux couvre moins d'une heure : il est lu toutes
les 20 min. Avantage sur Wikipédia : le jour même, au lieu de la veille.

Chaque nouveau terme est cherché au marché. Les cartes trouvées deviennent des signaux
(mesurés en fin de semaine) ; celles sous le budget d'une position sont signalées sur Discord.
"""

from __future__ import annotations

import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from src.core.notifier import Topic
from src.core.timeutils import utcnow
from src.workers.base import Worker
from src.workers.trend_common import flush, position_budget, scan_market

SOURCE = "google"
HT = "{https://trends.google.com/trending/rss}"


@dataclass(frozen=True)
class SearchTrend:
    term: str
    traffic: int | None


def parse_feed(xml: str) -> list[SearchTrend]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"flux Google Trends illisible : {exc}") from exc
    out: list[SearchTrend] = []
    for item in root.iter("item"):
        term = (item.findtext("title") or "").strip()
        if not term:
            continue
        digits = re.sub(r"\D", "", item.findtext(f"{HT}approx_traffic") or "")
        out.append(SearchTrend(term, int(digits) if digits else None))
    return out


class GoogleTrends(Worker):
    name = "gtrends"
    topic = Topic.TRENDS

    async def setup(self) -> None:
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers={"User-Agent": self.settings.wm_user_agent,
                                               "Accept": "application/rss+xml, application/xml"})

    async def step(self) -> float:
        s = self.settings
        resp = await self.http.get(s.google_trends_url.format(geo=s.google_trends_geo))
        resp.raise_for_status()
        trends = [t for t in parse_feed(resp.text) if (t.traffic or 0) >= s.google_trends_min_traffic]
        now = utcnow()
        fresh = [t for t in trends if await self.db.term_is_new(SOURCE, t.term, t.traffic, now)]
        lines: list[str] = []
        if fresh:
            max_price = await position_budget(self.ctx)
            for t in fresh:
                traffic = f"{t.traffic}+ recherches" if t.traffic else "en hausse"
                lines += await scan_market(self.ctx, source=SOURCE, term=t.term, max_price=max_price,
                                           detail=f"Google : « {t.term} », {traffic}")
        self.log.info("Google Trends : %d terme(s), %d nouveau(x), %d carte(s) signalée(s)",
                      len(trends), len(fresh), len(lines))
        flush(self.ctx, "Tendances Google", lines)
        return s.google_trends_interval_minutes * 60 * random.uniform(0.85, 1.15)
