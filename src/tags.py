"""Étiquettes du site (commande Discord /etiquettes) : les lire, et créer celles que le bot pose et qui manquent.

Table Supabase `tags` : {id, user_id, name, color, …}, créée par l'éditeur du site ou par une extension avec
{user_id, name, color} [WP tags.js]. L'émoji fait partie du nom (« 📅 Jour ») ; le bot compare les noms sans émojis
ni casse (tag_key). Émoji et couleur des étiquettes à créer : config/etiquettes.yaml (`emoji`, `couleur` d'un thème, et
section `autres` pour les étiquettes qui ne sont pas des thèmes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from src.core.text import tag_key
from src.workers.base import Context
from src.workers.themes import RULES_FILE, load_rules

# Couleurs de l'extension [WP tags.js], dans l'ordre, pour une étiquette sans couleur dans le fichier.
PALETTE = ("#34d399", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa", "#f87171", "#2dd4bf", "#fb923c")
KNOWN_FIELDS = {"id", "user_id", "name", "color", "created_at", "updated_at"}


@dataclass(frozen=True)
class Wanted:
    name: str        # sans émoji, comme dans les réglages
    emoji: str = ""
    color: str = ""

    @property
    def label(self) -> str:
        """Nom créé sur le site : « ⚽ Football »."""
        return f"{self.emoji} {self.name}".strip()


def _style(spec: Any) -> tuple[str, str]:
    spec = spec if isinstance(spec, dict) else {}
    return str(spec.get("emoji") or "").strip(), str(spec.get("couleur") or "").strip()


def wanted(ctx: Context) -> list[Wanted]:
    """Étiquettes que le bot pose avec les réglages actuels : thèmes (si ENABLE_THEME_TAGS), Doublon, GIF, collections,
    vitrines, souhaits, cartes invendues, reventes à la main. Dans l'ordre du fichier, puis des réglages."""
    from src.workers import collection  # import local : collection importe les workers

    s = ctx.settings
    raw = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
    styles = {tag_key(str(k)): _style(v) for k, v in {**(raw.get("themes") or {}), **(raw.get("autres") or {})}.items()}
    names: list[str] = []
    if s.enable_theme_tags:
        rules = load_rules()
        names += [*rules.names, *(t for v in rules.corrections.values() for t in v),
                  s.theme_duplicate_tag, s.theme_gif_tag]
    active = collection.collections(s)[0].values() if s.collection_sets or s.collection_titles else ()
    names += [c.tag for c in active]
    names += [s.showcase_tag, s.wish_tag, s.position_unsold_tag, s.manual_resale_tag]
    out: dict[str, Wanted] = {}
    for name in (n.strip() for n in names):
        if name and tag_key(name) not in out:
            emoji, color = styles.get(tag_key(name), ("", ""))
            out[tag_key(name)] = Wanted(name, emoji, color)
    return list(out.values())


async def site_tags(ctx: Context) -> tuple[int, list[dict[str, Any]]]:
    """(statut HTTP, étiquettes du compte avec tous leurs champs)."""
    status, data = await ctx.client.supabase("GET", "tags", params={"select": "*", "user_id": f"eq.{ctx.api.user_id}",
                                                                     "order": "name.asc"})
    return status, [t for t in data or [] if isinstance(t, dict)] if status == 200 else []


def missing(tags: list[dict[str, Any]], want: list[Wanted]) -> list[Wanted]:
    """Étiquettes voulues absentes du site, avec une couleur de la palette quand le fichier n'en donne pas."""
    have = {tag_key(str(t.get("name", ""))) for t in tags}
    todo = [w for w in want if tag_key(w.name) not in have]
    return [w if w.color else Wanted(w.name, w.emoji, PALETTE[i % len(PALETTE)]) for i, w in enumerate(todo)]


async def create(ctx: Context, tag: Wanted) -> tuple[bool, str]:
    """Crée une étiquette sur le site, comme l'extension [WP tags.js]. (créée, problème)."""
    if not ctx.settings.is_live("tag_card"):
        return True, ""
    status, data = await ctx.client.supabase("POST", "tags", prefer="return=minimal",
                                             json_body={"user_id": ctx.api.user_id, "name": tag.label,
                                                        "color": tag.color})
    return (True, "") if status in (200, 201, 204) else (False, f"HTTP {status} : {str(data)[:150]}")


def overview(tags: list[dict[str, Any]]) -> str:
    """Une ligne par étiquette : nom, couleur, puis les champs inconnus du bot (icône…)."""
    lines = []
    for t in tags:
        extra = ", ".join(f"{k}={v}" for k, v in t.items() if k not in KNOWN_FIELDS and v not in (None, ""))
        lines.append(f"• {t.get('name')} · `{t.get('color') or 'sans couleur'}`" + (f" · {extra}" if extra else ""))
    return "\n".join(lines) or "Aucune étiquette."
