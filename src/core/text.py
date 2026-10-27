"""Normalisation des titres pour rapprocher articles Wikipédia et cartes WikiMasters."""

from __future__ import annotations

import unicodedata


def normalize_title(title: str) -> str:
    """`Élection_présidentielle` et `election presidentielle` donnent la même clé."""
    text = unicodedata.normalize("NFKD", title.replace("_", " "))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def tag_key(name: str) -> str:
    """Nom d'étiquette comparable : « 🎵 Musique », « #musique » et « Musique » donnent la même clé (émojis, dièse,
    espaces et casse ignorés ; accents gardés : « Astéroïde »)."""
    text = unicodedata.normalize("NFC", name)
    # So/Sk : émojis et symboles ; Cf/Mn : liants (ZWJ) et sélecteurs de variante (U+FE0F). Après NFC, les
    # accents sont des lettres composées (Ll), pas des Mn.
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Sk", "Cf", "Mn"))
    return " ".join(text.replace("#", " ").casefold().split())
