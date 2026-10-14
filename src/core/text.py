"""Normalisation des titres pour rapprocher articles Wikipédia et cartes WikiMasters."""

from __future__ import annotations

import unicodedata


def normalize_title(title: str) -> str:
    """`Élection_présidentielle` et `election presidentielle` donnent la même clé."""
    text = unicodedata.normalize("NFKD", title.replace("_", " "))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())
