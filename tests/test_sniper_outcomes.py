"""Sniper : issue d'une vente surenchérie puis rejouée, vérification anti-bot, carte de collection gagnée."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.workers.auction_sniper import AuctionSniper
from src.workers.base import Context
from src.workers.collection import progress_lines
from tests.test_integration import make_client

RIVAL = "33333333-3333-3333-3333-333333333333"


def sale(aid: str, title: str, seconds: float, base: int = 10, bid: int | None = None,
         bidder: str | None = None) -> dict:
    return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
            "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
            "end_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
            "base_amount": base, "current_bid": bid, "current_bidder_id": bidder}


async def start(tmp_path, market: dict, replies: list, state: dict | None = None):
    """Sniper réel pour la collection (jours manquants) ; chaque mise reçoit la prochaine réponse de `replies`."""
    def handler(request):
        path = request.url.path
        if request.method == "POST":
            status, body = replies.pop(0)
            return httpx.Response(status, json=body)
        if path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3000})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        if path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if path.endswith("/sales"):
            return httpx.Response(404, json={})
        if path.startswith("/api/marketplace/"):
            return httpx.Response(200, json={"auction": market[path.rsplit("/", 1)[1]]})
        if path == "/api/marketplace":
            return httpx.Response(200, json={"auctions": list(market.values()), "hasMore": False})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, simulate=["sniper"],
                                   collection_sets=["jours"], collection_max_price=20)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.kv_set("collection", state or {"owned": [], "missing": ["14 octobre", "15 octobre"], "total": 366})
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    return sniper, db, notifier


@pytest.mark.asyncio
async def test_outbid_then_too_expensive_is_logged_and_not_rechecked(tmp_path, caplog):
    market = {"col": sale("col", "14 octobre", 15)}
    sniper, db, _ = await start(tmp_path, market, [(200, {"current_bid": 10, "bidder_balance": 2990})])
    await sniper.step()
    assert sniper._targets["col"].recheck  # misée : relue à sa fin

    # Le 15/10 (« 12 mars ») : surenchérie à 19 dans les 10 dernières secondes, donc prolongée ; la mise
    # suivante (21) dépasse le plafond de 20. Avant, l'issue restait « misée » : rien dans les logs.
    sniper._targets["col"].auction = sniper._targets["col"].auction.model_validate(sale("col", "14 octobre", -10))
    market["col"] = sale("col", "14 octobre", 15, bid=19, bidder=RIVAL)
    caplog.set_level("INFO", logger="worker.sniper")
    await sniper.step()
    assert "14 octobre prolongée" in caplog.text
    assert "collection : 14 octobre à 21 WB" in caplog.text and ": collection : trop chère" in caplog.text
    assert "col" not in sniper._targets  # plus relue à sa fin pour rien
    assert "collection : trop chère 1" in sniper.recap()[1]
    await db.close()


@pytest.mark.asyncio
async def test_antibot_refusal_alerts_once_until_a_bid_passes(tmp_path):
    market = {"a": sale("a", "14 octobre", 15)}
    refusal = (400, {"error": "Vérification anti-bot requise."})
    sniper, db, notifier = await start(tmp_path, market, [refusal, refusal,
                                                          (200, {"current_bid": 10, "bidder_balance": 2990})])
    await sniper.step()
    assert ("CRITICAL", "Sniper : vérification anti-bot demandée") in notifier.sent
    assert sniper.blocked and "vérification anti-bot" in sniper.blocked
    assert "mise refusée : vérification anti-bot 1" in sniper.recap()[1]

    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()  # encore refusée : pas de deuxième alerte
    assert notifier.sent.count(("CRITICAL", "Sniper : vérification anti-bot demandée")) == 1

    market["c"] = sale("c", "15 octobre", 15)
    await sniper.step()
    assert ("SUCCESS", "Sniper : vérification levée") in notifier.sent and sniper.blocked is None
    await db.close()


@pytest.mark.asyncio
async def test_won_collection_card_counts_at_once(tmp_path):
    state = {"owned": ["1 janvier"], "missing": ["14 octobre", "15 octobre"], "total": 366,
             "sets": {"Jours de l'année": [1, 366]}, "first_owned": {"1 janvier": None}}
    sniper, db, _ = await start(tmp_path, {}, [], state)
    await db.record_bid(auction_id="col", card_id="card-14 octobre", title="14 octobre", rarity="R", is_shiny=False,
                        amount=13, reference_price=None, reason="collection : carte manquante ≤ 20", night=False,
                        dry_run=False, status="placed", end_at=datetime.now(timezone.utc))
    row = await db.last_bid("col")
    await sniper._close_bid(row, won=True, simulated=False)

    # Une autre vente du même jour n'est plus misée, et le récap la compte sans attendre la lecture (6 h).
    assert "14 octobre" not in sniper._collection_missing
    saved = await db.kv_get("collection")
    assert saved["missing"] == ["15 octobre"] and "14 octobre" in saved["owned"]
    lines = progress_lines(saved, {}, datetime.now(timezone.utc))
    assert lines[0] == "Jours de l'année : 2/366 (0,5 %)" and lines[1].startswith("+1 en 7 jours : 14 octobre")
    await db.close()
