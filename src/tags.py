"""Étiquettes du site (commande Discord /profil tags) : les lire, et créer celles que le bot pose et qui manquent.

Table Supabase `tags` : {id, user_id, name, color, …}, créée par l'éditeur du site ou par une extension avec
{user_id, name, color} [WP tags.js]. L'émoji fait partie du nom (« 📅 Jour ») ; le bot compare les noms sans émojis
ni casse (tag_key). Émoji et couleur : config/themes.yaml (`emoji`, `couleur` d'un thème, et section `autres` pour
les étiquettes qui ne sont pas des thèmes), à la création et pour harmoniser les étiquettes existantes (PATCH du nom et de
la couleur, comme l'éditeur du site).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from src.core.text import config_value, tag_key
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
    return str(spec.get("emoji") or "").strip(), str(config_value(spec, "color", "couleur") or "").strip()


def styles() -> dict[str, Wanted]:
    """Style de chaque étiquette du fichier (thèmes et `autres`), par clé de nom (tag_key)."""
    raw = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
    out = {}
    for name, spec in {**(raw.get("themes") or {}), **(config_value(raw, "other_tags", "autres") or {})}.items():
        emoji, color = _style(spec)
        if emoji or color:
            out[tag_key(str(name))] = Wanted(str(name), emoji, color)
    return out


def split_name(name: str) -> tuple[str, str]:
    """(émoji en tête, reste) : « 📅 Jour » -> (« 📅 », « Jour ») ; « L+ » -> (« », « L+ »)."""
    match = re.match(r"^([^\w]*)(.*)$", name.strip())
    return (match[1].strip(), match[2].strip()) if match else ("", name.strip())


@dataclass(frozen=True)
class Restyle:
    id: Any
    old_name: str
    old_color: str
    name: str
    color: str


def restyles(tags: list[dict[str, Any]], known: dict[str, Wanted]) -> list[Restyle]:
    """Étiquettes du site dont le nom ou la couleur diffère du fichier. Sans émoji dans le fichier, celui du site est
    gardé ; avec, le nom devient « émoji + nom du fichier » (« revente » -> « 💰 Revente »)."""
    out = []
    for t in tags:
        name = str(t.get("name") or "")
        style = known.get(tag_key(name))
        if style is None or t.get("id") is None:
            continue
        emoji = style.emoji or split_name(name)[0]
        new_name = f"{emoji} {style.name}" if emoji else style.name
        new_color = style.color or str(t.get("color") or "")
        if new_name != name or new_color.lower() != str(t.get("color") or "").lower():
            out.append(Restyle(t["id"], name, str(t.get("color") or ""), new_name, new_color))
    return out


async def restyle(ctx: Context, change: Restyle) -> tuple[bool, str]:
    """Renomme et recolore une étiquette du site. Une ligne renvoyée prouve le changement (la base filtre sans erreur
    les lignes qu'elle refuse). (fait, problème)."""
    if not ctx.settings.is_live("tag_card"):
        return True, ""
    status, data = await ctx.client.supabase("PATCH", "tags", prefer="return=representation",
                                             params={"id": f"eq.{change.id}", "user_id": f"eq.{ctx.api.user_id}"},
                                             json_body={"name": change.name, "color": change.color})
    if status in (200, 201) and data:
        return True, ""
    return False, f"HTTP {status} : {str(data)[:150] if data else 'aucune ligne modifiée (refusée par le site ?)'}"


def wanted(ctx: Context) -> list[Wanted]:
    """Étiquettes que le bot pose avec les réglages actuels : thèmes (si THEMES_ENABLED), Doublon, GIF, collections,
    vitrines, souhaits, cartes invendues, reventes à la main. Dans l'ordre du fichier, puis des réglages."""
    from src.workers import collection  # import local : collection importe les workers

    s = ctx.settings
    known = styles()
    names: list[str] = []
    if s.themes_enabled:
        rules = load_rules()
        names += [*rules.names, *(t for v in rules.corrections.values() for t in v),
                  s.tag_duplicate, s.tag_gif]
    active = collection.collections(s)[0].values() if s.collection_sets or s.collection_titles else ()
    names += [c.tag for c in active]
    names += [s.tag_showcase, s.tag_wish, s.tag_unsold, s.tag_manual_resale]
    out: dict[str, Wanted] = {}
    for name in (n.strip() for n in names):
        if name and tag_key(name) not in out:
            style = known.get(tag_key(name))
            out[tag_key(name)] = Wanted(name, style.emoji, style.color) if style else Wanted(name)
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
