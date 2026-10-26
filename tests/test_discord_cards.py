"""Commandes Discord sur les cartes : garder, vendre, réglages, boutons des messages de vente."""

from __future__ import annotations

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
    posted, buttons, direct = [], [], []

    def webhook(request):
        posted.append(request.url.path)
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.test/api/webhooks/1/a", dry_run=False)
    notifier._http = httpx.AsyncClient(transport=httpx.MockTransport(webhook))

    async def with_buttons(actions, embed, url):
        buttons.append((actions["kind"], embed["title"], url))
        return actions["kind"] != "unknown"

    async def dm(embed):
        direct.append(embed["title"])

    notifier.with_buttons, notifier.direct = with_buttons, dm
    notifier.start()
    notifier.info("Revente sans acheteur", "Ada", actions={"kind": "relist", "listing": 3})
    notifier.info("Autre", "x", actions={"kind": "unknown"})  # le bot n'en veut pas : le webhook
    notifier.critical("Session perdue", "cookie")
    notifier.critical("Vérification anti-bot", "", dm=False)
    await notifier.close()
    assert buttons[0] == ("relist", "Revente sans acheteur", "https://discord.test/api/webhooks/1/a")
    assert len(posted) == 3  # tout sauf le message à boutons
    assert direct == ["Session perdue"]


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
    assert set(MESSAGE_BUTTONS) == {"relist", "fire_sale", "kept"}


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
