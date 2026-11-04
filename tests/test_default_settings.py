"""Les vraies valeurs par défaut du dépôt : prudentes, et sans aucun achat pour revendre tant que rien n'est choisi."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config import Settings
from src.core.models import Auction
from src.core.pricing import Quote
from src.workers.auction_sniper import AuctionSniper, decide, portfolio_state, snipe_params
from tests.conftest import TEST_SETTINGS
from tests.test_integration import make_client, make_context


@pytest.fixture
def defaults(monkeypatch) -> Settings:
    """Les réglages tels qu'un nouvel utilisateur les reçoit, sans ceux fixés pour les autres tests."""
    for name in TEST_SETTINGS:
        monkeypatch.delenv(name)
    return Settings(_env_file=None)


def test_nothing_is_bought_or_sent_until_chosen(defaults):
    assert defaults.dry_run
    assert defaults.sniper_resale_rarities == []
    assert defaults.collection_sets == [] and defaults.collection_titles == []
    assert not defaults.auto_list and not defaults.auto_discard


def test_a_card_can_still_be_bought_once_a_rarity_is_chosen(defaults):
    """Prudents, les défauts ne bloquent pas tout : une bonne affaire reste achetée, à la mise minimale."""
    auction = Auction.model_validate({
        "id": "a", "card": {"id": "c", "wikipedia_title": "Ada", "rarity": "SR"}, "status": "active",
        "seller_id": "22222222-2222-2222-2222-222222222222", "base_amount": 250,
        "end_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()})
    quote = Quote(800.0, 7, contested=7, spread=1.2)
    portfolio = portfolio_state(defaults, balance=3000, held_count=0, held_real_cost=0, held_sim_cost=0,
                                engaged_real=0, engaged_sim=0, bot_engaged_count=0)
    assert portfolio.position_budget == 300 and portfolio.min_profit == 150
    decision = decide(auction, snipe_params(defaults, False), quote, defaults.price_min_sales, portfolio,
                      push=defaults.sniper_bid_push)
    assert decision is not None and decision.amount == auction.min_bid and decision.ceiling == 300


@pytest.mark.asyncio
async def test_without_resale_the_sniper_reads_no_market_and_says_so(tmp_path):
    market_reads = []

    def handler(request):
        path = request.url.path
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        market_reads.append(str(request.url))
        return httpx.Response(200, json={"auctions": [], "hasMore": False})

    client, notifier = make_client(tmp_path, handler, sniper_resale_rarities=[])
    ctx = await make_context(tmp_path, client, notifier)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    for _ in range(25):
        await sniper.step()
    assert market_reads == []
    assert "Sniper : aucune revente" in {title for _, title in notifier.sent}
    assert "jours manquants" in sniper.recap()[0] and "( et" not in sniper.recap()[0]
