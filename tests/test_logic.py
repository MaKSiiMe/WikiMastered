from __future__ import annotations

import base64
import json
import math
import random
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from src.config import Settings
from src.core.client import user_id_from_cookie
from src.core.database import Database
from src.core.endpoints import Endpoints
from src.core.models import Auction, CollectionPage, OwnedCard, SaleRow, deterrent_bid, min_next_bid
from src.core.pricing import Quote, combine, recent_estimates
from src.core.text import normalize_title
from src.core.timeutils import PARIS, in_window
from src.tools.parse_curl import parse_curl, to_endpoint
from src.workers.auction_sniper import (
    decide, in_snipe_window, portfolio_state, position_cap, refusal, snipe_params, waiting_profits,
)
from src.workers.inventory_manager import (
    achievement_guard, group_by_card, plan_listing_time, select_for_discard, select_for_sale,
)

ROOT = Path(__file__).resolve().parents[1]
ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


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


def test_deterrent_bid_is_the_lowest_bid_whose_raise_reaches_the_price():
    for price in (1, 10, 11, 110, 2100, 55001):
        bid = deterrent_bid(price)
        assert min_next_bid(bid, 1) >= price and (bid == 1 or min_next_bid(bid - 1, 1) < price)
    assert deterrent_bid(110) == 100 and deterrent_bid(55001) == 50000


def test_invalid_rows_are_skipped():
    page = CollectionPage.model_validate({"collection": [{"id": "x", "card": {"id": "c"}}, {"oops": 1}]})
    assert [r.id for r in page.rows()] == ["x"]


# ---------------------------------------------------------------------- packs


# --------------------------------------------------------------------- sniper


def test_portfolio_decision_needs_real_profit():
    s = settings()  # pas de plafond par rareté par défaut
    params = snipe_params(s, night=False)
    pf = portfolio_state(s, balance=3000, held_count=0, held_real_cost=0, held_sim_cost=0,
                         engaged_real=0, engaged_sim=0, bot_engaged_count=0)
    assert pf.position_budget == 540 and pf.min_profit == 135      # 3000 × 0,9 / 5 ; 25 %
    card = {"id": "c", "wikipedia_title": "x", "rarity": "C"}
    assert decide(auction(card=card, base_amount=9), params, Quote(20.0, 5), 3, pf) is None  # +11 : trop petit
    big = decide(auction(card=card, base_amount=200), params, Quote(400.0, None), 3, pf)
    assert big and big.ceiling == 265 and big.profit == 200             # min(280, 400-135, 540)
    assert decide(auction(card=card, base_amount=300), params, Quote(400.0, 5), 3, pf) is None
    assert decide(auction(card=card, base_amount=200), params, Quote(400.0, 1), 3, pf) is None  # 1 vente


def test_position_skipped_when_our_bid_is_far_below_the_estimate():
    """17-20/10 : mises sous 40 % de l'estimation, 0 gagnée sur 33 (parties à ~2 fois notre mise). Sautées :
    la place du quota anti-bot sert à une enchère gagnable."""
    assert settings().sniper_min_bid_share == 0.50  # 31/10-01/11 : sous 50 %, 0 gagnée sur 18
    s = settings(sniper_min_bid_share=0.40)
    params = snipe_params(s, night=False)
    pf = portfolio_state(s, balance=3000, held_count=0, held_real_cost=0, held_sim_cost=0,
                         engaged_real=0, engaged_sim=0, bot_engaged_count=0)
    card = {"id": "c", "wikipedia_title": "x", "rarity": "L"}
    quote = Quote(1000.0, 5, contested=3, spread=1.5)
    # Plafond min(700, 1000 − 135, 540) = 540 ; mise 200 + ⌈340 × 0,5⌉ = 370 < 400 (40 %) : sautée.
    cheap = auction(card=card, base_amount=200)
    assert decide(cheap, params, quote, 3, pf, push=0.5) is None
    assert refusal(cheap, params, quote, 3, pf, push=0.5) == "mise < 40% de l'estimation"
    # Surenchérie : la mise suivante va au plafond (540 ≥ 400), elle part.
    assert decide(cheap, params, quote, 3, pf, push=1.0).amount == 540
    fair = decide(auction(card=card, base_amount=300), params, quote, 3, pf, push=0.5)
    assert fair and fair.amount == 420  # 300 + 120
    off = snipe_params(settings(sniper_min_bid_share=0), night=False)
    assert decide(cheap, off, quote, 3, pf, push=0.5).amount == 370


def test_position_needs_contested_and_steady_recent_sales():
    """18/10 : l'éclipse XXIe achetée 882, estimée 1197 sur 3 ventes au prix de départ et 2 disputées (970, 1774)."""
    s = settings()
    params = snipe_params(s, night=False)
    pf = portfolio_state(s, balance=3000, held_count=0, held_real_cost=0, held_sim_cost=0,
                         engaged_real=0, engaged_sim=0, bot_engaged_count=0)
    card = {"id": "c", "wikipedia_title": "x", "rarity": "L"}
    sale = auction(card=card, base_amount=200)
    assert decide(sale, params, Quote(400.0, 5, contested=3, spread=1.5), 3, pf)
    eclipse = Quote(400.0, 5, contested=2, spread=1.83)
    assert decide(sale, params, eclipse, 3, pf) is None
    assert refusal(sale, params, eclipse, 3, pf) == "trop peu de ventes disputées"
    wild = Quote(400.0, 5, contested=4, spread=2.5)
    assert decide(sale, params, wild, 3, pf) is None and refusal(sale, params, wild, 3, pf) == "ventes trop dispersées"
    loose = snipe_params(settings(sniper_min_contested_sales=0, sniper_max_price_spread=0), night=False)
    assert decide(sale, loose, wild, 3, pf) and decide(sale, loose, eclipse, 3, pf)


def test_position_bid_pushed_toward_ceiling_collection_stays_at_minimum():
    # Le 17/10 : les rivaux surenchérissent d'un pas en 20-30 s ; une mise plus haute d'emblée use moins le quota.
    s = settings()
    params = snipe_params(s, night=False)
    pf = portfolio_state(s, balance=3000, held_count=0, held_real_cost=0, held_sim_cost=0,
                         engaged_real=0, engaged_sim=0, bot_engaged_count=0)
    card = {"id": "c", "wikipedia_title": "x", "rarity": "L"}
    half = decide(auction(card=card, base_amount=200), params, Quote(400.0, None), 3, pf, push=0.5)
    assert half.amount == 233 and half.ceiling == 265 and half.profit == 167   # 200 + ⌈65 × 0,5⌉
    assert "bénéfice ~167" in half.reason
    top = decide(auction(card=card, base_amount=200), params, Quote(400.0, None), 3, pf, push=1.0)
    assert top.amount == top.ceiling == 265
    assert decide(auction(base_amount=12), params, None, 3, collection_cap=15, push=0.5).amount == 12  # minimum


def test_positions_follow_free_sale_slots():
    s = settings(portfolio_max_positions=15, portfolio_buffer=10)
    assert position_cap(s, 5, 4) == (11, 1)   # 1 place libre + 10 en file d'attente
    assert position_cap(s, 5, 5) == (10, 0)   # toutes les places prises : file d'attente seulement
    assert position_cap(s, 5, 0) == (15, 5)   # plafond absolu PORTFOLIO_MAX_POSITIONS


def test_no_position_cap_by_default():
    """31/10 : 5 positions attendaient une place, plus aucune L achetée depuis 18:40. 0 = pas de plafond."""
    cap, room = position_cap(settings(), 5, 5)
    assert room == 0 and cap > 1000
    cap, _ = position_cap(settings(portfolio_buffer=3), 5, 5)
    assert cap == 3                           # file d'attente limitée, sans plafond absolu
    assert position_cap(settings(), None, 0)[0] > 1000


def test_queued_positions_need_a_better_deal():
    s = settings(sniper_min_bid_share=0)  # 120 pour 400 : sous 40 %, règle testée à part
    params = snipe_params(s, night=False)
    card = {"id": "c", "wikipedia_title": "x", "rarity": "C"}
    quote = Quote(400.0, 5)

    def pf(open_positions: int, waiting=(300.0,)):  # une vente plus rentable attend déjà
        return portfolio_state(s, balance=3000, held_count=open_positions, held_real_cost=0, held_sim_cost=0,
                               engaged_real=0, engaged_sim=0, bot_engaged_count=0, max_positions=11, sale_room=1,
                               waiting=waiting)

    assert pf(2).required_profit == 270                                   # 50 % du budget de 540
    assert decide(auction(card=card, base_amount=200), params, quote, 3, pf(0))      # place libre : +200 suffit
    assert decide(auction(card=card, base_amount=200), params, quote, 3, pf(2)) is None  # attendrait : +200 < 270
    assert decide(auction(card=card, base_amount=120), params, quote, 3, pf(2))      # +280 : vaut l'attente


def test_queued_position_jumps_less_profitable_waiting_sales():
    """Une place libérée va à la vente la plus rentable : une position qui bat les ventes en attente sauf moins de
    `sale_room` n'attend que la prochaine place, au bénéfice minimum ; sinon, PORTFOLIO_QUEUE_PROFIT_RATIO."""
    s = settings(portfolio_min_profit=30)

    def pf(waiting, sale_room=2):
        return portfolio_state(s, balance=3000, held_count=6, held_real_cost=0, held_sim_cost=0, engaged_real=0,
                               engaged_sim=0, bot_engaged_count=0, max_positions=12, sale_room=sale_room,
                               waiting=waiting)

    assert pf(()).required_profit == 135                  # tout est en vente : la prochaine place sera la sienne
    assert pf((200.0,)).required_profit == 135            # une seule vente en attente pour 2 places
    assert pf((200.0, 90.0, 500.0)).required_profit == 201  # passer devant la 2e plus rentable (200) suffit
    assert pf((900.0, 800.0, 700.0)).required_profit == 270  # jamais plus que l'ancien seuil (50 % du budget)
    assert pf((math.inf, math.inf)).required_profit == 270   # ventes demandées sur Discord : toujours devant
    assert pf((200.0, 90.0)).queue_profit((300.0,)) == 201   # une position ouverte dans le passage compte aussi
    assert pf((), sale_room=0).required_profit == 270        # places prises par tes ventes : vraie attente


def test_waiting_profits_follow_the_sale_queue():
    rows = [{"amount": 3000, "reference_price": 4500.0, "reason": None, "base_amount": None},
            {"amount": None, "reference_price": None, "reason": "cote 1400", "base_amount": 1407},
            {"amount": None, "reference_price": None, "reason": "vente manuelle (Discord)", "base_amount": 500}]
    assert waiting_profits(rows, 0.0) == (1500.0, 1407.0, math.inf)
    assert waiting_profits(rows[:1], 0.1) == (4500 * 0.9 - 3000,)


def test_portfolio_counts_engaged_capital():
    s = settings()
    pf = portfolio_state(s, balance=1000, held_count=4, held_real_cost=1500, held_sim_cost=0,
                         engaged_real=500, engaged_sim=0, bot_engaged_count=2)
    # Le capital engagé compte : budget calculé sur 3000, pas sur le solde de 1000.
    assert pf.capital == 3000 and pf.position_budget == 540 and pf.available == 950
    assert pf.open_positions == 6 and pf.can_open             # 4 positions + 2 enchères < 15
    assert not portfolio_state(settings(portfolio_max_positions=6), balance=1000, held_count=4, held_real_cost=0,
                               held_sim_cost=0, engaged_real=0, engaged_sim=0, bot_engaged_count=2).can_open


def test_night_mode_is_more_aggressive():
    s = settings(sniper_daily_budget=300, night_budget_bonus=200)
    day, night = snipe_params(s, False), snipe_params(s, True)
    assert night.deal_ratio > day.deal_ratio and night.window_seconds == day.window_seconds  # même timing de mise
    assert night.budget > day.budget
    assert snipe_params(settings(), True).budget == 0  # pas de budget quotidien par défaut


def test_never_bid_on_own_or_leading_auction():
    assert in_snipe_window(auction(), ME, 20, NOW)
    assert not in_snipe_window(auction(seller_id=ME), ME, 20, NOW)
    assert not in_snipe_window(auction(current_bid=5, current_bidder_id=ME), ME, 20, NOW)
    assert not in_snipe_window(auction(end_at=(NOW + timedelta(seconds=60)).isoformat()), ME, 20, NOW)


def test_night_window_crossing_midnight():
    assert in_window(datetime(2026, 10, 14, 23, 30, tzinfo=PARIS), time(22, 0), time(2, 0))
    assert not in_window(datetime(2026, 10, 14, 12, 0, tzinfo=PARIS), time(22, 0), time(2, 0))


# ------------------------------------------------------------------ inventaire
def test_recent_sales_median_beats_stale_average():
    rows = [SaleRow(final_price=p, settled_at=NOW - timedelta(days=i)) for i, p in enumerate([500, 480, 30, 520, 510, 20, 15])]
    assert recent_estimates(rows, 5)["*"] == (500.0, 7)  # 5 dernières : 500 480 30 520 510


def test_observed_sales_cap_the_stale_site_average():
    assert combine([157], 1, 391.0) == Quote(157.0, 1)           # cas réel GMMTV
    assert combine([500, 480, 520], 3, 300.0) == Quote(300.0, 3)  # plafonné par prudence
    assert combine([], 0, 391.0) == Quote(391.0, 0)               # sans observation : pas fiable
    assert not combine([], 0, 391.0).reliable(2)


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


def test_last_copy_rarities_sold_except_bot_buys():
    """SELL_LAST_COPY_RARITIES=L : une L de paquet seule se vend ; jamais une carte achetée par le bot, un favori, un
    shiny, un exemplaire protégé, ni une rareté hors liste."""
    s = settings(sell_min_value=50, sell_last_copy_rarities=["l"])
    rows = [owned("p1", "P", "L"), owned("s1", "S", "L"), owned("f1", "F", "L", starred=True),
            owned("t1", "T", "L"), owned("r1", "R", "SR"), owned("m1", "M", "L"), owned("m2", "M", "C")]
    groups = group_by_card(rows)
    quotes = {cid: {"L": Quote(600.0, 5), "SR": Quote(300.0, 5), "C": Quote(80.0, 5)} for cid in "PSFTRM"}
    sale = select_for_sale(groups, quotes, {"t1"}, s, bought={"S"})
    assert {c.owned.id for c in sale} == {"p1", "m1"}  # M : la L se vend, la C est gardée à sa place
    assert {c.owned.id for c in select_for_sale(groups, quotes, set(), settings(sell_min_value=50))} == {"m2"}


def test_last_copy_discard_needs_a_known_worthless_card():
    """DISCARD_LAST_COPY_RARITIES=C,PC : le dernier exemplaire part si la carte est cotée sans vente au-dessus de
    DISCARD_MAX_VALUE ; jamais une carte pas encore cotée, achetée par le bot, un favori, ni une rareté hors liste
    (gardée à la place de la C)."""
    s = settings(discard_max_value=2, discard_last_copy_rarities=["c", "pc"])
    rows = [owned("n1", "N"), owned("n2", "N"), owned("v1", "V"), owned("u1", "U"), owned("b1", "B"),
            owned("f1", "F", starred=True), owned("r1", "R", "R"), owned("r2", "R", "C"), owned("p1", "P", "PC"),
            owned("x1", "X")]
    groups = group_by_card(rows)
    known = {"N": None, "V": 40.0, "B": None, "F": None, "R": 1.0, "P": 2.0, "X": None}
    chosen = select_for_discard(groups, {}, {"x1"}, s, known, bought={"B"})
    assert {c.owned.id for c in chosen} == {"n1", "n2", "r2", "p1"}  # U jamais cotée : attend l'analyse
    assert select_for_discard(groups, {}, set(), settings(discard_max_value=2), known) == []  # réglage vide : doublons seuls


@pytest.mark.asyncio
async def test_known_values_mix_site_frozen_and_observed(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.price_put("never_sold", {})
    await db.price_put("cheap", {"C": (1.0, 3), "R": (2.0, 1)})
    await db.price_put("seen_high", {})
    await db.freeze_values("frozen", {"C": 1.5})
    end = datetime.now(UTC) - timedelta(minutes=5)
    await db.upsert_auctions([{"auction_id": "a1", "card_id": "seen_high", "title": "t", "rarity": "L", "is_shiny": 0,
                               "current_bid": 300, "base_amount": 100, "min_bid": 330, "end_at": end.isoformat(),
                               "seen_at": (end - timedelta(seconds=5)).isoformat(), "is_gif": 0}])
    assert await db.harvest_ended_auctions(datetime.now(UTC)) == 1
    assert await db.known_values() == {"never_sold": None, "cheap": 2.0, "seen_high": 300, "frozen": 1.5}
    await db.close()


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
    morning = datetime(2026, 10, 14, 10, 0, tzinfo=PARIS)
    site = [10, 30, 60, 180, 360, 720, 1440]  # durées du formulaire de vente du site
    post, minutes, end = plan_listing_time(morning, time(20, 0), time(22, 30), site, 60, random.Random(1))
    assert minutes == 360 and post == end - timedelta(hours=6) and time(20, 0) <= end.time() <= time(22, 30)


# --------------------------------------------------------------------- trends


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


def test_never_bid_in_the_last_ten_seconds():
    """Une mise dans les 10 dernières secondes prolonge l'enchère de 60 s : fenêtre 20 s → 11 s."""
    ending = lambda s: auction(end_at=(NOW + timedelta(seconds=s)).isoformat())  # noqa: E731
    assert in_snipe_window(ending(18), ME, 20, NOW, min_left=11)
    assert not in_snipe_window(ending(10), ME, 20, NOW, min_left=11)
    assert not in_snipe_window(ending(25), ME, 20, NOW, min_left=11)
    s = settings()
    assert snipe_params(s, night=True).window_seconds == snipe_params(s, night=False).window_seconds == 60
    assert s.sniper_min_seconds_left == 15  # 16/10 : fenêtre élargie à 60 s → 15 s


def test_every_list_setting_accepts_csv_from_the_environment(monkeypatch):
    """PROTECTED_TAGS=date (un seul élément, sans crochets) faisait planter le démarrage."""
    from typing import get_origin

    from src.config import Settings

    lists = [name for name, f in Settings.model_fields.items() if get_origin(f.annotation) is list]
    assert {"protected_tags", "collection_sets", "simulate", "live_writes", "achievement_milestones"} <= set(lists)
    monkeypatch.setenv("PROTECTED_TAGS", "date")
    monkeypatch.setenv("SIMULATE", "sniper, collection")
    monkeypatch.setenv("ACHIEVEMENT_MILESTONES", "100,250")
    monkeypatch.setenv("COLLECTION_TITLES", '["Paris", "Lyon"]')
    s = Settings(_env_file=None)
    assert s.protected_tags == ["date"] and s.simulate == ["sniper", "collection"]
    assert s.achievement_milestones == [100, 250] and s.collection_titles == ["Paris", "Lyon"]


@pytest.mark.parametrize("value, expected", [
    ("0", {}), ("", {}), ("{}", {}),  # 31/10 : HTTP_WORKER_SPACING_SECONDS=0 empêchait le bot de démarrer
    ("cotes=5, etiquettes=3", {"cotes": 5.0, "etiquettes": 3.0}), ('{"cotes": 5}', {"cotes": 5.0}),
])
def test_worker_spacing_accepts_csv_json_or_nothing(monkeypatch, value, expected):
    monkeypatch.setenv("HTTP_WORKER_SPACING_SECONDS", value)
    assert Settings(_env_file=None).http_worker_spacing_seconds == expected


def test_refusal_names_the_blocking_rule():
    from src.core.models import Auction
    from src.workers.auction_sniper import Portfolio, refusal, snipe_params

    params = snipe_params(Settings(_env_file=None), night=False)
    portfolio = Portfolio(capital=3000, available=2950, position_budget=540, min_profit=135, open_positions=0,
                          max_positions=15)

    def auction(base):
        return Auction.model_validate({"id": "a", "card": {"id": "c", "wikipedia_title": "t", "rarity": "C"},
                                       "end_at": "2026-10-14T20:00:00+00:00", "base_amount": base})

    assert refusal(auction(10), params, None, 2, portfolio) == "pas assez vue vendue"
    assert refusal(auction(10), params, Quote(300.0, 1), 2, portfolio) == "pas assez vue vendue"
    assert refusal(auction(250), params, Quote(300.0, 3), 2, portfolio) == "mise > 70% de l'estimation"
    assert refusal(auction(200), params, Quote(300.0, 3), 2, portfolio) == "bénéfice attendu trop faible"
    assert refusal(auction(600), params, Quote(2000.0, 3), 2, portfolio) == "au-dessus du budget d'une position"
