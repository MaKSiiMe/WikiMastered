"""Sniper : issue d'une vente surenchérie puis rejouée, vérification anti-bot, carte de collection gagnée."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.database import Database
from src.core.pricing import PriceService
from src.core.models import Auction
from src.workers.auction_sniper import AuctionSniper, Decision, Target, position_cap
from src.workers.base import Context
from src.workers.collection import progress_lines
from tests.test_integration import ME, make_client, seed_observed

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
            aid = path.rsplit("/", 1)[1]
            if aid not in market:
                return httpx.Response(404, json={"error": "Enchère introuvable"})
            return httpx.Response(200, json={"auction": market[aid]})
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
    replies = [refusal, refusal, (200, {"current_bid": 10, "bidder_balance": 2990})]
    sniper, db, notifier = await start(tmp_path, market, replies)
    await sniper.step()
    assert ("CRITICAL", "Sniper : vérification anti-bot demandée") in notifier.sent
    assert sniper.blocked and "vérification anti-bot" in sniper.blocked
    assert "mise refusée : vérification anti-bot 1" in sniper.recap()[1]

    # Le 16/10 : ~45 mises refusées en 1 h 30. Tant que la vérification attend, pas de mise avant ANTIBOT_RETRY.
    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()
    assert len(replies) == 2 and "pas tentée : vérification anti-bot en attente 1" in sniper.recap()[1]

    sniper._antibot_retry_at = datetime.now(timezone.utc)
    market["c"] = sale("c", "15 octobre", 15)
    await sniper.step()  # encore refusée : pas de deuxième alerte
    assert len(replies) == 1
    assert notifier.sent.count(("CRITICAL", "Sniper : vérification anti-bot demandée")) == 1

    sniper._antibot_retry_at = datetime.now(timezone.utc)
    market["d"] = sale("d", "15 octobre", 15)
    await sniper.step()
    assert ("SUCCESS", "Sniper : vérification levée") in notifier.sent and sniper.blocked is None
    await db.close()


@pytest.mark.asyncio
async def test_antibot_alert_is_renewed_every_hour_while_pending(tmp_path):
    market = {"a": sale("a", "14 octobre", 15)}
    refusal = (400, {"error": "Vérification anti-bot requise."})
    sniper, db, notifier = await start(tmp_path, market, [refusal, refusal, refusal])
    reminder = ("CRITICAL", "Sniper : vérification anti-bot toujours en attente")
    await sniper.step()
    assert reminder not in notifier.sent  # la première alerte vient d'être envoyée

    # Une mise pas tentée (vérification en attente) avant l'heure : pas de rappel, mais elle est comptée.
    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()
    assert reminder not in notifier.sent and sniper._antibot_missed == 2

    sniper._antibot_since -= timedelta(hours=1, minutes=5)
    sniper._antibot_remind_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    sniper._antibot_retry_at = datetime.now(timezone.utc)
    market["c"] = sale("c", "15 octobre", 15)
    await sniper.step()
    assert notifier.sent.count(reminder) == 1 and sniper._antibot_missed == 3

    market["d"] = sale("d", "15 octobre", 15)
    await sniper.step()  # pas de nouveau rappel avant une heure
    assert notifier.sent.count(reminder) == 1
    await db.close()


@pytest.mark.asyncio
async def test_collection_bids_capped_per_verification(tmp_path):
    # Le 17/10 : chaque vérification laisse passer 9-10 mises ; la collection n'en prend que 2, le reste va aux L.
    state = {"owned": [], "missing": ["14 octobre", "15 octobre", "16 octobre"], "total": 366}
    ok = (200, {"current_bid": 10, "bidder_balance": 2990})
    market = {"a": sale("a", "14 octobre", 15)}
    replies = [ok, ok]
    sniper, db, _ = await start(tmp_path, market, replies, state)
    await sniper.step()
    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()
    assert not replies and sniper._collection_since_check == 2

    market["c"] = sale("c", "16 octobre", 15)
    await sniper.step()  # pas de requête : la mise aurait levé une erreur (plus de réponse prévue)
    assert "collection : quota de la vérification gardé pour les L 1" in sniper.recap()[1]

    # Nouvelle vérification demandée : le quota de la collection repart à zéro.
    replies.append((400, {"error": "Vérification anti-bot requise."}))
    await sniper._bid(Auction.model_validate(sale("d", "16 octobre", 15)),
                      Decision(10, 20, "collection : carte manquante ≤ 20", None), sniper_params(sniper.api.client))
    assert sniper._collection_since_check == 0 and sniper.blocked
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


@pytest.mark.asyncio
async def test_vanished_auction_closes_the_bid_without_failing(tmp_path):
    """Récap du 16/10 à 00:00 : « auction_detail: Enchère introuvable » faisait échouer le passage du sniper."""
    sniper, db, notifier = await start(tmp_path, {}, [])
    ended = datetime.now(timezone.utc) - timedelta(minutes=5)
    for aid, dry_run, status in (("gone-sim", True, "simulated"), ("gone-real", False, "placed")):
        await db.record_bid(auction_id=aid, card_id="card-x", title="Le Parrain", rarity="L", is_shiny=False,
                            amount=150, reference_price=550, reason="revente ~550 WB", night=False,
                            dry_run=dry_run, status=status, end_at=ended)
    await sniper._settle()  # aucune exception
    assert (await db.last_bid("gone-sim"))["status"] == "sim_lost"
    assert (await db.last_bid("gone-real"))["status"] == "lost"
    assert notifier.sent.count(("WARNING", "Mise à vérifier")) == 1  # seulement pour la mise réelle
    await db.close()


@pytest.mark.asyncio
async def test_missed_collection_card_counted_apart(tmp_path):
    """Récap du 16/10 : 1933 « pas relue à temps » sur 2091, sans savoir combien de cartes de collection."""
    sniper, db, _ = await start(tmp_path, {}, [])
    now = datetime.now(timezone.utc)
    for aid, title in (("col", "14 octobre"), ("ur", "Miley Cyrus")):
        sniper._targets[aid] = Target(Auction.model_validate(sale(aid, title, 5)), now)
    sniper._prune(now)
    assert sniper._outcomes == {"col": "collection : pas relue à temps", "ur": "pas relue à temps"}
    counted = sniper.recap()[1].split(" : ", 1)[1].split(" · ")
    assert sorted(counted) == ["collection : pas relue à temps 1", "pas relue à temps 1"]
    await db.close()


@pytest.mark.parametrize("simulate, open_positions", [([], 1), (["sniper"], 7)])
@pytest.mark.asyncio
async def test_live_sniper_ignores_simulated_positions_in_its_cap(tmp_path, simulate, open_positions):
    """Passage en réel le 16/10 avec PORTFOLIO_MAX_POSITIONS=5 : les 6 positions simulées ne bloquent pas les achats."""
    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, simulate=simulate, portfolio_max_positions=5,
                                   sniper_max_bid=500)
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(timezone.utc)
    for i in range(6):
        await db.record_bid(auction_id=f"sim{i}", card_id=f"c{i}", title=f"Carte {i}", rarity="L", is_shiny=False,
                            amount=200, reference_price=600, reason="revente ~600 WB", night=False, dry_run=True,
                            status="sim_won", end_at=now)
    await db.record_bid(auction_id="real", card_id="cr", title="Réelle", rarity="L", is_shiny=False, amount=400,
                        reference_price=900, reason="revente ~900 WB", night=False, dry_run=False, status="won",
                        end_at=now)
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    portfolio = await sniper.portfolio(now)
    assert portfolio.open_positions == open_positions and portfolio.can_open == (open_positions < 5)
    await db.close()


@pytest.mark.asyncio
async def test_no_rebuy_once_max_buys_reached(tmp_path):
    """SNIPER_MAX_BUYS=5 : après 5 achats réels, plus aucune position, même revendues ; la collection continue."""
    def handler(request):
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, portfolio_max_positions=5, sniper_max_buys=5)
    db = Database(tmp_path / "t.db")
    await db.connect()
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()
    now = datetime.now(timezone.utc)
    for i in range(5):
        await db.record_bid(auction_id=f"r{i}", card_id=f"c{i}", title=f"Carte {i}", rarity="L", is_shiny=False,
                            amount=400, reference_price=900, reason="revente ~900 WB", night=False, dry_run=False,
                            status="won", end_at=now)
        if i < 4:
            assert (await sniper.portfolio(now)).can_open
        await db._exec("UPDATE bids SET closed = 1, sale_price = 700")  # revendues : places libérées
    portfolio = await sniper.portfolio(now)
    assert portfolio.open_positions == 0 and not portfolio.can_open and sniper._buys_left == 0
    # Une L bien cotée, pas chère : suivie tant qu'il reste des achats, plus suivie ensuite (relectures épargnées).
    await seed_observed(db, "card-m", [900, 880, 920], rarity="L")
    market_l = Auction.model_validate({**sale("x", "Miley Cyrus", 600), "card": {"id": "card-m",
                                       "wikipedia_title": "Miley Cyrus", "rarity": "L"}})
    assert not await sniper._worth_watching(market_l, sniper_params(client), now)
    sniper._buys_left = 1
    assert await sniper._worth_watching(market_l, sniper_params(client), now)
    await db.close()


def sniper_params(client):
    from src.workers.auction_sniper import snipe_params
    return snipe_params(client.settings, night=False)


@pytest.mark.asyncio
async def test_shadow_sniper_simulates_what_the_live_one_skips(tmp_path):
    """Sniper réel limité à 500 WB par carte, sniper de test en plus : la L à 300 est misée pour de vrai, celle
    à 550 (au-dessus du plafond du test) est simulée, sans message Discord pour la simulation."""
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(request.url.path)
            return httpx.Response(200, json={"current_bid": 300, "bidder_balance": 3000})
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, sniper_resale_rarities=["L"], sniper_max_bid=500,
                                   portfolio_max_positions=5, sniper_max_buys=5, sniper_shadow=True,
                                   notify_simulated=False, notify_each_bid=True)
    db = Database(tmp_path / "t.db")
    await db.connect()
    for card in ("card-a", "card-b"):
        await seed_observed(db, card, [1500, 1450, 1550], rarity="L")
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()

    def l_sale(aid, card, base):
        return Auction.model_validate({**sale(aid, "x", 15, base=base),
                                       "card": {"id": card, "wikipedia_title": card, "rarity": "L"}})

    now = datetime.now(timezone.utc)
    cheap, dear = l_sale("cheap", "card-a", 300), l_sale("dear", "card-b", 550)  # budget d'une position : ~594
    await sniper._play([cheap, dear], sniper_params(client), now)
    assert posted == ["/api/marketplace/cheap/bid"]
    assert (await db.last_bid("cheap"))["status"] == "placed"
    assert (await db.last_bid("dear"))["status"] == "simulated"
    assert sniper._outcomes == {"cheap": "misée", "dear": "simulée (sniper de test)"}
    assert ("INFO", "Enchère placée") in notifier.sent and ("INFO", "Enchère simulée") not in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_position_bid_pushed_then_straight_to_ceiling(tmp_path):
    """17/10 : une L misée au minimum était surenchérie d'un pas en 20-30 s, et chaque remise usait le quota
    de la vérification anti-bot. Première mise : minimum + la moitié de l'écart ; surenchérie : le plafond."""
    posted: list[int] = []
    refusals: list[dict] = []

    def handler(request):
        if request.method == "POST":
            amount = json.loads(request.content)["amount"]
            posted.append(amount)
            if refusals:
                return httpx.Response(400, json=refusals.pop(0))
            return httpx.Response(200, json={"current_bid": amount, "bidder_balance": 3000})
        if request.url.path == "/api/wikibidous":
            return httpx.Response(200, json={"balance": 3300})
        if request.url.path == "/api/marketplace/mine":
            return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5})
        if "mine" in request.url.params:
            return httpx.Response(200, json={"bidding": [], "selling": []})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False, sniper_resale_rarities=["L"],
                                   sniper_max_bid=500, sniper_bid_push=0.5)
    db = Database(tmp_path / "t.db")
    await db.connect()
    for card in ("card-a", "card-b"):
        await seed_observed(db, card, [1500, 1450, 1550], rarity="L")
    api = WikiMastersAPI(client)
    sniper = AuctionSniper(Context(client.settings, client, api, db, notifier, PriceService(api, db, 30)))
    await sniper.setup()

    def l_sale(aid, card, base, bid=None):
        return Auction.model_validate({**sale(aid, "x", 15, base=base, bid=bid, bidder=RIVAL if bid else None),
                                       "card": {"id": card, "wikipedia_title": card, "rarity": "L"}})

    now = datetime.now(timezone.utc)
    await sniper._play([l_sale("a", "card-a", 300)], sniper_params(client), now)
    assert posted == [400]  # plafond 500 (SNIPER_MAX_BID) : 300 + ⌈200 × 0,5⌉

    await db.update_bid((await db.last_bid("a"))["id"], status="outbid")  # un rival est passé devant
    sniper._engaged.pop("a", None)
    await sniper._play([l_sale("a", "card-a", 300, bid=420)], sniper_params(client), now)
    assert posted == [400, 500]  # droit au plafond

    # Un rival mise entre la lecture et la mise : la mise repart au-dessus du nouveau minimum, pas au minimum.
    refusals.append({"error": "Mise trop basse (minimum 450 wikibidous)", "min": 450})
    await sniper._play([l_sale("b", "card-b", 300)], sniper_params(client), now)
    assert posted[2:] == [400, 475]  # 450 + ⌈50 × 0,5⌉
    assert "bénéfice ~" in (await db.last_bid("b"))["reason"]
    await db.close()


def ended(aid: str, title: str, *, winner: str, final: int, minutes_ago: float = 5) -> dict:
    return {**sale(aid, title, -60 * minutes_ago, bid=final, bidder=winner), "status": "settled_sold",
            "winner_id": winner, "final_price": final, "card": {"id": f"card-{title}", "wikipedia_title": title,
                                                                  "rarity": "L"}}


async def outbid_bid(db, aid: str, title: str, amount: int, minutes_ago: float = 5) -> int:
    return await db.record_bid(auction_id=aid, card_id=f"card-{title}", title=title, rarity="L", is_shiny=False,
                               amount=amount, reference_price=1900, reason="revente ~1900 WB, bénéfice ~1400",
                               night=False, dry_run=False, status="outbid", ceiling=500,
                               end_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


@pytest.mark.asyncio
async def test_outbid_bid_settled_at_the_end_won_becomes_a_position(tmp_path):
    """17/10 : Shakira « perdue » à 479, notre propre mise. Une mise surenchérie est relue à la fin de l'enchère :
    gagnée, elle devient une position ; perdue, elle prend le prix final du serveur."""
    market = {"w": ended("w", "Shakira", winner=ME, final=479), "l": ended("l", "Arte", winner=RIVAL, final=520)}
    sniper, db, notifier = await start(tmp_path, market, [])
    await outbid_bid(db, "w", "Shakira", 479)
    await outbid_bid(db, "l", "Arte", 442)
    await sniper._settle()
    won, lost = await db.last_bid("w"), await db.last_bid("l")
    assert won["status"] == "won" and won["final_price"] == 479
    assert (await db.positions_summary())["real_count"] == 1
    assert ("SUCCESS", "Position achetée !") in notifier.sent
    assert lost["status"] == "lost" and lost["final_price"] == 520
    await db.close()


@pytest.mark.asyncio
async def test_old_or_vanished_outbid_bids_settle_quietly(tmp_path):
    # Historique ancien : pas relu. Disparue du site : close comme perdue, sans alerte « Mise à vérifier ».
    sniper, db, notifier = await start(tmp_path, {"old": ended("old", "Arte", winner=ME, final=400)}, [])
    await outbid_bid(db, "old", "Arte", 400, minutes_ago=3 * 24 * 60)
    await outbid_bid(db, "gone", "Tapie", 445)
    await sniper._settle()
    assert (await db.last_bid("old"))["status"] == "outbid"
    assert (await db.last_bid("gone"))["status"] == "lost"
    assert not any(title == "Mise à vérifier" for _, title in notifier.sent)
    await db.close()


@pytest.mark.asyncio
async def test_outbid_needs_a_known_rival_above_our_bid(tmp_path):
    sniper, db, _ = await start(tmp_path, {}, [])
    bid_id = await db.record_bid(auction_id="a", card_id="card-x", title="x", rarity="L", is_shiny=False, amount=479,
                                 reference_price=1900, reason="revente", night=False, dry_run=False, status="placed",
                                 end_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    sniper._leading["a"] = (bid_id, 479)
    # Meneur absent, puis lecture du marché plus ancienne que notre mise (rival affiché sous notre prix) : rien.
    for view in (sale("a", "x", 30, bid=479), sale("a", "x", 30, bid=436, bidder=RIVAL)):
        await sniper._track_outbids([Auction.model_validate(view)], ME)
        assert (await db.last_bid("a"))["status"] == "placed" and "a" in sniper._leading
    await sniper._track_outbids([Auction.model_validate(sale("a", "x", 30, bid=527, bidder=RIVAL))], ME)
    assert (await db.last_bid("a"))["status"] == "outbid" and "a" not in sniper._leading
    await db.close()


@pytest.mark.asyncio
async def test_null_selling_count_does_not_crash_the_sniper(tmp_path):
    """Le 16/10 (site surchargé) : sellingCount absent, `None - int` faisait planter chaque passage pendant 5 min."""
    sniper, db, _ = await start(tmp_path, {}, [])
    sniper._sale_slots = (datetime.now(timezone.utc), 5, None)
    cap, room = await sniper._sale_capacity(datetime.now(timezone.utc))
    assert (cap, room) == position_cap(sniper.settings, 5, 0)
    await db.close()


@pytest.mark.asyncio
async def test_leading_bid_outbid_before_the_last_seconds_is_replayed(tmp_path, caplog):
    """Rapport du 16/10 : surenchérie avant les 10 dernières secondes (pas de prolongation), la mise n'était relue
    qu'après la fin et la vente partait sous notre plafond. La mise en tête est relue avant la fin et rejouée."""
    market = {"col": sale("col", "14 octobre", 15)}
    sniper, db, _ = await start(tmp_path, market, [(200, {"current_bid": 10, "bidder_balance": 2990}),
                                                   (200, {"current_bid": 14, "bidder_balance": 2986})])
    await sniper.step()
    target = sniper._targets["col"]
    assert target.recheck and "col" in sniper._leading

    # Un rival mise 12 à 20 s de la fin : la dernière relecture le voit, et le bot remise 14 (12 × 1,1).
    target.auction = target.auction.model_validate(sale("col", "14 octobre", 20))
    market["col"] = sale("col", "14 octobre", 20, bid=12, bidder=RIVAL)
    caplog.set_level("INFO", logger="worker.sniper")
    await sniper.step()
    assert "14 octobre surenchérie à" in caplog.text and ": rejouée" in caplog.text
    assert "prolongée" not in caplog.text
    assert sniper._leading["col"][1] == 14 and sniper._targets["col"].recheck
    await db.close()
