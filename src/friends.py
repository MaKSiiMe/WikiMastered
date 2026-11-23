"""Cartes de ta liste de souhaits que possèdent tes amis : à toi de leur proposer un échange sur le site.

Lecture seule, en requêtes de fond : /collection amis à la demande, et le worker « friends » (FRIENDS_CHECK_HOURS) qui
prévient sur Discord quand un ami possède une carte de ta liste qu'il n'avait pas au passage précédent. Le bot n'a aucune
route pour proposer un échange : rien n'est envoyé aux amis.

Le catalogue du site (`/api/cards?wishlist=1`) donne, pour chaque carte de ta liste, les amis qui la possèdent et dans
quelles raretés (`friendOwners`) : ~30 lectures quel que soit le nombre d'amis, au lieu d'une collection par ami. Une
carte compte chez un ami s'il l'a dans une rareté qui t'avance : n'importe laquelle pour une carte de collection
manquante, au-dessus de ton meilleur exemplaire pour une carte souhaitée (worker « wishes ») ou un favori à monter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.config import RARITIES, RARITY_RANK
from src.core.client import ApiError
from src.core.timeutils import utcnow
from src.roi import plural
from src.trades import Priced, card_value
from src.workers import favorites, wishes
from src.workers.base import Context
from src.workers.collection import KV_KEY as COLLECTION_KV
from src.workers.collection import collection_key

KV_KEY = "friends_wishlist"
FRESH = timedelta(hours=1)  # /collection amis puis /collection amis pseudo : pas de nouvelle lecture du catalogue
PAGE_SIZE = 50
MAX_PAGES = 200
FRIENDS_SHOWN = 15
TITLES_SHOWN = 8


@dataclass(frozen=True)
class Needs:
    """Ce qui t'avance pour chaque carte de ta liste : `missing` (clés de titre) en toute rareté, `above` (id de carte ->
    ton meilleur rang) au-dessus de ce rang. Carte inconnue des deux (ajoutée depuis le dernier passage) : toute rareté."""

    missing: frozenset[str] = frozenset()
    above: dict[str, int] | None = None

    def useful(self, card_id: str, title: str, rarities: list[str]) -> list[str] | None:
        """Raretés de l'ami qui t'avancent, de la plus haute à la plus basse ; [] : le site ne dit pas lesquelles (gardée
        quand même) ; None : aucune ne t'avance."""
        floor = -1 if collection_key(title) in self.missing else (self.above or {}).get(card_id, -1)
        names = {str(r).upper() for r in rarities if r}
        if not names:
            return []
        # Rareté inconnue du bot (nouvelle sur le site) : gardée, au-dessus de toutes.
        kept = sorted((r for r in names if RARITY_RANK.get(r, len(RARITIES)) > floor),
                      key=lambda r: -RARITY_RANK.get(r, len(RARITIES)))
        return kept or None


async def needs(ctx: Context) -> Needs:
    missing = frozenset((await ctx.db.kv_get(COLLECTION_KV) or {}).get("missing") or [])
    above: dict[str, int] = {}
    for key in (wishes.KV_KEY, favorites.KV_KEY):
        for cid, card in ((await ctx.db.kv_get(key) or {}).get("cards") or {}).items():
            best = int(card.get("best", -1))
            above[cid] = min(above.get(cid, best), best)
    return Needs(missing, above)


async def overview(ctx: Context, refresh: bool = False, now: datetime | None = None) -> dict:
    """{"at", "cards" : cartes de ta liste lues, "friends" : {pseudo : [{id, title, rarities}]}}, gardé une heure."""
    now = now or utcnow()
    state = await ctx.db.kv_get(KV_KEY)
    if state and not refresh and now - datetime.fromisoformat(state["at"]) < FRESH:
        return state
    need = await needs(ctx)
    friends: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        data = await ctx.api.wishlist_catalog_page(page)
        cards = data.get("cards")
        owners = data.get("friendOwners")
        if not isinstance(cards, list) or not isinstance(owners, dict):
            raise ApiError(200, None, "wishlist_catalog", "réponse sans cartes ni amis")
        fresh = [c for c in cards if isinstance(c, dict) and c.get("id") not in seen]
        seen.update(str(c.get("id")) for c in fresh)
        for card in fresh:
            cid, title = str(card.get("id")), str(card.get("wikipedia_title") or "")
            for owner in owners.get(cid) or []:
                if not isinstance(owner, dict) or not owner.get("username"):
                    continue
                rarities = need.useful(cid, title, owner.get("rarities") or [])
                if rarities is not None:
                    friends.setdefault(str(owner["username"]), []).append(
                        {"id": cid, "title": title, "rarities": rarities})
        if len(cards) < PAGE_SIZE or not fresh:
            break
    for cards in friends.values():
        cards.sort(key=lambda c: (-max((RARITY_RANK.get(r, len(RARITIES)) for r in c["rarities"]), default=-1),
                                  c["title"].casefold()))
    state = {"at": now.isoformat(), "cards": len(seen),
             "friends": dict(sorted(friends.items(), key=lambda kv: (-len(kv[1]), kv[0].casefold())))}
    await ctx.db.kv_set(KV_KEY, state)
    return state


def _card(card: dict) -> str:
    return card["title"] + (f" ({', '.join(card['rarities'])})" if card["rarities"] else "")


def friends_lines(friends: dict[str, list[dict]]) -> list[str]:
    lines = []
    for name, cards in list(friends.items())[:FRIENDS_SHOWN]:
        more = "…" if len(cards) > TITLES_SHOWN else ""
        lines.append(f"**{name}** ({len(cards)}) : " + ", ".join(_card(c) for c in cards[:TITLES_SHOWN]) + more)
    if len(friends) > FRIENDS_SHOWN:
        lines.append(f"… et {len(friends) - FRIENDS_SHOWN} autres amis")
    return lines


def overview_text(state: dict) -> str:
    friends = state["friends"]
    if not friends:
        return "Aucun ami ne possède une carte de ta liste de souhaits dans une rareté qui t'avance, pour l'instant."
    found = {card["id"] for cards in friends.values() for card in cards}
    return "\n".join([f"**{plural(len(found), 'carte')}** de ta liste chez **{plural(len(friends), 'ami')}**, "
                      f"sur {state['cards']} souhaitées.\n", *friends_lines(friends)])


def pairs(state: dict) -> set[str]:
    """Une clé par carte chez un ami : « pseudo|id de carte »."""
    return {f"{name}|{card['id']}" for name, cards in state["friends"].items() for card in cards}


def news_text(state: dict, keys: set[str]) -> str:
    """Le message du worker « friends » : seulement les cartes de `keys`, par ami."""
    friends = {name: kept for name, cards in state["friends"].items()
               if (kept := [c for c in cards if f"{name}|{c['id']}" in keys])}
    found = {card["id"] for cards in friends.values() for card in cards}
    return "\n".join([f"{plural(len(found), 'carte')} de ta liste chez {plural(len(friends), 'ami')} : propose-leur un "
                      "échange sur le site.\n", *friends_lines(friends),
                      "\n/collection amis pseudo : valeur, doublons et offre équilibrée."])


@dataclass(frozen=True)
class FriendCard:
    priced: Priced
    copies: int
    starred: bool
    pending: bool

    @property
    def spare(self) -> bool:
        """Il peut sans doute la donner : en double, ni favorite, ni déjà prise par un échange."""
        return self.copies > 1 and not self.starred and not self.pending


async def friend_cards(ctx: Context, username: str) -> list[FriendCard]:
    """Les cartes de ta liste chez cet ami, dans une rareté qui t'avance : celles qu'il peut donner d'abord, puis par
    valeur croissante."""
    need = await needs(ctx)
    rows, pending = await ctx.api.friend_wishlisted(username)
    by_card: dict[str, list] = {}
    for row in rows:
        if need.useful(row.catalog_id, row.title, [row.rarity] if row.rarity else []) is not None:
            by_card.setdefault(row.catalog_id, []).append(row)
    out = []
    for card_id, copies in by_card.items():
        first = copies[0]
        count = sum(max(1, int((r.model_extra or {}).get("count") or 1)) for r in copies)
        priced = await card_value(ctx, card_id, first.title, first.rarity, first.is_shiny)
        out.append(FriendCard(priced, count, any(r.starred for r in copies),
                              card_id in pending or any(r.id in pending for r in copies)))
    known = float("inf")
    return sorted(out, key=lambda c: (not c.spare, c.priced.value if c.priced.value is not None else known))


def friend_text(cards: list[FriendCard]) -> str:
    if not cards:
        return "Cet ami n'a aucune carte de ta liste dans une rareté qui t'avance, ou sa collection n'est pas lisible."
    lines = []
    for c in cards:
        value = f"~{c.priced.value:.0f} WB" if c.priced.value is not None else "valeur inconnue"
        state = "⭐ favorite" if c.starred else "🔒 déjà dans un échange" if c.pending else \
            f"×{c.copies}, en double" if c.copies > 1 else "son seul exemplaire"
        lines.append(f"{c.priced.label} : {value} · {state}")
    spare = [c for c in cards if c.spare]
    if spare:
        total = sum(c.priced.value or 0 for c in spare)
        unknown = sum(c.priced.value is None for c in spare)
        lines.append(f"\n**Offre équilibrée pour {plural(len(spare), 'doublon')} : ~{total:.0f} WB**"
                     + (f" (hors {unknown} sans valeur connue)" if unknown else ""))
    return "\n".join(lines)
