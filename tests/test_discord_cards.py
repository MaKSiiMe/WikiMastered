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
