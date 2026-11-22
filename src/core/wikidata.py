"""Wikidata : nature de l'article de chaque carte (personne, pays, film…), pour les étiquettes de thème.

Deux lectures, toutes deux de simples recherches de liens directs (le service SPARQL limite le temps de calcul) :
- chaque carte : l'API de Wikipédia (fr) donne l'élément Wikidata de son titre (redirections suivies), puis SPARQL ses
  liens directs « est un » (P31), « sous-classe de » (P279), « métier » (P106), « taxon parent » (P171) et sa
  description en français (`Claims`), gardés en base tels quels ;
- les classes et taxons atteints : leurs parents (P279, P171), lus une fois et gardés en base (`Hierarchy`), car
  partagés par des milliers de cartes.
Les classes atteintes par chaque carte se calculent ensuite en local (`Hierarchy.facts`) : changer les règles de
config/themes.yaml ne demande aucune nouvelle lecture.

Jusqu'au 22/11, une seule requête cherchait les chemins (P31/P279*…) de 50 cartes vers toutes les classes des
règles : ~40 s par lot avec les ~250 classes des sous-thèmes, au-delà de 60 s pour certains lots (réponse coupée en
plein JSON, puis 429), et chaque changement de règles faisait relire les 18 000 cartes de la collection.

Aucune requête au site du jeu : rien ne compte pour la vérification anti-bot. Requêtes espacées d'au moins
SPACING secondes ; une erreur remonte (ApiError), la lecture reprend au passage suivant.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.core.client import ApiError

WIKIPEDIA_API = "https://fr.wikipedia.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
# Politique des robots de Wikimedia (https://w.wiki/4wJS) : nom, contact (une adresse web suffit) et bibliothèque.
# Sans contact, Wikipédia répond 403 (essai du 27/10).
USER_AGENT = f"WikiMastered/1.0 (https://github.com/MaKSiiMe/WikiMastered) httpx/{httpx.__version__}"
BATCH = 50          # titres par requête à Wikipédia (limite de l'API)
ITEMS_BATCH = 100   # éléments par requête SPARQL (liens directs : moins d'une seconde quand le service va bien)
PARENTS_BATCH = 200
SPACING = 1.0
ENTITY = "http://www.wikidata.org/entity/"
SUBCLASS, TAXON = "P279", "P171"
CLAIMS = {"P31": "instance_of", "P279": "subclass_of", "P106": "occupations", "P171": "parent_taxa"}


@dataclass
class Facts:
    """Classes cherchées que l'article atteint ; `qid` None : pas de page Wikipédia ou pas d'élément Wikidata."""

    qid: str | None = None
    natures: set[str] = field(default_factory=set)  # classes cherchées atteintes par P31/P279* ou P279+
    jobs: set[str] = field(default_factory=set)     # métiers cherchés atteints par P106/P279*
    taxa: set[str] = field(default_factory=set)     # taxons cherchés atteints par P171* (l'élément compris)
    description: str = ""


@dataclass
class Claims:
    """Liens directs d'un élément, sans rapport avec les règles."""

    qid: str | None = None
    instance_of: set[str] = field(default_factory=set)
    subclass_of: set[str] = field(default_factory=set)
    occupations: set[str] = field(default_factory=set)
    parent_taxa: set[str] = field(default_factory=set)
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"qid": self.qid, **{name: sorted(getattr(self, name)) for name in CLAIMS.values()},
                "description": self.description}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Claims:
        return cls(data.get("qid"), *(set(data.get(name) or ()) for name in CLAIMS.values()),
                   description=str(data.get("description") or ""))

    @property
    def classes(self) -> set[str]:
        """Classes dont il faut connaître les parents (P279) pour classer l'élément."""
        return self.instance_of | self.subclass_of | self.occupations


class Hierarchy:
    """Parents de chaque classe (P279) et de chaque taxon (P171) déjà lus : propriété -> élément -> parents."""

    def __init__(self, parents: dict[str, dict[str, list[str]]] | None = None):
        self.parents: dict[str, dict[str, list[str]]] = {SUBCLASS: {}, TAXON: {}}
        for prop, known in (parents or {}).items():
            self.parents.setdefault(prop, {}).update(known)
        # Beaucoup de cartes partent des mêmes classes (Q5 : être humain) : chemin calculé une fois.
        self._cache: dict[tuple[str, frozenset[str]], tuple[frozenset[str], frozenset[str]]] = {}

    def add(self, prop: str, parents: dict[str, list[str]]) -> None:
        self.parents.setdefault(prop, {}).update(parents)
        self._cache.clear()

    def _walk(self, starts: Iterable[str], prop: str) -> tuple[frozenset[str], frozenset[str]]:
        """(éléments atteints, départs compris ; atteints dont les parents ne sont pas encore lus)."""
        key = (prop, frozenset(starts))
        if key in self._cache:
            return self._cache[key]
        known = self.parents.get(prop, {})
        seen: set[str] = set()
        unknown: set[str] = set()
        stack = list(key[1])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in known:
                stack += known[node]
            else:
                unknown.add(node)
        self._cache[key] = out = (frozenset(seen), frozenset(unknown))
        return out

    def ancestors(self, starts: Iterable[str], prop: str) -> frozenset[str]:
        return self._walk(starts, prop)[0]

    def missing(self, starts: Iterable[str], prop: str) -> frozenset[str]:
        return self._walk(starts, prop)[1]

    def complete(self, claims: Claims) -> bool:
        """Tous les parents utiles à cet élément sont lus : ses classes atteintes sont sûres."""
        return not self.missing(claims.classes, SUBCLASS) and not self.missing(claims.parent_taxa, TAXON)

    def facts(self, claims: Claims, natures: set[str], jobs: set[str], taxa: set[str]) -> Facts:
        """Classes cherchées atteintes, comme le chemin SPARQL (P31/P279*)|P279+, P106/P279* et P171*."""
        reached = self.ancestors(claims.instance_of | claims.subclass_of, SUBCLASS)
        lineage = self.ancestors(claims.parent_taxa, TAXON) | ({claims.qid} if claims.qid else set())
        return Facts(claims.qid, set(reached & natures), set(self.ancestors(claims.occupations, SUBCLASS) & jobs),
                     set(lineage & taxa), claims.description)


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _values(ids: Iterable[str]) -> str:
    return " ".join(f"wd:{q}" for q in ids)


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
        try:
            return resp.json()
        except ValueError as exc:
            # Requête trop longue : le service SPARQL coupe la réponse en plein JSON (22/11, « Unterminated
            # string », « Invalid control character »), avec un statut 200.
            raise ApiError(resp.status_code, None, url, f"réponse coupée ({len(resp.content)} octets)") from exc

    async def _sparql(self, query: str) -> list[dict[str, Any]]:
        data = await self._request("POST", SPARQL, data={"query": query, "format": "json"},
                                   headers={"Accept": "application/sparql-results+json"})
        try:
            return data["results"]["bindings"]
        except (KeyError, TypeError) as exc:
            raise ApiError(200, None, SPARQL, "réponse sans résultats") from exc

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

    async def claims(self, qids: Iterable[str]) -> dict[str, Claims]:
        """Liens directs et description de chaque élément."""
        items = sorted(set(qids))
        out = {q: Claims(qid=q) for q in items}
        props = " ".join(f"wdt:{p}" for p in CLAIMS)
        for i in range(0, len(items), ITEMS_BATCH):
            rows = await self._sparql(f"""SELECT ?item ?p ?o WHERE {{
  VALUES ?item {{ {_values(items[i:i + ITEMS_BATCH])} }}
  {{ VALUES ?p {{ {props} }} ?item ?p ?o. FILTER(STRSTARTS(STR(?o), "{ENTITY}Q")) }}
  UNION {{ ?item schema:description ?o. FILTER(lang(?o) = "fr") BIND("description" AS ?p) }}
}}""")
            for row in rows:
                claims = out.get(_qid(row["item"]["value"]))
                prop, value = _qid(row["p"]["value"]), row["o"]["value"]
                if claims is None:
                    continue
                if prop == "description":
                    claims.description = value
                elif prop in CLAIMS:
                    getattr(claims, CLAIMS[prop]).add(_qid(value))
        return out

    async def parents(self, ids: Iterable[str], prop: str) -> dict[str, list[str]]:
        """Parents directs (P279 ou P171) de chaque élément ; [] : aucun."""
        items = sorted(set(ids))
        out: dict[str, set[str]] = {q: set() for q in items}
        for i in range(0, len(items), PARENTS_BATCH):
            rows = await self._sparql(f"""SELECT ?item ?parent WHERE {{
  VALUES ?item {{ {_values(items[i:i + PARENTS_BATCH])} }}
  ?item wdt:{prop} ?parent. FILTER(STRSTARTS(STR(?parent), "{ENTITY}Q"))
}}""")
            for row in rows:
                if (item := _qid(row["item"]["value"])) in out:
                    out[item].add(_qid(row["parent"]["value"]))
        return {q: sorted(p) for q, p in out.items()}

    async def lookup(self, titles: list[str]) -> dict[str, Claims]:
        """Liens directs de chaque titre (Claims() vide : pas d'élément Wikidata)."""
        qids = await self.qids(titles)
        claims = await self.claims(q for q in qids.values() if q)
        return {t: claims[q] if q else Claims() for t, q in qids.items()}

    async def fill(self, hierarchy: Hierarchy, claims: Iterable[Claims],
                   on_read: Callable[[str, dict[str, list[str]]], Awaitable[None]] | None = None) -> int:
        """Lit les parents qui manquent pour classer ces éléments, niveau par niveau ; `on_read(prop, parents)` est
        appelé après chaque requête, pour garder la progression même si la suivante échoue. Renvoie le nombre
        d'éléments lus."""
        claims = list(claims)
        read = 0
        for prop, starts in ((SUBCLASS, set().union(*(c.classes for c in claims))),
                             (TAXON, set().union(*(c.parent_taxa for c in claims)))):
            while todo := sorted(hierarchy.missing(starts, prop)):
                for i in range(0, len(todo), PARENTS_BATCH):
                    found = await self.parents(todo[i:i + PARENTS_BATCH], prop)
                    hierarchy.add(prop, found)
                    read += len(found)
                    if on_read is not None:
                        await on_read(prop, found)
        return read

    async def gif_frames(self, files: list[str]) -> dict[str, int | None]:
        """Nombre d'images de chaque fichier GIF (1 : image fixe) ; None : fichier ou nombre introuvable."""
        out: dict[str, int | None] = {}
        for i in range(0, len(files), BATCH):
            chunk = files[i:i + BATCH]
            moves: dict[str, str] = {}
            frames: dict[str, int] = {}
            more: dict[str, Any] = {}
            while True:  # métadonnées trop longues pour une réponse : le site donne la suite dans « continue »
                data = await self._request("GET", WIKIPEDIA_API, params={
                    "action": "query", "titles": "|".join(f"File:{f}" for f in chunk), "prop": "imageinfo",
                    "iiprop": "metadata", "format": "json", "formatversion": 2, **more})
                query = data.get("query") or {}
                moves.update({m["from"]: m["to"] for m in query.get("normalized") or ()})
                for page in query.get("pages") or ():
                    for info in (page.get("imageinfo") or ())[:1]:
                        meta = {m["name"]: m["value"] for m in info.get("metadata") or ()}
                        if "frameCount" in meta:
                            frames[page["title"]] = int(meta["frameCount"])
                more = data.get("continue") or {}
                if not more:
                    break
            for file in chunk:
                title = f"File:{file}"
                out[file] = frames.get(moves.get(title, title))
        return out

    async def article_titles(self, page_ids: list[int]) -> dict[int, str]:
        """Titre de chaque page qui est un article ; les autres pages et celles supprimées sont absentes."""
        out: dict[int, str] = {}
        for i in range(0, len(page_ids), BATCH):
            data = await self._request("GET", WIKIPEDIA_API, params={
                "action": "query", "pageids": "|".join(map(str, page_ids[i:i + BATCH])), "format": "json",
                "formatversion": 2})
            out.update({p["pageid"]: p["title"] for p in (data.get("query") or {}).get("pages") or ()
                        if p.get("ns") == 0 and not p.get("missing")})
        return out
