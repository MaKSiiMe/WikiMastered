"""Commandes Discord sur les cartes : garder, vendre, réglages, boutons des messages de vente."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import keeps, overrides
from src.core.models import OwnedCard
from src.core.notifier import DiscordNotifier
from src.discord_cards import MESSAGE_BUTTONS, MessageButton, message_view, sellable
from src.workers.inventory_manager import excluded_copies, stop_resale
from tests.test_integration import make_client, make_context


def owned(oid: str, title: str, rarity: str = "L", *, starred: bool = False, shiny: bool = False,
          days: int = 0) -> OwnedCard:
    return OwnedCard.model_validate({
        "id": oid, "card": {"id": f"cat-{title}", "wikipedia_title": title, "rarity": rarity}, "starred": starred,
        "is_shiny": shiny, "obtained_at": (datetime.now(UTC) - timedelta(days=days)).isoformat()})


def trades(_request):
    return httpx.Response(200, json={"trades": []})


@pytest.mark.asyncio
async def test_kept_cards_are_never_sold(tmp_path):
    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    await keeps.add(ctx.db, "Napoléon Ier", "L")
    await keeps.add(ctx.db, "napoleon ier", "L")  # même carte, même rareté : pas de doublon
    rows = [owned("a", "Napoléon Ier", "L"), owned("b", "Napoléon Ier", "UR"), owned("c", "Ada")]
    assert await excluded_copies(ctx, rows) == {"a"}  # seule la L est gardée
    assert [k.label for k in await keeps.load(ctx.db)] == ["Napoléon Ier (L)"]
    assert await keeps.remove(ctx.db, "napoléon ier (l)") is not None
    assert await excluded_copies(ctx, rows) == set()
    await ctx.db.close()


def test_copy_to_sell_skips_favorites_shiny_and_excluded():
    old, new = owned("old", "Ada", days=3), owned("new", "Ada", days=1)
    assert sellable([old, new], set())[0] is new  # le plus récent
    assert sellable([old, new], {"new"})[0] is old
    assert sellable([owned("s", "Ada", shiny=True)], set())[0] is None
    copy, why = sellable([owned("f", "Ada", starred=True)], set())
    assert copy is None and "favori" in why
    assert "aucun exemplaire en UR" in sellable([old], set(), "UR")[1]


@pytest.mark.asyncio
async def test_manual_sale_goes_first_and_keeping_cancels_a_resale(tmp_path):
    from src.core.database import MANUAL_SALE_REASON

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await db.record_bid(auction_id="x", card_id="c1", title="Ada", rarity="L", is_shiny=False, amount=100,
                              reference_price=500, reason="revente", night=False, dry_run=False, status="won",
                              end_at=now)
    common = {"rarity": "L", "duration_minutes": 360, "post_at": now - timedelta(minutes=1),
              "target_end": now + timedelta(hours=6)}
    resale = await db.schedule_listing(owned_card_id="o1", card_id="c1", title="Ada", base_amount=900,
                                       reason="revente", bid_id=bid, **common)
    manual = await db.schedule_listing(owned_card_id="o2", card_id="c2", title="Bob", base_amount=50,
                                       reason=f"{MANUAL_SALE_REASON} (Discord)", **common)
    assert [r["id"] for r in await db.due_listings(now)] == [manual, resale]  # avant un plus gros gain

    assert await stop_resale(ctx, bid) == 1
    assert (await db.get_listing(resale))["status"] == "cancelled"
    assert (await db.get_bid(bid))["closed"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_messages_with_buttons_go_through_the_bot_else_the_webhook(tmp_path):
    posted, buttons = [], []

    def webhook(request):
        posted.append(json.loads(request.content).get("content"))
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.test/api/webhooks/1/a", dry_run=False, mention="<@1>")
    notifier._http = httpx.AsyncClient(transport=httpx.MockTransport(webhook))

    async def with_buttons(actions, embed, url, mention):
        buttons.append((actions["kind"], embed["title"], url, mention))
        return actions["kind"] != "unknown"

    notifier.with_buttons = with_buttons
    notifier.start()
    notifier.info("Revente sans acheteur", "Ada", actions={"kind": "relist", "listing": 3})
    notifier.info("Autre", "x", actions={"kind": "unknown"})  # le bot n'en veut pas : le webhook
    notifier.critical("Session perdue", "cookie")
    notifier.critical("Vérification anti-bot", "", actions={"kind": "antibot"})
    await notifier.close()
    assert buttons[0] == ("relist", "Revente sans acheteur", "https://discord.test/api/webhooks/1/a", "")
    # Une alerte critique à bouton garde la mention ; jamais de message privé.
    assert buttons[-1] == ("antibot", "Vérification anti-bot", "https://discord.test/api/webhooks/1/a", "<@1>")
    assert posted == [None, "<@1>"]  # « Autre » et « Session perdue » par le webhook


@pytest.mark.asyncio
async def test_message_buttons_survive_a_restart():
    """L'action et la vente sont dans l'identifiant du bouton : le bot les retrouve après un redémarrage."""
    view = message_view({"kind": "relist", "listing": 42})
    ids = [item.custom_id for item in view.children]
    assert ids == ["wm:price:42", "wm:cheap:42", "wm:keep:42"]
    template = MessageButton.__discord_ui_compiled_template__
    assert all(re.fullmatch(template, i) for i in ids)
    assert not re.fullmatch(template, "wm:verif-faite")  # le bouton anti-bot garde le sien
    assert message_view({"kind": "relist"}) is None and message_view({"kind": "autre", "listing": 1}) is None
    assert set(MESSAGE_BUTTONS) == {"relist", "fire_sale", "kept", "stale"}


@pytest.mark.asyncio
async def test_collection_cap_changed_on_discord_survives_a_restart(tmp_path):
    client, notifier = make_client(tmp_path, trades, collection_max_price=20)
    ctx = await make_context(tmp_path, client, notifier)
    assert await overrides.set_value(ctx, "collection_max_price", 50) == 20
    assert ctx.settings.collection_max_price == 50
    ctx.settings.collection_max_price = 20  # redémarrage : la variable d'environnement…
    assert await overrides.load(ctx) == {"collection_max_price": 50}  # … puis le réglage de Discord
    assert ctx.settings.collection_max_price == 50
    with pytest.raises(ValueError):
        await overrides.set_value(ctx, "dry_run", False)
    await ctx.db.close()


@pytest.mark.asyncio
async def test_unsold_resale_message_offers_buttons_for_the_new_listing(tmp_path):
    from src.workers.inventory_manager import InventoryManager
    from tests.test_validation import simulated_position

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"collection": []}))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    bid_id = await simulated_position(db, 400)
    await db._exec("UPDATE bids SET dry_run = 0, status = 'won'")  # position réelle
    await db._exec("UPDATE listings SET status = 'unsold'")  # comme settle_listings avant _unsold
    sent = []
    notifier.send = lambda level, title, description="", **fields: sent.append((title, fields.get("actions")))
    inv = InventoryManager(ctx)
    row = await db._one("SELECT * FROM listings")
    await inv._unsold(row, await db.get_bid(bid_id), "Mème (C)")
    relist = await db._one("SELECT id, base_amount FROM listings WHERE status = 'scheduled'")
    assert relist["base_amount"] == 360
    assert sent == [("Revente sans acheteur", {"kind": "relist", "listing": relist["id"]})]
    await db.close()


@pytest.mark.asyncio
async def test_turning_off_duplicate_sales_cancels_queued_duplicates_only(tmp_path):
    from src.core.database import MANUAL_SALE_REASON

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    common = {"card_id": "c", "title": "Ada", "rarity": "C", "base_amount": 20, "duration_minutes": 360,
              "post_at": now, "target_end": now + timedelta(hours=6)}
    dup = await db.schedule_listing(owned_card_id="d", reason="cote 25", **common)
    manual = await db.schedule_listing(owned_card_id="m", reason=f"{MANUAL_SALE_REASON} (Discord)", **common)
    assert await db.cancel_scheduled_duplicates() == 1
    assert (await db.get_listing(dup))["status"] == "cancelled"
    assert (await db.get_listing(manual))["status"] == "scheduled"
    await overrides.set_value(ctx, "auto_discard", False)
    ctx.settings.auto_discard = True
    await overrides.load(ctx)
    assert ctx.settings.auto_discard is False
    await db.close()


async def held_position(db, *, hours_ago: float, dry_run: bool = False, amount: int = 3000) -> int:
    bid = await db.record_bid(auction_id=f"a{hours_ago}{dry_run}", card_id="c1", title="Ada", rarity="L",
                              is_shiny=False, amount=amount, reference_price=4000, reason="revente", night=False,
                              dry_run=dry_run, status="sim_won" if dry_run else "won", end_at=datetime.now(UTC))
    await db._exec("UPDATE bids SET settled_at = ? WHERE id = ?",
                   ((datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(), bid))
    return bid


@pytest.mark.asyncio
async def test_stale_position_is_signalled_once_with_buttons(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    sent = []
    notifier.send = lambda level, title, description="", **fields: sent.append((title, description,
                                                                                  fields.get("actions")))
    stale = await held_position(db, hours_ago=60)
    await held_position(db, hours_ago=10)  # récente
    await held_position(db, hours_ago=90, dry_run=True)  # simulée : jamais signalée
    inv = InventoryManager(ctx)
    assert await inv.warn_stale_positions() == 1
    title, description, actions = sent[0]
    assert title == "Position qui traîne" and "il y a 60 h" in description and "aucune vente prévue" in description
    assert actions == {"kind": "stale", "bid": stale}
    assert await inv.warn_stale_positions() == 0  # une seule alerte
    assert len(sent) == 1
    view = message_view(actions)
    assert [i.custom_id for i in view.children] == [f"wm:stalefire:{stale}", f"wm:stalekeep:{stale}"]
    await db.close()


@pytest.mark.asyncio
async def test_orders_apply_when_the_published_sale_ends_unsold(tmp_path):
    """« Garder » ou « Brader » sur une vente déjà publiée : appliqué à sa fin ; une position close n'est jamais
    remise en vente (carte gardée sur Discord pendant que sa vente était publiée)."""
    from src import orders
    from src.workers.inventory_manager import InventoryManager

    client, notifier = make_client(tmp_path, lambda r: httpx.Response(200, json={"collection": []}))
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    inv = InventoryManager(ctx)
    now = datetime.now(UTC)

    async def unsold_listing(bid_id: int):
        lid = await db.schedule_listing(owned_card_id=f"o{bid_id}", card_id="c1", title="Ada", rarity="L",
                                        base_amount=4000, duration_minutes=360, post_at=now,
                                        target_end=now + timedelta(hours=6), reason="position", bid_id=bid_id)
        await db.update_listing(lid, status="unsold")
        return await db.get_listing(lid)

    keep = await held_position(db, hours_ago=60)
    await orders.give(db, keep, orders.KEEP)
    await inv._unsold(await unsold_listing(keep), await db.get_bid(keep), "Ada (L)")
    assert (await db.get_bid(keep))["closed"] == 1 and await orders.take(db, keep) is None
    assert not await db._all("SELECT * FROM listings WHERE status = 'scheduled'")

    fire = await held_position(db, hours_ago=61)
    await orders.give(db, fire, orders.FIRE)
    await inv._unsold(await unsold_listing(fire), await db.get_bid(fire), "Ada (L)")
    row = await db._one("SELECT base_amount, reason FROM listings WHERE bid_id = ? AND status = 'scheduled'", (fire,))
    assert row["base_amount"] == 1 and row["reason"].startswith("liquidation")

    closed = await held_position(db, hours_ago=62)
    await db.close_position(closed, None)  # gardée pendant que sa vente était publiée
    await inv._unsold(await unsold_listing(closed), await db.get_bid(closed), "Ada (L)")
    assert not await db._all("SELECT * FROM listings WHERE bid_id = ? AND status = 'scheduled'", (closed,))
    await db.close()


@pytest.mark.asyncio
async def test_buying_a_discord_sale_to_one_wb_picks_a_late_ending_for_at_least_three_hours(tmp_path):
    from src.workers.inventory_manager import InventoryManager

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    now = datetime.now(UTC)
    bid = await held_position(db, hours_ago=60)
    lid = await db.schedule_listing(owned_card_id="o", card_id="c1", title="Ada", rarity="L", base_amount=4000,
                                    duration_minutes=720, post_at=now, target_end=now + timedelta(hours=12),
                                    reason="position", bid_id=bid)
    await InventoryManager(ctx).liquidate(lid)
    row = await db.get_listing(lid)
    assert row["base_amount"] == 1 and row["duration_minutes"] >= 180 and row["reason"].startswith("liquidation")
    await db.close()


@pytest.mark.asyncio
async def test_shiny_lists_open_shiny_auctions_with_their_quote(tmp_path):
    from src.discord_cards import shiny_embeds
    from src.roi import wb

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)

    def auction(aid, title, shiny, minutes, min_bid, bid=None):
        return {"auction_id": aid, "card_id": aid, "title": title, "rarity": "L", "is_shiny": shiny, "current_bid": bid,
                "base_amount": min_bid, "min_bid": min_bid, "end_at": (now + timedelta(minutes=minutes)).isoformat(),
                "seen_at": now.isoformat()}

    await ctx.db.upsert_auctions([auction("a", "Ada", 1, 90, 5000), auction("b", "Bob", 1, 30, 9000, bid=8000),
                                  auction("n", "Normale", 0, 10, 100), auction("f", "Finie", 1, -5, 100)])
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at) "
        "VALUES (?, 'a', 'Ada', 'L', 1, ?, ?)",
        [(f"s{i}", p, (now - timedelta(days=1)).isoformat()) for i, p in enumerate((7000, 8000, 8000, 9000))])
    await ctx.db.db.commit()

    [embed], picks = await shiny_embeds(ctx, 25)
    assert embed.title == "✨ Shiny en vente (2)"
    first, second = embed.description.splitlines()
    assert first.startswith(f"**Bob** (L) · mise min **{wb(9000)}** · déjà misée · cote ?")  # finit le premier
    s = ctx.settings
    ceiling = int(min(8000 * s.sniper_deal_ratio_max, 8000 * (1 - s.sale_fee_pct) - s.portfolio_min_profit))
    assert second.startswith(f"💰 **Ada** (L) · mise min **{wb(5000)}** · cote {wb(8000)} · snipe ≤ **{wb(ceiling)}**")
    [good], _ = await shiny_embeds(ctx, 25, profitable=True)
    assert good.title == "✨ Shiny rentables en vente (1)" and "Bob" not in good.description
    [none], no_picks = await shiny_embeds(ctx, 25, "end", 10, True)
    assert none.description == "Aucun shiny rentable en vente vu par le sniper."
    assert not no_picks
    assert [(p.title, p.rarity, p.min_price, p.ceiling) for p in picks] == [("Ada", "L", 5000, ceiling)]
    assert "Normale" not in embed.description and "Finie" not in embed.description
    assert (await shiny_embeds(ctx, 25, "low"))[0][0].description.startswith("💰 **Ada**")
    assert (await shiny_embeds(ctx, 25, "high"))[0][0].description.startswith("**Bob**")
    [cheap], _ = await shiny_embeds(ctx, 25, "end", 6000)
    assert cheap.title == "✨ Shiny en vente (1)" and "Bob" not in cheap.description
    [nothing], _ = await shiny_embeds(ctx, 25, "end", 10)
    assert nothing.description == "Aucun shiny en vente vu par le sniper."
    [one], _ = await shiny_embeds(ctx, 1)
    assert one.footer.text.startswith("1 sur 2, fin la plus proche.")


@pytest.mark.asyncio
async def test_shiny_menu_offers_each_pick_and_asks_its_ceiling():
    from unittest.mock import AsyncMock, MagicMock

    from src.discord_cards import PriceModal, ShinyPick, ShinyPicker

    on_price = AsyncMock()
    picks = [ShinyPick(f"Carte {i}", "L", 100, 500 + i) for i in range(30)]
    view = ShinyPicker(42, picks, on_price)
    assert len(view.select.options) == 25 and view.select.options[1].label == "Carte 1"
    assert not await view.interaction_check(MagicMock(user=MagicMock(id=7)))

    view.select._values = ["1"]
    interaction = MagicMock(response=MagicMock(send_modal=AsyncMock()))
    await view.chosen(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, PriceModal) and modal.price.default == "501"
    submitted = MagicMock()
    await modal.on_price(submitted, 450)
    on_price.assert_awaited_once_with(submitted, picks[1], 450)


@pytest.mark.asyncio
async def test_top_ranks_cards_by_average_recent_price(tmp_path):
    from src.discord_cards import top_embeds
    from src.roi import wb

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    now = datetime.now(UTC)
    sales = [  # (carte, titre, shiny, prix, jours)
        *[("a", "Ada", 0, p, 1) for p in (900, 1000, 1100)],
        *[("b", "Bob", 0, p, 1) for p in (2000, 2100, 2200)],
        ("c", "Cid", 0, 9000, 1), ("c", "Cid", 0, 9000, 1),  # 2 ventes seulement : pas classée
        *[("d", "Dan", 0, 5000, 10) for _ in range(3)],      # trop anciennes
        *[("a", "Ada", 1, p, 1) for p in (7000, 8000, 9000)],
    ]
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at) "
        "VALUES (?, ?, ?, 'L', ?, ?, ?)",
        [(f"s{i}", card, title, shiny, price, (now - timedelta(days=days)).isoformat())
         for i, (card, title, shiny, price, days) in enumerate(sales)])
    await ctx.db.db.commit()

    [embed] = await top_embeds(ctx, 10, False)
    assert embed.title == "🏆 Top 10 des cartes normales les plus chères"
    lines = embed.description.splitlines()
    assert len(lines) == 2 and "Ada" in lines[1]
    # Revente ~2100 (médiane) : à 1909, la relance minimale (2100) l'atteint ; bénéfice 191.
    assert f"**Bob** (L) · **{wb(2100)}** · 3 ventes · {wb(2000)}–{wb(2200)} · miser **{wb(1909)}** (+191)" \
        in lines[0]
    [shiny] = await top_embeds(ctx, 10, True)
    assert f"**Ada** (L) · **{wb(8000)}**" in shiny.description and "Bob" not in shiny.description
    assert f"miser **{wb(7272)}**" in shiny.description  # médiane shiny 8000, pas celle de la carte normale

    # 50 cartes : deux embeds de 25 lignes (une description fait 4096 caractères au plus).
    await ctx.db.db.executemany(
        "INSERT INTO observed_sales (auction_id, card_id, title, rarity, is_shiny, price, ended_at) "
        "VALUES (?, ?, ?, 'R', 0, 100, ?)",
        [(f"m{c}-{k}", f"m{c}", "M" * 60, now.isoformat()) for c in range(48) for k in range(3)])
    await ctx.db.db.commit()
    first, second = await top_embeds(ctx, 50, False)
    assert len(first.description.splitlines()) == len(second.description.splitlines()) == 25
    assert second.title.endswith("(suite)") and second.footer.text and not first.footer.text
    await ctx.db.close()


@pytest.mark.asyncio
async def test_antibot_refusal_keeps_the_resale_and_alerts_once(tmp_path):
    """01/11 : 3 refus « Vérification anti-bot requise » fermaient la position, 74 alertes en 1 h. Maintenant : la
    revente reste programmée, un seul essai par attente, une alerte, et le prix sous une concurrente n'est pas gardé."""
    from src.workers.inventory_manager import InventoryManager
    from tests.test_validation import simulated_position

    posts = {"n": 0, "refuse": True}

    def handler(request):
        if request.method == "POST":
            posts["n"] += 1
            if posts["refuse"]:
                return httpx.Response(400, json={"error": "Vérification anti-bot requise."})
            return httpx.Response(201, json={"auction": {"id": "a1"}})
        return httpx.Response(200, json={"sellingCount": 0, "maxConcurrentAuctions": 5, "auctions": []})

    client, notifier = make_client(tmp_path, handler)
    ctx = await make_context(tmp_path, client, notifier)
    ctx.settings.dry_run = False
    db = ctx.db
    bid_id = await simulated_position(db, 400)
    await db._exec("UPDATE bids SET dry_run = 0, status = 'won'")
    await db._exec("UPDATE listings SET status = 'scheduled', dry_run = 0, owned_card_id = 'o1', "
                   f"post_at = '2020-01-01T00:00:00+00:00', bid_id = {bid_id}")
    sent = []
    notifier.send = lambda level, title, description="", **fields: sent.append(title)
    inv = InventoryManager(ctx)
    await inv.post_due_listings()
    await inv.post_due_listings()  # attente : aucun nouvel essai
    assert posts["n"] == 1 and sent.count("Mises en vente suspendues") == 1
    row = await db._one("SELECT status FROM listings")
    assert row["status"] == "scheduled" and await db.failed_listings_count(bid_id) == 0
    assert "anti-bot" in inv.blocked
    assert inv.verification_done()
    posts["refuse"] = False
    await inv.post_due_listings()
    assert posts["n"] == 2 and "Mises en vente reprises" in sent and inv.blocked is None
    assert (await db._one("SELECT status FROM listings"))["status"] == "posted"
    await db.close()


@pytest.mark.asyncio
async def test_positions_abandoned_for_antibot_are_reopened_at_their_relist_price(tmp_path):
    from tests.test_validation import simulated_position

    client, notifier = make_client(tmp_path, trades)
    ctx = await make_context(tmp_path, client, notifier)
    db = ctx.db
    bid_id = await simulated_position(db, 400)
    other = await simulated_position(db, 500)
    await db._exec("UPDATE bids SET dry_run = 0, status = 'won', closed = 1")
    await db._exec(f"UPDATE listings SET status = 'failed', relist_count = 2, base_amount = 360, "
                   f"error = 'Vérification anti-bot requise.' WHERE bid_id = {bid_id}")
    await db._exec(f"UPDATE listings SET status = 'failed', error = 'Autre erreur' WHERE bid_id = {other}")
    assert await db.reopen_antibot_positions() == 1
    assert await db.reopen_antibot_positions() == 0  # une seule fois
    assert (await db.get_bid(bid_id))["closed"] == 0 and (await db.get_bid(other))["closed"] == 1
    assert await db.failed_listings_count(bid_id) == 0
    assert (await db.last_failed_relist(bid_id))["base_amount"] == 360
    await db.close()

