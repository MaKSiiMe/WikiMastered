"""Sniper : issue d'une vente surenchérie puis rejouée, vérification anti-bot, carte de collection gagnée."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.models import Auction
from src.workers.auction_sniper import CHECK_QUOTA_BIDS, AuctionSniper, Decision, Target, position_cap, retry_text
from src.workers.collection import progress_lines
from tests.test_integration import ME, make_client, make_context, seed_observed

RIVAL = "33333333-3333-3333-3333-333333333333"


def sale(aid: str, title: str, seconds: float, base: int = 10, bid: int | None = None,
         bidder: str | None = None) -> dict:
    return {"id": aid, "card": {"id": f"card-{title}", "wikipedia_title": title, "rarity": "R"},
            "seller_id": "22222222-2222-2222-2222-222222222222", "status": "active",
            "end_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            "base_amount": base, "current_bid": bid, "current_bidder_id": bidder}


async def start(tmp_path, market: dict, replies: list, state: dict | None = None, simulate: tuple = ("sniper",),
                owned: list | None = None, **overrides):
    """Sniper réel pour la collection (jours manquants) ; chaque mise reçoit la prochaine réponse de `replies`.
    Les positions sont simulées (`simulate`), sauf demande contraire. `owned` : derniers exemplaires obtenus.
    `overrides` : réglages en plus."""
    def handler(request):
        path = request.url.path
        if path == "/api/my-collection":
            return httpx.Response(200, json={"collection": owned or []})
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

    client, notifier = make_client(tmp_path, handler, dry_run=False, simulate=list(simulate),
                                   collection_sets=["jours"], collection_max_price=20, **overrides)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    await db.kv_set("collection", state or {"owned": [], "missing": ["14 octobre", "15 octobre"], "total": 366})
    sniper = AuctionSniper(ctx)
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

    # Le 16/10 : ~45 mises refusées en 1 h 30. Tant que la vérification attend, une mise toutes les
    # SNIPER_ANTIBOT_RETRY_SECONDS (5 min).
    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()
    assert len(replies) == 2 and "pas tentée : vérification anti-bot en attente 1" in sniper.recap()[1]

    sniper._antibot_retry_at = datetime.now(UTC)
    market["c"] = sale("c", "15 octobre", 15)
    await sniper.step()  # encore refusée : pas de deuxième alerte
    assert len(replies) == 1
    assert notifier.sent.count(("CRITICAL", "Sniper : vérification anti-bot demandée")) == 1

    sniper._antibot_retry_at = datetime.now(UTC)
    market["d"] = sale("d", "15 octobre", 15)
    await sniper.step()
    assert ("SUCCESS", "Sniper : vérification levée") in notifier.sent and sniper.blocked is None
    await db.close()


@pytest.mark.asyncio
async def test_antibot_retry_zero_tries_every_bid_while_pending(tmp_path):
    # Le 24/10 : une extension passe la vérification dans le navigateur ; la levée doit être vue tout de suite.
    market = {"a": sale("a", "14 octobre", 15)}
    refusal = (400, {"error": "Vérification anti-bot requise."})
    replies = [refusal, (200, {"current_bid": 10, "bidder_balance": 2990})]
    sniper, db, notifier = await start(tmp_path, market, replies, sniper_antibot_retry_seconds=0)
    await sniper.step()
    assert ("CRITICAL", "Sniper : vérification anti-bot demandée") in notifier.sent

    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()  # tentée sans attendre, et elle passe
    assert not replies and ("SUCCESS", "Sniper : vérification levée") in notifier.sent
    assert "pas tentée" not in sniper.recap()[1]
    await db.close()


def test_retry_text():
    assert retry_text(300) == "ne tente qu'une mise toutes les 5 min"
    assert retry_text(30) == "ne tente qu'une mise toutes les 30 s"
    assert retry_text(0) == "tente quand même chaque mise"


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
    sniper._antibot_remind_at = datetime.now(UTC) - timedelta(seconds=1)
    sniper._antibot_retry_at = datetime.now(UTC)
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
    sniper, db, _ = await start(tmp_path, market, replies, state, simulate=())
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
async def test_collection_quota_renewed_after_check_quota_without_refusal(tmp_path):
    """21/10 après 14:19 : 36 mises sans vérification, la collection gardait son quota épuisé pendant 10 h."""
    state = {"owned": [], "missing": ["14 octobre", "15 octobre", "16 octobre"], "total": 366}
    ok = (200, {"current_bid": 10, "bidder_balance": 2990})
    market = {"a": sale("a", "14 octobre", 15)}
    replies = [ok] * (CHECK_QUOTA_BIDS + 1)
    sniper, db, _ = await start(tmp_path, market, replies, state, simulate=())
    await sniper.step()
    market["b"] = sale("b", "15 octobre", 15)
    await sniper.step()
    assert sniper._collection_since_check == 2
    for n in range(CHECK_QUOTA_BIDS - 2):  # les autres mises du quota, acceptées sans refus
        await sniper._bid(Auction.model_validate(sale(f"x{n}", "16 octobre", 15)),
                          Decision(10, 20, "collection : carte manquante ≤ 20", None),
                          sniper_params(sniper.api.client))
    assert sniper._collection_since_check == 0 and sniper._bids_since_check == 0

    market["c"] = sale("c", "16 octobre", 15)
    await sniper.step()  # quota renouvelé : la mise part
    assert not replies and "quota de la vérification gardé" not in sniper.recap()[1]
    await db.close()


@pytest.mark.asyncio
async def test_collection_quota_not_kept_while_positions_are_simulated(tmp_path):
    """18/10 après 12:00 (fin de SNIPER_LIVE_END) : les L simulées, la collection gardait quand même leur quota."""
    state = {"owned": [], "missing": ["14 octobre", "15 octobre", "16 octobre"], "total": 366}
    ok = (200, {"current_bid": 10, "bidder_balance": 2990})
    market = {"a": sale("a", "14 octobre", 15)}
    replies = [ok, ok, ok]
    sniper, db, _ = await start(tmp_path, market, replies, state)  # positions simulées
    for aid, day in (("b", "15 octobre"), ("c", "16 octobre")):
        await sniper.step()
        market[aid] = sale(aid, day, 15)
    await sniper.step()
    assert not replies and sniper._collection_since_check == 3
    assert "quota de la vérification gardé" not in sniper.recap()[1]
    await db.close()


@pytest.mark.asyncio
async def test_won_collection_card_counts_at_once(tmp_path):
    state = {"owned": ["1 janvier"], "missing": ["14 octobre", "15 octobre"], "total": 366,
             "sets": {"Jours de l'année": [1, 366]}, "first_owned": {"1 janvier": None}}
    sniper, db, _ = await start(tmp_path, {}, [], state)
    await db.record_bid(auction_id="col", card_id="card-14 octobre", title="14 octobre", rarity="R", is_shiny=False,
                        amount=13, reference_price=None, reason="collection : carte manquante ≤ 20", night=False,
                        dry_run=False, status="placed", end_at=datetime.now(UTC))
    row = await db.last_bid("col")
    await sniper._close_bid(row, won=True, simulated=False)

    # Une autre vente du même jour n'est plus misée, et le récap la compte sans attendre la lecture (6 h).
    assert "14 octobre" not in sniper._collection_missing
    saved = await db.kv_get("collection")
    assert saved["missing"] == ["15 octobre"] and "14 octobre" in saved["owned"]
    lines = progress_lines(saved, {}, datetime.now(UTC))
    assert lines[0] == "Jours de l'année : 2/366 (0,5 %)" and lines[1].startswith("+1 en 7 jours : 14 octobre")
    await db.close()


@pytest.mark.asyncio
async def test_vanished_auction_closes_the_bid_without_failing(tmp_path):
    """Récap du 16/10 à 00:00 : « auction_detail: Enchère introuvable » faisait échouer le passage du sniper."""
    sniper, db, notifier = await start(tmp_path, {}, [])
    ended = datetime.now(UTC) - timedelta(minutes=5)
    for aid, dry_run, status in (("gone-sim", True, "simulated"), ("gone-real", False, "placed")):
        await db.record_bid(auction_id=aid, card_id="card-x", title="Le Parrain", rarity="L", is_shiny=False,
                            amount=150, reference_price=550, reason="revente ~550 WB", night=False,
                            dry_run=dry_run, status=status, end_at=ended)
    await sniper._settle()  # aucune exception
    assert (await db.last_bid("gone-sim"))["status"] == "sim_lost"
    # 18/10 à 23:00, site instable : un 404 juste après la fin ne prouve pas la perte. Relue plus tard.
    real = await db.last_bid("gone-real")
    assert real["status"] == "placed" and real["id"] in sniper._vanished_retry
    await sniper._settle()  # relecture pas encore due : aucune requête
    assert (await db.last_bid("gone-real"))["status"] == "placed"
    await db.update_bid(real["id"], end_at=datetime.now(UTC) - timedelta(minutes=40))
    sniper._vanished_retry.clear()
    await sniper._settle()
    assert (await db.last_bid("gone-real"))["status"] == "lost"
    assert notifier.sent.count(("WARNING", "Mise à vérifier")) == 1  # seulement pour la mise réelle
    await db.close()


def arrived(card_id: str, rarity: str = "L", at: datetime | None = None) -> dict:
    return {"id": "o-new", "card_id": card_id, "card": {"id": card_id, "wikipedia_title": "Quentin Dupieux",
                                                         "rarity": rarity},
            "obtained_at": (at or datetime.now(UTC)).isoformat()}


@pytest.mark.asyncio
async def test_vanished_auction_won_when_the_card_arrived(tmp_path):
    """Quentin Dupieux (L), 444 WB, 18/10 à 23:00 : enchère introuvable, mais la carte est dans la collection."""
    owned = [{**arrived("card-qd", at=datetime.now(UTC) - timedelta(days=3)), "id": "o-old"},  # d'avant la mise
             arrived("card-qd", "UR")]  # autre rareté
    sniper, db, notifier = await start(tmp_path, {}, [], simulate=(), owned=owned)
    await db.record_bid(auction_id="qd", card_id="card-qd", title="Quentin Dupieux", rarity="L", is_shiny=False,
                        amount=444, reference_price=717, reason="revente ~717 WB", night=False, dry_run=False,
                        status="placed", end_at=datetime.now(UTC) - timedelta(minutes=2))
    await sniper._settle()
    assert (await db.last_bid("qd"))["status"] == "placed"  # pas encore arrivée : relue plus tard
    owned.append(arrived("card-qd"))
    sniper._vanished_retry.clear()
    await sniper._settle()
    bid = await db.last_bid("qd")
    assert (bid["status"], bid["closed"], bid["final_price"]) == ("won", 0, 444)  # une position, à revendre
    assert ("SUCCESS", "Position achetée !") in notifier.sent
    assert ("WARNING", "Mise à vérifier") not in notifier.sent
    await db.close()


@pytest.mark.asyncio
async def test_bid_wrongly_counted_lost_is_recovered_at_startup(tmp_path):
    """Mises closes perdues sur un seul 404 avant ce correctif : retrouvées au démarrage si la carte est arrivée."""
    owned = [arrived("card-qd")]
    sniper, db, notifier = await start(tmp_path, {}, [], simulate=(), owned=owned)
    for aid, card in (("qd", "card-qd"), ("other", "card-zz")):
        bid_id = await db.record_bid(auction_id=aid, card_id=card, title="Quentin Dupieux", rarity="L",
                                     is_shiny=False, amount=444, reference_price=717, reason="revente ~717 WB",
                                     night=False, dry_run=False, status="placed",
                                     end_at=datetime.now(UTC) - timedelta(minutes=10))
        await db.update_bid(bid_id, status="lost", note="enchère introuvable sur le site")
    await sniper.setup()
    assert (await db.last_bid("qd"))["status"] == "won"
    assert (await db.last_bid("other"))["status"] == "lost"  # carte jamais arrivée : reste perdue
    await db.close()


@pytest.mark.asyncio
async def test_missed_collection_card_counted_apart(tmp_path):
    """Récap du 16/10 : 1933 « pas relue à temps » sur 2091, sans savoir combien de cartes de collection."""
    sniper, db, _ = await start(tmp_path, {}, [])
    now = datetime.now(UTC)
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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    for i in range(6):
        await db.record_bid(auction_id=f"sim{i}", card_id=f"c{i}", title=f"Carte {i}", rarity="L", is_shiny=False,
                            amount=200, reference_price=600, reason="revente ~600 WB", night=False, dry_run=True,
                            status="sim_won", end_at=now)
    await db.record_bid(auction_id="real", card_id="cr", title="Réelle", rarity="L", is_shiny=False, amount=400,
                        reference_price=900, reason="revente ~900 WB", night=False, dry_run=False, status="won",
                        end_at=now)
    sniper = AuctionSniper(ctx)
    await sniper.setup()
    portfolio = await sniper.portfolio(now)
    assert portfolio.open_positions == open_positions and portfolio.can_open == (open_positions < 5)
    await db.close()


def sniper_params(client):
    from src.workers.auction_sniper import snipe_params
    return snipe_params(client.settings, night=False)


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
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    for card in ("card-a", "card-b"):
        await seed_observed(db, card, [1500, 1450, 1550], rarity="L")
    sniper = AuctionSniper(ctx)
    await sniper.setup()

    def l_sale(aid, card, base, bid=None):
        return Auction.model_validate({**sale(aid, "x", 15, base=base, bid=bid, bidder=RIVAL if bid else None),
                                       "card": {"id": card, "wikipedia_title": card, "rarity": "L"}})

    now = datetime.now(UTC)
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
                               end_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))


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
async def test_win_rebid_by_hand_costs_the_final_price(tmp_path):
    """27/10 : FC Barcelone, mise du bot 2574 surenchérie, remisée à la main et gagnée à 3116. La position coûte
    3116 : la revente part de ce prix, pas de 2574 (elle était remise en vente à 3130, à perte après commission)."""
    sniper, db, _ = await start(tmp_path, {"w": ended("w", "FC Barcelone", winner=ME, final=3116)}, [])
    await outbid_bid(db, "w", "FC Barcelone", 2574)
    await sniper._settle()
    won = await db.last_bid("w")
    assert (won["status"], won["amount"], won["final_price"]) == ("won", 3116, 3116)
    assert "2574" in won["note"]
    await db.close()


@pytest.mark.asyncio
async def test_won_bid_below_final_price_is_repaired_on_startup(tmp_path):
    sniper, db, _ = await start(tmp_path, {}, [])
    bid_id = await outbid_bid(db, "w", "FC Barcelone", 2574)
    await db.update_bid(bid_id, status="won", final_price=3116)
    await db.connect()
    assert (await db.last_bid("w"))["amount"] == 3116
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
                                 end_at=datetime.now(UTC) + timedelta(seconds=30))
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
    sniper._sale_slots = (datetime.now(UTC), 5, None)
    cap, room = await sniper._sale_capacity(datetime.now(UTC))
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
