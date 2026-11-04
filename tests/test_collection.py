"""Collection : lecture tolérante, doublons, mode collection des jours de l'année."""

from __future__ import annotations

from datetime import UTC

import httpx
import pytest

from src.core.api import WikiMastersAPI
from tests.test_integration import make_client, make_context


def copy(oid: str, card: str, title: str | None = None) -> dict:
    return {"id": oid, "card_id": card, "card": {"id": card, "wikipedia_title": title or card, "rarity": "C"}}


@pytest.mark.asyncio
async def test_collection_read_survives_changes_during_paging(tmp_path):
    """Une carte gagnée pendant la lecture décale les pages : l'exemplaire revu est ignoré."""
    page0 = [copy(f"o{i}", f"c{i}") for i in range(50)]
    page1 = [copy("o49", "c49"), copy("o50", "c50")]  # o49 revu à cause du décalage

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"collection": page0 if page == 0 else page1})

    client, _ = make_client(tmp_path, handler)
    rows = await WikiMastersAPI(client).load_collection()
    assert [r.id for r in rows] == [f"o{i}" for i in range(51)]


@pytest.mark.asyncio
async def test_duplicates_priced_within_quota(tmp_path):
    """Pas plus de SELL_PRICE_LOOKUPS_PER_RUN cotes demandées ; les autres cartes : ventes observées."""
    from src.workers.inventory_manager import InventoryManager, select_for_sale
    from tests.test_integration import seed_observed

    rows = [copy("a1", "A"), copy("a2", "A"), copy("a3", "A"), copy("b1", "B"), copy("b2", "B"),
            copy("c1", "C"), copy("c2", "C")]
    lookups = []

    def handler(request):
        if request.url.path == "/api/my-collection":
            return httpx.Response(200, json={"collection": rows})
        if request.url.path == "/api/trades":
            return httpx.Response(200, json={"trades": []})
        if request.url.path.endswith("/sales"):
            lookups.append(request.url.path)
            return httpx.Response(200, json={"summary": {"C": {"average": 80}}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, sell_price_lookups_per_run=1)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await seed_observed(db, "B", [60, 70])
    inv = InventoryManager(ctx)
    groups, excluded, quotes, _ = await inv.snapshot()
    assert len(lookups) == 1  # A (3 exemplaires, prioritaire) : la cote seule, 1 seule carte
    assert quotes["C"] == {}  # ni cote ni vente observée : ignorée ce passage
    sales = select_for_sale(groups, quotes, excluded, client.settings)
    assert sorted((c.owned.catalog_id, c.price) for c in sales) == [("A", 80), ("A", 80), ("B", 65)]
    await db.close()


@pytest.mark.asyncio
async def test_live_messages_have_no_dry_run_prefix():
    import json

    from src.core.notifier import DiscordNotifier, Level, _Message

    titles = []

    def handler(request):
        titles.append(json.loads(request.content)["embeds"][0]["title"])
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.test/webhook", dry_run=True)
    notifier._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await notifier._deliver(_Message(Level.SUCCESS, "Tirage remarquable !", "", {}, live=True))
    await notifier._deliver(_Message(Level.INFO, "Enchère simulée", ""))
    assert titles == ["Tirage remarquable !", "[DRY RUN] Enchère simulée"]


def test_year_days_and_title_keys():
    from src.workers.collection import collection_key, year_days

    days = year_days()
    assert len(days) == 366 and days[0] == "1er janvier" and "29 février" in days and days[-1] == "31 décembre"
    assert collection_key("1er Janvier") == collection_key("1 janvier") == "1 janvier"
    assert collection_key("14 Octobre") == "14 octobre"


@pytest.mark.asyncio
async def test_collection_refresh_finds_owned_days(tmp_path):
    from src.workers.collection import refresh

    def handler(request):
        q = request.url.params.get("q", "")
        rows = {"janvier": [copy("o1", "d1", "1er janvier"), copy("o2", "d1", "1er janvier"),
                            copy("o3", "r1", "Révolution de janvier"), copy("o4", "d3", "3 Janvier")],
                "octobre": [copy("o5", "d14", "14 octobre"), copy("o6", "x", "14 octobre 1066")]}.get(q, [])
        return httpx.Response(200, json={"collection": rows})

    client, notifier = make_client(tmp_path, handler, collection_sets=["jours"])
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    state = await refresh(ctx)
    assert state["owned"] == ["1 janvier", "14 octobre", "3 janvier"] and len(state["missing"]) == 363
    assert state["unmatched"] == ["14 octobre 1066"]
    assert (await db.kv_get("collection"))["total"] == 366
    await db.close()


@pytest.mark.asyncio
async def test_sniper_completes_collection_one_auction_at_a_time(tmp_path):
    from datetime import datetime, timedelta

    from src.workers.auction_sniper import AuctionSniper

    now = datetime.now(UTC)

    def sale(aid, title, base, seconds):
        return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": (now + timedelta(seconds=seconds)).isoformat(), "base_amount": base,
                "current_bid": None, "current_bidder_id": None}

    market = [sale("a1", "14 octobre", 20, 15), sale("a2", "14 octobre", 10, 16), sale("a3", "2 mars", 80, 15),
              sale("a4", "3 mars", 30, 15)]

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, collection_sets=["jours"], collection_max_price=50)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set("collection", {"owned": ["3 mars"], "missing": ["14 octobre", "2 mars"], "total": 366})
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    rows = await db._all("SELECT auction_id, amount, reason FROM bids")
    # 14 octobre : une seule des deux ventes ; 2 mars : 80 > 50 WB ; 3 mars : déjà possédé.
    assert len(rows) == 1 and rows[0]["auction_id"] in ("a1", "a2") and rows[0]["reason"].startswith("collection")
    await sniper.step()
    assert len(await db._all("SELECT * FROM bids")) == 1  # pas de mise sur l'autre vente du 14 octobre
    await db._exec("UPDATE bids SET status = 'sim_won', closed = 1")
    await sniper._load_collection()
    assert "14 octobre" not in sniper._collection_missing  # gagnée : plus recherchée
    await db.close()


@pytest.mark.asyncio
async def test_owned_days_get_the_date_tag(tmp_path):
    import json as jsonlib

    from src.workers.collection import refresh
    from tests.test_session import cookie_for

    inserted = []

    def day(oid, title, tags=(), starred=False):
        row = copy(oid, f"card-{title}", title)
        row.update(tags=[{"id": 1, "name": t} for t in tags], starred=starred)
        return row

    def handler(request):
        if request.url.host.endswith("supabase.co"):
            assert request.headers["apikey"] == "anon-key" and request.headers["authorization"] == "Bearer a1"
            if request.method == "GET":
                assert request.url.path == "/rest/v1/tags"
                return httpx.Response(200, json=[{"id": 3, "name": "Pokémon"}, {"id": 7, "name": "Date"}])
            inserted.append(jsonlib.loads(request.content))
            return httpx.Response(201)
        rows = {"janvier": [day("o1", "1er janvier"), day("o2", "1er janvier", starred=True),
                            day("o3", "2 janvier", tags=["date"])]}.get(request.url.params.get("q", ""), [])
        return httpx.Response(200, json={"collection": rows})

    client, notifier = make_client(tmp_path, handler, dry_run=False, collection_sets=["Jours"],
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    state = await refresh(ctx)
    assert state["owned"] == ["1 janvier", "2 janvier"] and state["unknown_sets"] == []
    # 1er janvier : l'exemplaire favori est étiqueté ; 2 janvier l'est déjà.
    assert inserted == [{"user_card_id": "o2", "tag_id": 7}] and state["tagged"] == 1 and state["to_tag"] == 0
    await db.close()


@pytest.mark.asyncio
async def test_collection_from_file_reads_everything_and_uses_its_own_tag(tmp_path, monkeypatch):
    """Une collection de config/collections.yaml : toute la collection est lue (requêtes de fond), son
    avancement est suivi à part et son étiquette est posée sur ses cartes, celle des jours sur les jours."""
    import json as jsonlib

    from src.workers import collection
    from tests.test_session import cookie_for

    path = tmp_path / "collections.yaml"
    path.write_text("elements:\n  nom: Éléments chimiques\n  etiquette: élément\n"
                    "  titres: [Hydrogène, Hélium, Mercure (chimie)]\n", encoding="utf-8")
    monkeypatch.setattr(collection, "COLLECTIONS_FILE", path)
    queries, inserted = [], []

    def handler(request):
        if request.url.host.endswith("supabase.co"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 7, "name": "date"}, {"id": 9, "name": "Élément"}])
            inserted.append(jsonlib.loads(request.content))
            return httpx.Response(201)
        queries.append(request.url.params.get("q", ""))
        return httpx.Response(200, json={"collection": [copy("o1", "h", "Hydrogène"), copy("o2", "m", "Mercure"),
                                                         copy("o3", "d", "1er janvier")]})

    client, notifier = make_client(tmp_path, handler, dry_run=False, collection_sets=["Jours", "elements", "pays"],
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    state = await collection.refresh(ctx)
    assert queries == [""]  # une seule lecture complète, pas de recherche par mois
    assert state["owned"] == ["1 janvier", "hydrogene"] and state["unknown_sets"] == ["pays"]
    assert state["sets"] == {"Jours de l'année": [1, 366], "Éléments chimiques": [1, 3]}
    assert sorted(i["tag_id"] for i in inserted) == [7, 9] and state["tagged"] == 2
    assert "elements" in collection.unknown_text(client.settings, ["pays"])
    await ctx.db.close()


def test_completion_tracks_when_each_card_was_obtained():
    from datetime import datetime, timedelta

    from src.workers.collection import first_owned, progress_lines

    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    first = first_owned({}, {"1 janvier", "2 janvier"}, now - timedelta(days=10))
    assert first == {"1 janvier": None, "2 janvier": None}  # déjà là au 1er relevé : date inconnue
    later = first_owned({"first_owned": first}, {"1 janvier", "2 janvier", "14 octobre"}, now - timedelta(days=1))
    assert later["14 octobre"] == (now - timedelta(days=1)).isoformat() and later["1 janvier"] is None
    state = {"owned": sorted(later), "total": 368, "first_owned": later,
             "sets": {"Jours de l'année": [3, 366], "Titres": [0, 2]}}
    assert progress_lines(state, {"14 octobre": "14 octobre"}, now) == [
        "Jours de l'année : 3/366 (0,8 %)", "Titres : 0/2 (0,0 %)", "+1 en 7 jours : 14 octobre (14/10)"]


@pytest.mark.asyncio
async def test_profile_command_reads_the_rpc_and_hides_personal_fields(tmp_path):
    from src.main import profile_text
    from tests.test_session import cookie_for

    def handler(request):
        assert request.url.host.endswith("supabase.co") and request.url.path == "/rest/v1/rpc/get_my_profile"
        assert request.method == "POST" and request.headers["authorization"] == "Bearer a1"
        return httpx.Response(200, json={"packs_remaining": 3, "pity_counter": 7, "email": "moi@exemple.fr"})

    client, _ = make_client(tmp_path, handler, supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    status, data = await client.supabase("POST", "rpc/get_my_profile", json_body={})
    text = profile_text(data)
    assert status == 200 and "pity_counter = 7" in text and "packs_remaining = 3" in text
    assert "email = ***" in text and "exemple.fr" not in text
    assert "Réponse inattendue" in profile_text(None)


@pytest.mark.asyncio
async def test_sales_history_asked_only_for_a_pro_account(tmp_path):
    """Historique des ventes réservé aux comptes PRO : le 18/10, le bot le demandait toutes les 6 h pour un 403."""
    asked, pro = [], {"isPro": False}

    def handler(request):
        scope = request.url.params.get("scope")
        asked.append(scope or "history")
        if scope == "summary":
            return httpx.Response(200, json={"summary": {"L": {"average": 900}}, **pro})
        return httpx.Response(200, json={"recent": [{"final_price": 700, "snapshot_rarity": "L"}]})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    prices = ctx.prices
    assert (await prices.quote("a", "L")).average == 900 and asked == ["summary"]
    pro["isPro"] = True
    await prices.quote("b", "L")
    assert (await prices.quote("c", "L")).average == 700 and asked == ["summary", "summary", "history"]
    await ctx.db.close()


@pytest.mark.asyncio
async def test_tag_moves_to_the_highest_rarity_copy(tmp_path):
    import json as jsonlib

    from src.workers.collection import refresh
    from tests.test_session import cookie_for

    calls = []

    def day(oid, title, rarity, tags=()):
        row = copy(oid, f"card-{title}", title)
        row.update(snapshot_rarity=rarity, tags=[{"id": 7, "name": t} for t in tags])
        return row

    def handler(request):
        if request.url.host.endswith("supabase.co"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 7, "name": "Date"}])
            calls.append((request.method, jsonlib.loads(request.content) if request.content
                          else dict(request.url.params)))
            return httpx.Response(201 if request.method == "POST" else 204)
        rows = {"janvier": [day("o1", "1er janvier", "R", tags=["date"]), day("o2", "1er janvier", "SR"),
                            day("o3", "2 janvier", "UR", tags=["date"])]}.get(request.url.params.get("q", ""), [])
        return httpx.Response(200, json={"collection": rows})

    client, notifier = make_client(tmp_path, handler, dry_run=False, collection_sets=["jours"],
                                   supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600))
    ctx = await make_context(tmp_path, client, notifier)
    state = await refresh(ctx)
    assert state["best"] == {"1 janvier": "SR", "2 janvier": "UR"}
    # Le SR gagné reprend l'étiquette, puis le R la perd (il peut partir).
    assert calls == [("POST", {"user_card_id": "o2", "tag_id": 7}),
                     ("DELETE", {"user_card_id": "eq.o1", "tag_id": "eq.7"})]
    assert state["tagged"] == 1 and state["untagged"] == 1
    await ctx.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade", [True, False])
async def test_sniper_bids_on_a_higher_rarity_of_an_owned_card(tmp_path, upgrade):
    from datetime import datetime, timedelta

    from src.workers.auction_sniper import AuctionSniper

    now = datetime.now(UTC)

    def sale(aid, title, rarity, base):
        return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": rarity},
                "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
                "end_at": (now + timedelta(seconds=15)).isoformat(), "base_amount": base,
                "current_bid": None, "current_bidder_id": None}

    market = [sale("a1", "3 mars", "R", 10), sale("a2", "3 mars", "SR", 20), sale("a3", "4 mars", "SR", 20)]

    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if request.url.path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": market, "hasMore": False})
        if request.url.path.endswith("/sales"):
            return httpx.Response(404, json={})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, collection_sets=["jours"], collection_max_price=50,
                                   collection_upgrade=upgrade)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set("collection", {"owned": ["3 mars", "4 mars"], "missing": [], "total": 366,
                                   "best": {"3 mars": "R", "4 mars": "UR"}})
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper.step()
    rows = await db._all("SELECT id, auction_id, title, rarity, reason FROM bids")
    if not upgrade:
        assert rows == [] and sniper._collection_best == {}
        await db.close()
        return
    # 3 mars : le SR monte le R possédé, le R n'apporte rien ; 4 mars : déjà en UR.
    assert [(r["auction_id"], r["reason"]) for r in rows] == [("a2", "collection : rareté R → SR ≤ 50")]
    await sniper._close_bid(await db.last_bid("a2"), won=True, simulated=False)
    assert sniper._collection_best["3 mars"] == 3
    assert (await db.kv_get("collection"))["best"]["3 mars"] == "SR"
    await db.close()


def test_long_collection_titles_come_from_a_file(tmp_path, monkeypatch):
    from src.config import Settings
    from src.workers import collection

    path = tmp_path / "collections.yaml"
    path.write_text("gifs:\n  nom: GIF animés\n  titres: [Premier]\n  fichier: liste.txt\n", encoding="utf-8")
    (tmp_path / "liste.txt").write_text("Royaumes francs\n\n  Big Crunch \n", encoding="utf-8")
    monkeypatch.setattr(collection, "COLLECTIONS_FILE", path)
    assert collection.defined(Settings())["gifs"].titles == ("Premier", "Royaumes francs", "Big Crunch")


def test_gif_collection_is_shipped_without_its_own_tag():
    """« GIF » est posée par le worker etiquettes d'après l'image de chaque carte : deux poseurs se contrediraient."""
    from src.config import Settings
    from src.workers import collection

    gifs = collection.defined(Settings())["gifs"]
    assert gifs.tag == "" and "Royaumes francs" in gifs.titles and len(gifs.titles) == len(set(gifs.titles))
    assert {"Cuisine du Yémen", "Déclin de l’Empire byzantin", "Shitposting"} <= set(gifs.titles)  # vues chez un autre joueur


def test_gif_list_reads_the_dump_and_keeps_only_animated_articles():
    import asyncio

    from src.core.wikidata import Wikidata
    from src.tools.gif_list import animated_articles, lead_gifs

    pages = lead_gifs(["INSERT INTO `page_props` VALUES (1,'page_image_free','Anim.gif',NULL),"
                       "(2,'page_image','L\\'Anim.GIF',NULL),(3,'page_image_free','Photo.jpg',NULL),"
                       "(4,'wikibase_item','Q1.gif',NULL),(5,'page_image_free','Fixe.gif',NULL),"
                       "(6,'page_image_free','Anim.gif',NULL);"])
    assert pages == {1: "Anim.gif", 2: "L'Anim.GIF", 5: "Fixe.gif", 6: "Anim.gif"}

    def wiki(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("prop") == "imageinfo":
            return httpx.Response(200, json={"query": {"pages": [
                {"title": f"File:{f}", "imageinfo": [{"metadata": [{"name": "frameCount", "value": n}]}]}
                for f, n in (("Anim.gif", 17), ("L'Anim.GIF", 2), ("Fixe.gif", 1))]}})
        return httpx.Response(200, json={"query": {"pages": [
            {"pageid": 1, "ns": 0, "title": "Zèbre"}, {"pageid": 2, "ns": 0, "title": "Anim"},
            {"pageid": 6, "ns": 2, "title": "Utilisateur:X"}]}})

    wikidata = Wikidata(httpx.AsyncClient(transport=httpx.MockTransport(wiki)), spacing=0)
    assert asyncio.run(animated_articles(pages, wikidata)) == ["Anim", "Zèbre"]


def _wishlist_site(wished: list[dict], notifications: list[dict], market: dict | None = None, calls=None,
                   refuse_add: bool = False):
    import json

    catalogue = {"14 octobre": "c-14", "2 mars": "c-2", "Mars (planète), étude": "c-m"}

    def handler(request):
        path = request.url.path
        if calls is not None:
            calls.append((request.method, path))
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/rest/v1/cards":
            return httpx.Response(200, json=[{"id": cid, "wikipedia_title": t} for t, cid in catalogue.items()
                                             if f'"{t}"' in request.url.params["wikipedia_title"]])
        if path == "/rest/v1/wishlist_items":
            if request.method == "GET":
                return httpx.Response(200, json=[{"card_id": w["card_id"]} for w in wished])
            if request.method == "POST":
                if refuse_add:
                    return httpx.Response(400, json={"message": "limite"})
                wished.extend(json.loads(request.content))
                return httpx.Response(201)
            ids = request.url.params["card_id"]
            wished[:] = [w for w in wished if f'"{w["card_id"]}"' not in ids]
            return httpx.Response(204)
        if path == "/rest/v1/notifications":
            cursor = request.url.params["created_at"][3:]
            return httpx.Response(200, json=[n for n in notifications if n["created_at"] > cursor])
        if market and path.startswith("/api/marketplace/") and path.rsplit("/", 1)[1] in market:
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        raise AssertionError(f"route inattendue {request.method} {request.url}")
    return handler


def _wishlist_client(tmp_path, handler, **settings):
    from tests.test_session import cookie_for

    settings.setdefault("dry_run", False)
    return make_client(tmp_path, handler, supabase_anon_key="anon-key", wm_cookie=cookie_for("a1", "r1", 3600),
                       collection_sets=["jours"], collection_wishlist=True, **settings)


@pytest.mark.asyncio
async def test_wishlist_follows_missing_cards_and_keeps_the_players_own(tmp_path):
    from src.workers import wishlist

    wished = [{"card_id": "c-2"}, {"card_id": "c-joueur"}]  # le joueur a déjà mis « 2 mars » et une autre carte
    client, notifier = _wishlist_client(tmp_path, _wishlist_site(wished, []))
    ctx = await make_context(tmp_path, client, notifier)
    titles = {"14 octobre": "14 octobre", "2 mars": "2 mars", "inconnue": "Inconnue"}

    done = await wishlist.sync(ctx, ["14 octobre", "2 mars", "inconnue"], titles)
    assert (done["added"], done["removed"], done["covered"], done["unknown"]) == (1, 0, 2, 1)
    assert {w["card_id"] for w in wished} == {"c-2", "c-joueur", "c-14"}
    assert await wishlist.covered(ctx) == {"14 octobre", "2 mars"}

    # Toutes deux gagnées : seule celle ajoutée par le bot part.
    done = await wishlist.sync(ctx, [], titles)
    assert (done["added"], done["removed"], done["covered"]) == (0, 1, 0)
    assert {w["card_id"] for w in wished} == {"c-2", "c-joueur"}
    await ctx.db.close()


@pytest.mark.asyncio
async def test_wishlist_refused_keeps_the_title_search(tmp_path):
    from src.workers import wishlist

    client, notifier = _wishlist_client(tmp_path, _wishlist_site([], [], refuse_add=True))
    ctx = await make_context(tmp_path, client, notifier)
    done = await wishlist.sync(ctx, ["14 octobre"], {"14 octobre": "14 octobre"})
    assert "HTTP 400" in done["error"] and done["covered"] == 0
    assert await wishlist.covered(ctx) == set()
    await ctx.db.close()


@pytest.mark.asyncio
async def test_wishlist_writes_nothing_when_simulated(tmp_path):
    from src.workers import wishlist

    wished: list[dict] = []
    calls: list = []
    client, notifier = _wishlist_client(tmp_path, _wishlist_site(wished, [], calls=calls), dry_run=True)
    ctx = await make_context(tmp_path, client, notifier)
    done = await wishlist.sync(ctx, ["14 octobre"], {"14 octobre": "14 octobre"})
    assert wished == [] and done["covered"] == 0 and {m for m, _ in calls} == {"GET"}
    await ctx.db.close()


@pytest.mark.asyncio
async def test_listings_are_read_once_and_only_for_missing_cards(tmp_path):
    from datetime import datetime, timedelta

    from src.workers import wishlist

    start = datetime.now(UTC)
    at = lambda s: (start + timedelta(seconds=s)).isoformat()
    def note(s, title, aid):
        return {"created_at": at(s), "data": {"auction_id": aid, "card_id": "x", "card_title": title}}
    notes = [note(1, "14 octobre", "a1"), note(2, "Carte du joueur", "a2"), note(3, "14 Octobre", "a1")]
    client, notifier = _wishlist_client(tmp_path, _wishlist_site([], notes))
    ctx = await make_context(tmp_path, client, notifier)

    assert await wishlist.new_listings(ctx, {"14 octobre"}, start) == ["a1"]
    assert await wishlist.new_listings(ctx, {"14 octobre"}, start) == []  # curseur avancé
    notes.append(note(4, "14 octobre", "a3"))
    assert await wishlist.new_listings(ctx, {"14 octobre"}, start) == ["a3"]  # après un redémarrage aussi : en base
    await ctx.db.close()


@pytest.mark.asyncio
async def test_sniper_follows_an_announced_sale_without_searching_its_title(tmp_path):
    from datetime import datetime, timedelta

    from src.workers import wishlist
    from src.workers.auction_sniper import AuctionSniper, snipe_params

    now = datetime.now(UTC)
    sale = {"id": "a1", "card": {"id": "c-14", "wikipedia_title": "14 octobre", "rarity": "R"},
            "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
            "end_at": (now + timedelta(hours=1)).isoformat(), "base_amount": 5, "current_bid": None,
            "current_bidder_id": None}
    notes = [{"created_at": (now + timedelta(seconds=1)).isoformat(),
              "data": {"auction_id": "a1", "card_id": "c-14", "card_title": "14 octobre"}}]
    client, notifier = _wishlist_client(tmp_path, _wishlist_site([], notes, {"a1": sale}), collection_max_price=50)
    ctx = await make_context(tmp_path, client, notifier)
    await ctx.db.kv_set("collection", {"owned": [], "missing": ["14 octobre", "2 mars"], "total": 366})
    await ctx.db.kv_set(wishlist.KV_KEY, {"covered": ["14 octobre"]})
    await ctx.db.kv_set(wishlist.CURSOR_KEY, now.isoformat())
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    await sniper._load_collection()
    assert sniper._searched() == {"2 mars"}  # annoncée par le site : plus cherchée par son titre
    await sniper._read_wishlist(snipe_params(ctx.settings, False), now)
    assert "a1" in sniper._targets
    await ctx.db.close()
