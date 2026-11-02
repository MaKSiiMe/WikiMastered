"""Liste des articles dont l'image principale est un GIF animé (config/gifs_animes.txt, collection « gifs »).

    python -m src.tools.gif_list

Lit l'image principale de chaque page dans l'export `page_props` de Wikipédia (102 Mo le 02/11, republié environ deux
fois par mois), garde les fichiers .gif, puis demande à Wikipédia leur nombre d'images : 10 273 articles ont un .gif,
1 595 sont vraiment animés. Aucune requête au site du jeu. À lancer à la main, puis à livrer le fichier par une PR.
"""

from __future__ import annotations

import asyncio
import gzip
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

import httpx

from src.core.wikidata import USER_AGENT, Wikidata

DUMP = "https://dumps.wikimedia.org/frwiki/latest/frwiki-latest-page_props.sql.gz"
OUTPUT = Path(__file__).resolve().parents[2] / "config" / "gifs_animes.txt"
ENTRY = re.compile(r"\((\d+),'page_image(?:_free)?','((?:[^'\\]|\\.)*)'")


def lead_gifs(lines: Iterable[str]) -> dict[int, str]:
    """Page -> fichier .gif de son image principale, d'après les lignes SQL de l'export."""
    out: dict[int, str] = {}
    for line in lines:
        for page, name in ENTRY.findall(line):
            if name.lower().endswith(".gif"):
                out[int(page)] = re.sub(r"\\(.)", r"\1", name)
    return out


async def animated_articles(pages: dict[int, str], wikidata: Wikidata) -> list[str]:
    """Titres triés des articles dont le fichier a plus d'une image."""
    frames = await wikidata.gif_frames(sorted(set(pages.values())))
    moving = [page for page, file in pages.items() if (frames.get(file) or 0) > 1]
    return sorted((await wikidata.article_titles(moving)).values())


async def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sql.gz") as dump:
        async with httpx.AsyncClient(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http, \
                http.stream("GET", DUMP) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                dump.write(chunk)
        dump.flush()
        with gzip.open(dump.name, "rt", encoding="utf-8", errors="replace") as lines:
            pages = lead_gifs(lines)
    print(f"{len(pages)} pages avec un .gif comme image principale")
    wikidata = Wikidata()
    try:
        titles = await animated_articles(pages, wikidata)
    finally:
        await wikidata.close()
    OUTPUT.write_text("\n".join(titles) + "\n", encoding="utf-8")
    print(f"{len(titles)} articles animés écrits dans {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
