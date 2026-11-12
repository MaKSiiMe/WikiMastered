"""Cartes souhaitées : une carte que tu ajoutes toi-même à ta liste de souhaits du site est achetée par le sniper.

Toutes les WISH_CHECK_MINUTES, le worker « souhaits » relit ta liste de souhaits (table Supabase `wishlist_items`).
Les cartes que le bot y a mises pour les collections (COLLECTION_WISHLIST) ne comptent pas : seules les tiennes
deviennent des cartes souhaitées (kv « wishes »), avec leur titre et la plus haute rareté que tu en possèdes déjà.

Le sniper les mise comme une carte demandée sur Discord (/snipe), au minimum, jusqu'à la cote de la carte dans la rareté
en vente et jamais au-dessus de WISH_MAX_PRICE. Sans cote connue : pas de mise. Il prend toute rareté plus haute que
celle que tu possèdes : une carte obtenue en R peut encore être rachetée en SR, tant qu'elle reste dans ta liste.
Une carte gagnée est gardée (pas revendue) et reçoit l'étiquette WISH_TAG. Le bot ne retire rien de ta liste : retire
la carte toi-même pour arrêter.
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime, timedelta

from src.config import RARITIES, RARITY_RANK
from src.core.api import COLLECTION_PAGE_SIZE
from src.core.client import ApiError
from src.core.database import Database
from src.core.models import OwnedCard
from src.core.notifier import Topic
from src.core.text import tag_key
from src.core.timeutils import utcnow
from src.snipes import Snipe
from src.workers import wishlist
from src.workers.base import Context, Worker
from src.workers.collection import collection_key

KV_KEY = "wishes"
# Motif des mises d'une carte souhaitée : « collection » en tête, la carte est gardée comme une carte de collection.
REASON = "collection : souhaitée"
OWNED_FRESH = timedelta(hours=6)  # rareté possédée relue au plus toutes les N h (un paquet peut l'avoir donnée)
SEARCH_PAGES = 5
LOOKUP_BATCH = 50
TAG_TRIES = 12  # passages avant de renoncer à étiqueter une carte gagnée (exemplaire jamais arrivé)


async def _state(db: Database) -> dict:
    state = await db.kv_get(KV_KEY) or {}
    state.setdefault("cards", {})
    state.setdefault("to_tag", [])
    return state


async def load(db: Database, max_price: int) -> dict[str, Snipe]:
    """Cartes souhaitées, par clé de titre, comme des demandes du sniper : plafond WISH_MAX_PRICE (la cote le baisse
    à chaque vente), raretés au-dessus de celle possédée. Déjà en L : plus rien à acheter."""
    if max_price <= 0:
        return {}
    out: dict[str, Snipe] = {}
    for card in (await _state(db))["cards"].values():
        best = int(card.get("best", -1))
        if card.get("title") and best < len(RARITIES) - 1:
            snipe = Snipe(card["title"], max_price, added_at=card.get("since", ""), wish=True, above=best)
            out[snipe.key] = snipe
    return out


async def card_ids(db: Database) -> dict[str, str]:
    """Id de carte -> clé de titre, pour reconnaître les ventes annoncées par le site."""
    return {cid: collection_key(card["title"]) for cid, card in (await _state(db))["cards"].items()
            if card.get("title")}


async def record_win(db: Database, title: str, rarity: str | None, at: datetime | None = None) -> None:
    """Carte souhaitée gagnée : sa rareté devient celle possédée, et l'exemplaire attend son étiquette."""
    state = await _state(db)
    key = collection_key(title)
    for card in state["cards"].values():
        if card.get("title") and collection_key(card["title"]) == key:
            card["best"] = max(int(card.get("best", -1)), RARITY_RANK.get(rarity or "", -1))
    state["to_tag"].append({"title": title, "rarity": rarity, "since": (at or utcnow()).isoformat(), "tries": 0})
    await db.kv_set(KV_KEY, state)


async def _titles(ctx: Context, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for start in range(0, len(ids), LOOKUP_BATCH):
        batch = ids[start:start + LOOKUP_BATCH]
        status, rows = await ctx.client.supabase("GET", "cards", background=True, params={
            "select": "id,wikipedia_title", "id": f"in.({','.join(batch)})"})
        if status != 200 or not isinstance(rows, list):
            raise ApiError(status, rows, "supabase:cards")
        out.update({str(r["id"]): str(r["wikipedia_title"]) for r in rows if r.get("wikipedia_title")})
    return out


async def _copies(ctx: Context, title: str) -> list[OwnedCard]:
    """Tes exemplaires de cette carte (titre exact, casse ignorée)."""
    rows: list[OwnedCard] = []
    for number in range(SEARCH_PAGES):
        page = await ctx.api.collection_page(number, sort="rarity", q=title)
        rows += [r for r in page.rows() if collection_key(r.title) == collection_key(title)]
        if len(page.collection) < COLLECTION_PAGE_SIZE:
            break
    return rows


def best_rank(rows: list[OwnedCard]) -> int:
    return max((RARITY_RANK.get(r.rarity or "", -1) for r in rows), default=-1)


async def sync(ctx: Context, now: datetime | None = None) -> dict:
    """Aligne les cartes souhaitées sur ta liste de souhaits. Renvoie {"added": titres, "removed": titres}."""
    now = now or utcnow()
    state = await _state(ctx.db)
    cards: dict[str, dict] = state["cards"]
    bot = set(((await ctx.db.kv_get(wishlist.KV_KEY) or {}).get("added") or {}).values())
    mine = await wishlist.current(ctx) - bot
    removed = [cards.pop(cid).get("title", cid) for cid in list(cards) if cid not in mine]
    fresh = sorted(mine - set(cards))
    titles = await _titles(ctx, fresh) if fresh else {}
    added = []
    for cid in fresh:
        if cid in titles:
            cards[cid] = {"title": titles[cid], "since": now.isoformat()}
            added.append(titles[cid])
    for card in cards.values():
        checked = card.get("checked")
        if checked and now - datetime.fromisoformat(checked) < OWNED_FRESH:
            continue
        with contextlib.suppress(ApiError):  # relue au passage suivant
            # Jamais sous la rareté d'une carte gagnée : l'exemplaire met un moment à arriver dans la collection.
            card["best"] = max(int(card.get("best", -1)), best_rank(await _copies(ctx, card["title"])))
            card["checked"] = now.isoformat()
    await ctx.db.kv_set(KV_KEY, state)
    return {"added": added, "removed": removed}


async def tag_won(ctx: Context) -> tuple[int, str]:
    """Pose WISH_TAG sur l'exemplaire de chaque carte souhaitée gagnée. Renvoie (étiquettes posées, problème)."""
    state = await _state(ctx.db)
    pending: list[dict] = state["to_tag"]
    tag = ctx.settings.wish_tag.strip()
    if not pending:
        return 0, ""
    if not tag or not ctx.settings.is_live("tag_card"):
        state["to_tag"] = []
        await ctx.db.kv_set(KV_KEY, state)
        return 0, ""
    status, data = await ctx.client.supabase("GET", "tags", params={
        "select": "id,name", "user_id": f"eq.{ctx.api.user_id}"})
    ids = {tag_key(str(t.get("name", ""))): t["id"] for t in data or [] if isinstance(t, dict)} \
        if status == 200 else {}
    tag_id = ids.get(tag_key(tag))
    if tag_id is None:
        return 0, f"étiquette « {tag} » introuvable (HTTP {status}) : crée-la une fois sur le site"
    done, left = 0, []
    for item in pending:
        posted = False
        with contextlib.suppress(ApiError):
            since = datetime.fromisoformat(item["since"]) - timedelta(minutes=1)
            copies = [c for c in await _copies(ctx, item["title"])
                      if (c.rarity or "?") == (item.get("rarity") or "?") and c.obtained_at and c.obtained_at >= since]
            if copies:
                copy = max(copies, key=lambda c: c.obtained_at)
                status, _ = await ctx.client.supabase("POST", "user_card_tags", prefer="return=minimal",
                                                      json_body={"user_card_id": copy.id, "tag_id": tag_id})
                posted = status in (200, 201, 204, 409)  # 409 : déjà étiquetée
        item["tries"] = int(item.get("tries", 0)) + 1
        if posted:
            done += 1
        elif item["tries"] < TAG_TRIES:
            left.append(item)
    # Relu : le sniper a pu enregistrer une carte gagnée pendant les requêtes.
    state = await _state(ctx.db)
    state["to_tag"] = left + state["to_tag"][len(pending):]
    await ctx.db.kv_set(KV_KEY, state)
    return done, ""


async def overview_text(ctx: Context) -> str:
    """Commande /souhaits : chaque carte souhaitée, la rareté déjà possédée et le plafond de chaque rareté encore à
    prendre (sa cote, au plus WISH_MAX_PRICE ; sans requête : ventes observées et cote du site en cache)."""
    cap = ctx.settings.wish_max_price
    if cap <= 0:
        return "Cartes souhaitées coupées : renseigne `WISH_MAX_PRICE` (plafond en WB) pour que le sniper les achète."
    cards = (await _state(ctx.db))["cards"]
    if not cards:
        return ("Aucune carte souhaitée. Ajoute une carte à ta liste de souhaits sur le site : elle apparaît ici en "
                f"{ctx.settings.wish_check_minutes:g} min au plus.")
    lines = [f"Plafond : la cote de la carte, au plus **{cap} WB**.\n"]
    for cid, card in sorted(cards.items(), key=lambda kv: kv[1].get("title", "")):
        best = int(card.get("best", -1))
        have = f"tu as la {RARITIES[best]}" if best >= 0 else "pas encore possédée"
        try:
            quotes = await ctx.prices.quotes(cid, fetch=False)
        except Exception:  # noqa: BLE001 - la liste s'affiche sans les cotes
            quotes = {}
        caps = []
        for rarity in RARITIES[best + 1:]:
            quote = quotes.get(rarity)
            if quote is not None and quote.average is not None:
                caps.append(f"{rarity} ≤ {min(int(quote.average), cap)}")
        detail = " · ".join(caps) if caps else ("déjà en L : plus rien à acheter" if best >= len(RARITIES) - 1
                                                else "aucune cote connue pour l'instant : pas de mise")
        lines.append(f"• **{card.get('title', cid)}** ({have})\n{detail}")
    return "\n".join(lines)


class WishWatch(Worker):
    name = "souhaits"
    topic = Topic.COLLECTION

    async def setup(self) -> None:
        self._tag_warned = False

    async def step(self) -> float:
        changed = await sync(self.ctx)
        tag = self.settings.wish_tag.strip()
        for title in changed["added"]:
            self.log.info("carte souhaitée : %s, suivie par le sniper", title)
        for title in changed["removed"]:
            self.log.info("carte souhaitée : %s retirée de ta liste, plus suivie", title)
        if changed["added"]:
            self.notifier.info(
                "Cartes souhaitées suivies", ", ".join(changed["added"]) + " : le sniper les mise jusqu'à leur cote "
                f"(au plus {self.settings.wish_max_price} WB), toute rareté au-dessus de celle que tu possèdes. "
                "Gagnée, la carte est gardée" + (f" avec l'étiquette « {tag} »." if tag else "."), important=True)
        done, problem = await tag_won(self.ctx)
        if done:
            self.log.info("étiquette « %s » posée sur %d carte(s) souhaitée(s)", tag, done)
        if problem and not self._tag_warned:
            self._tag_warned = True
            self.notifier.warning("Cartes souhaitées : étiquette", problem)
        return random.uniform(0.8, 1.2) * self.settings.wish_check_minutes * 60
