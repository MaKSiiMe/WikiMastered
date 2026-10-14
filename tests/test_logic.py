from __future__ import annotations

import base64
import json
import random
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from src.config import Settings
from src.core.client import user_id_from_cookie
from src.core.endpoints import Endpoints
from src.core.models import Auction, CollectionPage, OwnedCard, PackOpenResult, min_next_bid
from src.core.pricing import Quote
from src.core.text import normalize_title
from src.core.timeutils import PARIS, in_window
from src.tools.parse_curl import parse_curl, to_endpoint
from src.workers.auction_sniper import decide, in_snipe_window, snipe_params
from src.workers.inventory_manager import (
    achievement_guard, group_by_card, plan_listing_time, select_for_discard, select_for_sale,
)
from src.workers.pack_opener import PackState, seconds_until_stock
from src.workers.wiki_trends import detect_spike, expected_rarity

ROOT = Path(__file__).resolve().parents[1]
ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def auction(**kw) -> Auction:
    data = {
        "id": "33333333-3333-3333-3333-333333333333",
        "card": {"id": "44444444-4444-4444-4444-444444444444", "wikipedia_title": "Ada Lovelace", "rarity": "SR"},
        "seller_id": OTHER, "status": "active", "end_at": (NOW + timedelta(seconds=10)).isoformat(),
        "base_amount": 5, "current_bid": None, "current_bidder_id": None,
    }
    data.update(kw)
    return Auction.model_validate(data)


def owned(oid: str, card_id: str, rarity: str = "C", **kw) -> OwnedCard:
    return OwnedCard.model_validate({"id": oid, "card": {"id": card_id, "wikipedia_title": card_id, "rarity": rarity}, **kw})


# ------------------------------------------------------------------ endpoints
def test_endpoints_render_types_and_drop_empty_params():
    eps = Endpoints.load(ROOT / "config" / "endpoints.yaml")
    bid = eps.build("place_bid", auction_id="abc", amount=12)
    assert bid.url == "https://www.wiki-masters.com/api/marketplace/abc/bid"
    assert bid.json == {"amount": 12} and bid.is_mutation
    market = eps.build("market_page")
    assert market.params == {"page": "1", "limit": "50", "sort": "ending_soon"}
    discard = eps.build("bulk_discard", owned_card_ids=["a", "b"])
    assert discard.json == {"card_ids": ["a", "b"]}


# --------------------------------------------------------------------- cookie
def test_user_id_from_chunked_supabase_cookie():
    session = json.dumps({"access_token": "x", "user": {"id": ME.upper()}})
    encoded = "base64-" + base64.urlsafe_b64encode(session.encode()).decode().rstrip("=")
    cookie = f"other=1; sb-abcdef-auth-token.0={encoded[:30]}; sb-abcdef-auth-token.1={encoded[30:]}"
    assert user_id_from_cookie(cookie) == ME
    assert user_id_from_cookie("sb-abcdef-auth-token=base64-@@@") is None


# --------------------------------------------------------------------- models
def test_min_bid_matches_site_rule():
    assert min_next_bid(None, 7) == 7
    assert min_next_bid(10, 7) == 11
    assert min_next_bid(25, 7) == 28  # ceil(27.5)


def test_invalid_rows_are_skipped():
    page = CollectionPage.model_validate({"collection": [{"id": "x", "card": {"id": "c"}}, {"oops": 1}]})
    assert [r.id for r in page.rows()] == ["x"]


def test_pack_pairs_shiny_copy():
    pack = PackOpenResult.model_validate({
        "cards": [{"id": "a", "wikipedia_title": "A", "rarity": "C"}, {"id": "a", "wikipedia_title": "A", "rarity": "C"}],
        "packs_remaining": 3,
        "owned_copies": [{"id": "o1", "card_id": "a", "is_shiny": False}, {"id": "o2", "card_id": "a", "is_shiny": True}],
    })
    assert [(c.id, copy.id, copy.is_shiny) for c, copy in pack.pulled()] == [("a", "o1", False), ("a", "o2", True)]


# ---------------------------------------------------------------------- packs
def test_pack_wait_from_last_regen():
    state = PackState(remaining=0, last_regen_at=NOW - timedelta(minutes=4))
    assert seconds_until_stock(state, 1, timedelta(minutes=10), NOW) == pytest.approx(360)
    assert seconds_until_stock(PackState(), 1, timedelta(minutes=10), NOW) == 0


# --------------------------------------------------------------------- sniper
def test_rarity_cap_and_deal_rules():
    s = settings(sniper_rarity_caps={"SR": 15}, sniper_deal_ratio_max=0.45)
    params = snipe_params(s, night=False)
    assert decide(auction(base_amount=12), params, None, 3).ceiling == 15
    assert decide(auction(base_amount=20), params, None, 3) is None
    # C sans plafond : seul l'écart de cote compte.
    common = auction(card={"id": "c", "wikipedia_title": "x", "rarity": "C"}, base_amount=9)
    assert decide(common, params, Quote(20.0, 5), 3).ceiling == 9      # floor(20 × 0,45) = 9
    assert decide(common, params, Quote(19.0, 5), 3) is None          # 8 < 9
    assert decide(common, params, Quote(40.0, 1), 3) is None          # pas assez de ventes


def test_night_mode_is_more_aggressive():
    s = settings()
    day, night = snipe_params(s, False), snipe_params(s, True)
    assert night.deal_ratio > day.deal_ratio and night.window_seconds > day.window_seconds
    assert night.rarity_caps["UR"] > day.rarity_caps["UR"] and night.budget > day.budget


def test_never_bid_on_own_or_leading_auction():
    assert in_snipe_window(auction(), ME, 20, NOW)
    assert not in_snipe_window(auction(seller_id=ME), ME, 20, NOW)
    assert not in_snipe_window(auction(current_bid=5, current_bidder_id=ME), ME, 20, NOW)
    assert not in_snipe_window(auction(end_at=(NOW + timedelta(seconds=60)).isoformat()), ME, 20, NOW)


def test_night_window_crossing_midnight():
    assert in_window(datetime(2026, 10, 14, 23, 30, tzinfo=PARIS), time(22, 0), time(2, 0))
    assert not in_window(datetime(2026, 10, 14, 12, 0, tzinfo=PARIS), time(22, 0), time(2, 0))


# ------------------------------------------------------------------ inventaire
def test_sale_and_discard_selection_keeps_one_and_protects():
    s = settings(sell_min_value=15, discard_max_value=2, discard_rarities=["C"])
    rows = [owned("a1", "A"), owned("a2", "A"), owned("a3", "A", starred=True),
            owned("b1", "B"), owned("b2", "B"), owned("b3", "B", is_shiny=True), owned("b4", "B")]
    groups = group_by_card(rows)
    assert groups["A"][0].id == "a3"  # le favori est gardé en premier
    quotes = {"A": {"C": Quote(30.0, 5)}, "B": {"C": Quote(1.0, 5)}}
    sale = select_for_sale(groups, quotes, set(), s)
    assert {c.owned.id for c in sale} == {"a1", "a2"}
    discard = select_for_discard(groups, quotes, {"b4"}, s)
    assert {c.owned.id for c in discard} == {"b1", "b2"}  # b3 shiny gardé, b4 protégé


def test_achievement_guard():
    assert achievement_guard(980, 40, [], 50, True)[0] == 0
    assert achievement_guard(980, 40, [1000], 50, True)[0] == 0          # palier proche
    assert achievement_guard(1030, 40, [1000, 2000], 50, True)[0] == 30  # ne repasse pas sous 1000
    assert achievement_guard(1500, 40, [1000, 2000], 50, True)[0] == 40
    assert achievement_guard(990, 40, [1000], 50, False)[0] == 40        # mode unique


def test_listing_ends_in_peak_window():
    now = datetime(2026, 10, 14, 15, 0, tzinfo=PARIS)
    post, minutes, end = plan_listing_time(now, time(20, 0), time(22, 30), [], 60, random.Random(1))
    assert post == now and time(20, 0) <= end.time() <= time(22, 30)
    assert abs((post + timedelta(minutes=minutes) - end).total_seconds()) < 60
    post, minutes, end = plan_listing_time(now, time(20, 0), time(22, 30), [1440, 60], 60, random.Random(1))
    assert post >= now and post + timedelta(minutes=minutes) == end and minutes == 60


# --------------------------------------------------------------------- trends
def test_spike_detection_and_rarity():
    s = settings(trends_spike_ratio=3, trends_min_daily_views=1000)
    trend = detect_spike("Ada_Lovelace", 15000, [300] * 30, s)
    assert trend and trend.ratio == pytest.approx(50)
    assert expected_rarity(trend.projected_30d, {"L": 20000}) == "L"
    assert detect_spike("X", 1500, [1000] * 30, s) is None
    assert normalize_title("Élection_présidentielle") == normalize_title("election  presidentielle")


# ------------------------------------------------------------------------ curl
def test_parse_curl_from_devtools():
    text = r"""curl 'https://www.wiki-masters.com/api/marketplace/33333333-3333-3333-3333-333333333333/bid' \
  -H 'accept: */*' \
  -H 'content-type: application/json' \
  -b 'sb-x-auth-token=abc; other=1' \
  -H 'user-agent: Mozilla/5.0 Test' \
  -H 'x-custom: 1' \
  --data-raw '{"amount":12}'"""
    parsed = parse_curl(text)
    assert parsed.method == "POST" and parsed.body == {"amount": 12}
    assert parsed.cookie == "sb-x-auth-token=abc; other=1" and parsed.extra_headers == {"x-custom": "1"}
    spec = to_endpoint(parsed, ["auction_id"])
    assert spec["path"] == "/api/marketplace/{auction_id}/bid" and spec["json"] == {"amount": 12}
