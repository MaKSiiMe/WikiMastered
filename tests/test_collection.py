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
    assert len(lookups) == 2  # A (3 exemplaires, prioritaire) : historique refusé puis moyenne, 1 seule carte
    assert quotes["C"] == {}  # ni cote ni vente observée : ignorée ce passage
    sales = select_for_sale(groups, quotes, excluded, client.settings)
    assert sorted((c.owned.catalog_id, c.price) for c in sales) == [("A", 80), ("A", 80), ("B", 65)]
    await db.close()


@pytest.mark.asyncio
async def test_live_writes_open_packs_despite_dry_run(tmp_path):
    from src.workers.pack_opener import PackOpener

    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"cards": [{"id": "c1", "wikipedia_title": "Ada", "rarity": "L"}],
                                         "packs_remaining": 0})

    client, notifier = make_client(tmp_path, handler, dry_run=True, live_writes=["open_pack"],
                                   pack_between_opens_min_seconds=0, pack_between_opens_max_seconds=0)
    assert client.settings.is_live("open_pack") and not client.settings.is_live("place_bid")
    ctx = await make_context(tmp_path, client, notifier)
    db, api = ctx.db, ctx.api
    assert (await api.place_bid("x", 5)).dry_run  # les mises restent simulées
    opener = PackOpener(ctx)
    await opener.setup()
    await opener._open_batch()
    assert sent == ["/api/packs/open"]
    assert len(await db.recent_pulls(__import__("datetime").datetime(2000, 1, 1))) == 1
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
