"""Favoris à monter en rareté (src/workers/favorites.py) : demandé le 22/11, pour monter les cartes des vitrines et
des collections sans chercher des centaines de titres un par un."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.models import Auction
from src.snipes import Snipe
from src.workers import favorites
from tests.test_integration import make_client, make_context
from tests.test_session import cookie_for

CATALOG = [{"id": "c-ada", "wikipedia_title": "Ada Lovelace", "rarity": "C"},
           {"id": "c-gold", "wikipedia_title": "Or", "rarity": "C"},
           {"id": "c-top", "wikipedia_title": "Khéops", "rarity": "C"},
           {"id": "c-up", "wikipedia_title": "Sphinx", "rarity": "C"}]


def copy(cid: str, oid: str, rarity: str | None, starred: bool = False, shiny: bool = False) -> dict:
    return {"id": oid, "card_id": cid, "snapshot_rarity": rarity, "is_shiny": shiny, "starred": starred}


COPIES = [copy("c-ada", "a-r", "R", starred=True),
          copy("c-gold", "g-sr", "SR", starred=True), copy("g-c", "x", None),  # carte d'un autre favori ignorée
          copy("c-gold", "g-shiny", "C", shiny=True),  # un shiny : déjà au plus haut
          copy("c-top", "t-l", "L", starred=True),
          copy("c-up", "u-r", "R", starred=True), copy("c-up", "u-ur", "UR")]  # monté hors favori : à déplacer


def test_plan_tracks_cards_below_l_without_shiny_and_moves_the_favorite_to_the_best_copy():
    copies = {}
    for row in COPIES:
        copies.setdefault(row["card_id"], []).append(row)
    cards, moves = favorites.plan(copies, {c["id"]: c for c in CATALOG})
    assert cards == {"c-ada": {"title": "Ada Lovelace", "best": 2}, "c-top": {"title": "Khéops", "best": 5},
                     "c-up": {"title": "Sphinx", "best": 4}}
    assert [(best["id"], [r["id"] for r in lower], title) for best, lower, title in moves] == [
        ("u-ur", ["u-r"], "Sphinx")]


def test_favorite_demand_takes_only_higher_rarities_never_a_shiny_sale():
    snipe = Snipe("Ada Lovelace", 0, favorite=True, above=2)

    def sale(rarity, shiny=False):
        return Auction.model_validate({"id": "a", "card": {"id": "c-ada", "wikipedia_title": "Ada Lovelace",
                                                           "rarity": rarity}, "rarity": rarity, "is_shiny": shiny,
                                       "end_at": datetime.now(UTC).isoformat(), "base_amount": 10})

    assert snipe.matches(sale("SR")) and snipe.matches(sale("L"))
    assert not snipe.matches(sale("R")) and not snipe.matches(sale("PC")) and not snipe.matches(sale("SR", True))


def site(writes: list, wishlist: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if request.method != "GET":
            body = json.loads(request.content) if request.content else None
            writes.append((request.method, path.rsplit("/", 1)[-1], body if body is not None else dict(params)))
            return httpx.Response(204)
        if path == "/rest/v1/user_cards" and params.get("starred") == "eq.true":
            return httpx.Response(200, json=[{"card_id": r["card_id"]} for r in COPIES if r["starred"]])
        if path == "/rest/v1/user_cards":
            wanted = params["card_id"].removeprefix("in.(").removesuffix(")").split(",")
            return httpx.Response(200, json=[r for r in COPIES if r["card_id"] in wanted])
        if path == "/rest/v1/cards":
            wanted = params["id"].removeprefix("in.(").removesuffix(")").split(",")
            return httpx.Response(200, json=[c for c in CATALOG if c["id"] in wanted])
        if path == "/rest/v1/wishlist_items":
            return httpx.Response(200, json=[{"card_id": c} for c in wishlist])
        raise AssertionError(f"route inattendue {request.method} {request.url}")
    return handler


@pytest.mark.asyncio
async def test_sync_puts_favorites_in_the_wishlist_moves_stars_and_leaves_your_own_wishes(tmp_path):
    writes: list = []
    wishlist = ["c-up", "c-old"]  # c-up : un souhait à toi ; c-old : un favori retiré depuis
    client, notifier = make_client(tmp_path, site(writes, wishlist), dry_run=False, favorites_upgrade=True,
                                   supabase_anon_key="k", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    await ctx.db.kv_set(favorites.KV_KEY, {"cards": {}, "added": {"c-old": "Vieux favori"}})
    result = await favorites.sync(ctx)
    assert result == {"tracked": 2, "added": 1, "removed": 1, "moved": ["Sphinx"], "error": ""}
    me = client.user_id
    assert writes == [
        ("POST", "wishlist_items", [{"user_id": me, "card_id": "c-ada"}]),  # c-up y est déjà : pas ajoutée
        ("DELETE", "wishlist_items", {"user_id": f"eq.{me}", "card_id": "in.(c-old)"}),
        ("PATCH", "user_cards", {"starred": True}), ("PATCH", "user_cards", {"starred": False}),
    ]
    assert set(await favorites.added_ids(ctx.db)) == {"c-ada"}  # c-up reste à toi : jamais retirée par ce worker
    loaded = await favorites.load(ctx.db)
    assert {(s.title, s.above, s.favorite) for s in loaded.values()} == {("Ada Lovelace", 2, True),
                                                                        ("Sphinx", 4, True)}
    # Victoire du sniper pas encore arrivée dans la collection : la SR compte quand même au passage suivant.
    await favorites.record_win(ctx.db, "Ada Lovelace", "SR")
    await favorites.sync(ctx)
    assert (await favorites.load(ctx.db))[Snipe("Ada Lovelace", 0).key].above == 3
    await ctx.db.close()


@pytest.mark.asyncio
async def test_simulated_sync_writes_nothing_but_still_tracks(tmp_path):
    writes: list = []
    client, notifier = make_client(tmp_path, site(writes, []), dry_run=False, simulate=["favorites"],
                                   favorites_upgrade=True, supabase_anon_key="k",
                                   wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    result = await favorites.sync(ctx)
    assert writes == [] and result["tracked"] == 2 and result["added"] == 0 and result["moved"] == []
    assert len(await favorites.load(ctx.db)) == 2
    await ctx.db.close()


@pytest.mark.asyncio
async def test_sniper_bids_on_a_favorite_up_to_its_quote_and_counts_the_win(tmp_path):
    """Plafond = la cote de la rareté en vente, jamais plus ; gagnée, la carte est gardée et la rareté suivante est
    visée."""
    from src.workers.auction_sniper import AuctionSniper, Target
    from tests.test_integration import seed_observed
    from tests.test_sniper_discovery import sale

    market = {"f1": sale("f1", "Ada Lovelace", "SR", 300, base=40), "f0": sale("f0", "Ada Lovelace", "R", 300, base=5)}
    for aid in market:
        market[aid]["card"]["id"] = "c-ada"

    def handler(request):
        path, params = request.url.path, request.url.params
        if path.startswith("/rest/v1/"):
            return httpx.Response(200, json=[])
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": []})
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 5000})
        if "mine" in params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales") or path.endswith("/price-history"):
            return httpx.Response(404, json={})
        if path.endswith("/bid"):
            return httpx.Response(200, json={"current_bid": 40, "bidder_balance": 4960})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": [], "hasMore": False})
        raise AssertionError(f"route inattendue {request.method} {request.url}")

    client, notifier = make_client(tmp_path, handler, sniper_resale_rarities=["L"], favorites_upgrade=True,
                                   dry_run=False, supabase_anon_key="k", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set(favorites.KV_KEY, {"cards": {"c-ada": {"title": "Ada Lovelace", "best": 2}}, "added": {}})
    await seed_observed(db, "c-ada", [60, 64, 62], rarity="SR")
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    assert sniper._searched() == set()  # jamais cherchée par son titre
    now = datetime.now(UTC)
    for aid in ("f1", "f0"):
        market[aid]["end_at"] = (now + timedelta(seconds=16)).isoformat()
        sniper._targets[aid] = Target(Auction.model_validate(market[aid]), now - timedelta(seconds=30))
    for _ in range(4):
        await sniper.step()
        if bids := await db._all("SELECT * FROM bids"):
            break
    assert [(b["auction_id"], b["amount"]) for b in bids] == [("f1", 40)]  # la R n'est pas au-dessus
    assert bids[0]["reason"].startswith(f"{favorites.REASON} ≤ 6")  # cote ~62
    await sniper._close_bid(bids[0], won=True, simulated=False)
    assert ("SUCCESS", "Favori monté en rareté !") in notifier.sent
    assert [s.above for s in sniper._favorites.values()] == [3]  # l'UR et la L restent visées
    await db.close()


def test_kept_copy_of_a_collection_card_is_never_sold_even_without_a_tag():
    """22/11 : « Vitrine » ne va plus sur les cartes des collections. La collection des pays n'a pas d'étiquette
    propre : une France seule en L serait partie avec DUPLICATES_LAST_COPY_RARITIES=L."""
    from types import SimpleNamespace

    from src.config import Settings
    from src.core.models import OwnedCard
    from src.core.pricing import Quote
    from src.workers.inventory_manager import collection_kept, select_for_sale

    def owned(oid, card, title, rarity):
        return OwnedCard.model_validate({"id": oid, "card_id": card, "snapshot_rarity": rarity,
                                         "card": {"id": card, "wikipedia_title": title, "rarity": rarity}})

    rows = [owned("fr", "c-fr", "France", "L"), owned("zz", "c-zz", "Zinedine Zidane", "L")]
    s = Settings(_env_file=None, collection_sets=["countries"], duplicates_last_copy_rarities=["L"],
                 duplicates_min_value=0)
    excluded = collection_kept(SimpleNamespace(settings=s), rows)
    assert excluded == {"fr"}
    quotes = {"c-fr": {"L": Quote(2000, 5)}, "c-zz": {"L": Quote(2000, 5)}}
    groups = {"c-fr": [rows[0]], "c-zz": [rows[1]]}
    assert [c.owned.id for c in select_for_sale(groups, quotes, excluded, s)] == ["zz"]
