"""Cartes de collection manquantes que possèdent tes amis (/amis) : à toi de leur proposer un échange sur le site.

Lecture seule, à la demande, en requêtes de fond. Le bot n'a aucune route pour proposer un échange : rien n'est envoyé
aux amis. La liste de souhaits porte les cartes manquantes (COLLECTION_WISHLIST) et le catalogue dit, pour chacune, quels
amis la possèdent (`friendOwners`) : ~30 lectures quel que soit le nombre d'amis, au lieu d'une collection par ami.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.client import ApiError
from src.core.timeutils import utcnow
from src.roi import plural
from src.trades import Priced, card_value
from src.workers.base import Context
from src.workers.collection import KV_KEY as COLLECTION_KV
from src.workers.collection import collection_key

KV_KEY = "friends_missing"
FRESH = timedelta(hours=1)  # /amis puis /amis pseudo : pas de nouvelle lecture du catalogue
PAGE_SIZE = 50
MAX_PAGES = 200
FRIENDS_SHOWN = 15
TITLES_SHOWN = 5


async def _missing(ctx: Context) -> set[str]:
    return set((await ctx.db.kv_get(COLLECTION_KV) or {}).get("missing") or [])


async def overview(ctx: Context, refresh: bool = False, now: datetime | None = None) -> dict:
    """{"at", "cards" : cartes manquantes lues, "friends" : {pseudo : [titres]}}, gardé une heure."""
    now = now or utcnow()
    state = await ctx.db.kv_get(KV_KEY)
    if state and not refresh and now - datetime.fromisoformat(state["at"]) < FRESH:
        return state
    missing = await _missing(ctx)
    friends: dict[str, list[str]] = {}
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
            title = str(card.get("wikipedia_title") or "")
            if collection_key(title) not in missing:
                continue  # dans la liste de souhaits par le joueur lui-même : pas une carte de collection
            for owner in owners.get(card.get("id")) or []:
                if isinstance(owner, dict) and owner.get("username"):
                    friends.setdefault(str(owner["username"]), []).append(title)
        if len(cards) < PAGE_SIZE or not fresh:
            break
    state = {"at": now.isoformat(), "cards": len(missing),
             "friends": dict(sorted(friends.items(), key=lambda kv: (-len(kv[1]), kv[0].casefold())))}
    await ctx.db.kv_set(KV_KEY, state)
    return state


def overview_text(state: dict) -> str:
    friends = state["friends"]
    if not friends:
        return "Aucun ami ne possède tes cartes manquantes pour l'instant."
    found = {title for titles in friends.values() for title in titles}
    lines = [f"**{len(found)} cartes** chez **{len(friends)} amis**, sur {state['cards']} manquantes.\n"]
    for name, titles in list(friends.items())[:FRIENDS_SHOWN]:
        more = "…" if len(titles) > TITLES_SHOWN else ""
        lines.append(f"**{name}** ({len(titles)}) : " + ", ".join(titles[:TITLES_SHOWN]) + more)
    if len(friends) > FRIENDS_SHOWN:
        lines.append(f"… et {len(friends) - FRIENDS_SHOWN} autres amis")
    return "\n".join(lines)


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
    """Les cartes manquantes de cet ami, celles qu'il peut donner d'abord, puis par valeur croissante."""
    missing = await _missing(ctx)
    rows, pending = await ctx.api.friend_wishlisted(username)
    by_card: dict[str, list] = {}
    for row in rows:
        if collection_key(row.title) in missing:
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
        return "Cet ami n'a aucune de tes cartes manquantes, ou sa collection n'est pas lisible."
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
