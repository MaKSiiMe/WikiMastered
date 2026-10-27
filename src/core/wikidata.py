"""Wikidata : nature de l'article de chaque carte (personne, pays, film…), pour les étiquettes de thème.

Par lots de 50 titres : l'API de Wikipédia (fr) donne l'élément Wikidata de chaque titre (redirections suivies), puis
le service SPARQL de Wikidata dit quelles classes cherchées chaque élément atteint :
- natures : « est un » (P31) puis « sous-classe de » (P279) en chaîne, ou l'élément est lui-même une sous-classe
  (« Marteau », « Pizza » sont des classes) ;
- métiers (P106, puis P279 en chaîne) et taxons parents (P171 en chaîne : lion -> … -> Animalia) ;
- et la description en français.

Aucune requête au site du jeu : rien ne compte pour la vérification anti-bot. Requêtes espacées d'au moins
SPACING secondes (le service SPARQL limite le temps de calcul par minute) ; une erreur remonte (ApiError), le lot
est retenté au passage suivant. Essai du 27/10 : 300 cartes du marché en ~2 min, toutes trouvées sur Wikipédia.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from src.core.client import ApiError

WIKIPEDIA_API = "https://fr.wikipedia.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
# Politique des robots de Wikimedia (https://w.wiki/4wJS) : nom, contact (une adresse web suffit) et bibliothèque.
# Sans contact, Wikipédia répond 403 (essai du 27/10).
USER_AGENT = f"WikiMastered/1.0 (https://github.com/MaKSiiMe/WikiMastered) httpx/{httpx.__version__}"
BATCH = 50
SPACING = 1.0


@dataclass
class Facts:
    """Ce que Wikidata dit d'un article ; `qid` None : pas de page Wikipédia ou pas d'élément Wikidata."""

    qid: str | None = None
    natures: set[str] = field(default_factory=set)  # classes cherchées atteintes par P31/P279* ou P279+
    jobs: set[str] = field(default_factory=set)     # métiers cherchés atteints par P106/P279*
    taxa: set[str] = field(default_factory=set)     # taxons cherchés atteints par P171*
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("natures", "jobs", "taxa"):
            data[key] = sorted(data[key])
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Facts:
        return cls(qid=data.get("qid"), natures=set(data.get("natures") or ()), jobs=set(data.get("jobs") or ()),
                   taxa=set(data.get("taxa") or ()), description=str(data.get("description") or ""))


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


class Wikidata:
    def __init__(self, http: httpx.AsyncClient | None = None, spacing: float = SPACING):
        self._http = http or httpx.AsyncClient(timeout=90, headers={"User-Agent": USER_AGENT})
        self._spacing = spacing
        self._last = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        wait = self._last + self._spacing - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, None, url, f"réseau : {type(exc).__name__}") from exc
        finally:
            self._last = time.monotonic()
        if resp.status_code != 200:
            raise ApiError(resp.status_code, None, url, resp.text[:200])
        return resp.json()

    async def _sparql(self, query: str) -> list[dict[str, Any]]:
        data = await self._request("POST", SPARQL, data={"query": query, "format": "json"},
                                   headers={"Accept": "application/sparql-results+json"})
        return data["results"]["bindings"]

    async def qids(self, titles: list[str]) -> dict[str, str | None]:
        """Élément Wikidata de chaque titre de Wikipédia (fr), redirections suivies ; None : introuvable."""
        out: dict[str, str | None] = {}
        for i in range(0, len(titles), BATCH):
            chunk = titles[i:i + BATCH]
            data = await self._request("GET", WIKIPEDIA_API, params={
                "action": "query", "titles": "|".join(chunk), "prop": "pageprops", "ppprop": "wikibase_item",
                "redirects": 1, "format": "json", "formatversion": 2})
            query = data.get("query") or {}
            # « titre demandé -> titre final » : normalisation (majuscule, espaces) puis redirection.
            moves = {m["from"]: m["to"] for key in ("normalized", "redirects") for m in query.get(key) or ()}
            pages = {p.get("title"): (p.get("pageprops") or {}).get("wikibase_item") for p in query.get("pages") or ()}
            for title in chunk:
                final, seen = title, set()
                while final in moves and final not in seen:
                    seen.add(final)
                    final = moves[final]
                out[title] = pages.get(final)
        return out

    async def facts(self, qids: Iterable[str], natures: set[str], jobs: set[str], taxa: set[str]) -> dict[str, Facts]:
        """Faits de chaque élément, pour les classes cherchées seulement."""
        items = sorted(set(qids))
        out = {q: Facts(qid=q) for q in items}
        values = " ".join(f"wd:{t}" for t in sorted(natures | jobs | taxa))
        for i in range(0, len(items), BATCH):
            chunk = " ".join(f"wd:{q}" for q in items[i:i + BATCH])
            rows = await self._sparql(f"""SELECT ?item ?via ?target WHERE {{
  VALUES ?item {{ {chunk} }}
  VALUES ?target {{ {values} }}
  {{ ?item (wdt:P31/wdt:P279*)|wdt:P279+ ?target. BIND("n" AS ?via) }}
  UNION {{ ?item wdt:P106/wdt:P279* ?target. BIND("j" AS ?via) }}
  UNION {{ ?item wdt:P171* ?target. BIND("t" AS ?via) }}
}}""")
            for row in rows:
                facts, target = out[_qid(row["item"]["value"])], _qid(row["target"]["value"])
                via = row["via"]["value"]
                if via == "n" and target in natures:
                    facts.natures.add(target)
                elif via == "j" and target in jobs:
                    facts.jobs.add(target)
                elif via == "t" and target in taxa:
                    facts.taxa.add(target)
            rows = await self._sparql(f"""SELECT ?item ?desc WHERE {{
  VALUES ?item {{ {chunk} }}
  ?item schema:description ?desc FILTER(lang(?desc) = "fr")
}}""")
            for row in rows:
                if "desc" in row:
                    out[_qid(row["item"]["value"])].description = row["desc"]["value"]
        return out

    async def lookup(self, titles: list[str], natures: set[str], jobs: set[str], taxa: set[str]) -> dict[str, Facts]:
        """Faits de chaque titre (Facts() vide : pas d'élément Wikidata)."""
        qids = await self.qids(titles)
        facts = await self.facts((q for q in qids.values() if q), natures, jobs, taxa)
        return {t: facts[q] if q else Facts() for t, q in qids.items()}
