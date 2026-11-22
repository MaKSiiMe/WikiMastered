"""Salon #wikinsider : achats et ventes réels du bot à noter à la main sur Wikinsider, bouton « Noté »."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import wikinsider
from src.wikinsider import worth_noting
from src.core.notifier import DiscordNotifier, Level, Topic, _Message
from src.discord_cards import message_view
from src.workers.inventory_manager import InventoryManager
from tests.test_integration import ME, make_client, make_context

HOOK = "https://discord/wikinsider"
BASE = "https://www.wiki-masters.com"


class Capture:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, level, title, description="", **kwargs):
        self.sent.append({"level": level, "title": title, "description": description, **kwargs})

    def info(self, title, description="", **kwargs):
        self.send(Level.INFO, title, description, **kwargs)

    def success(self, title, description="", **kwargs):
        self.send(Level.SUCCESS, title, description, **kwargs)

    def warning(self, title, description="", **kwargs):
        self.send(Level.WARNING, title, description, **kwargs)


def test_only_its_own_channel_never_another():
    """Sans salon #wikinsider, rien (pas de repli sur le principal) ; avec, toujours lui."""
    msg = _Message(Level.INFO, "Achat à noter : Zidane", "", topic=Topic.WIKINSIDER)
    assert DiscordNotifier("https://discord/main", "https://discord/alertes")._webhook_for(msg) == ""
    n = DiscordNotifier("https://discord/main", channel_webhooks={"wikinsider": HOOK, "ventes": "https://discord/v"})
    assert n._webhook_for(msg) == HOOK
    assert n._webhook_for(_Message(Level.INFO, "Revente réussie", "", topic=Topic.SALES)) == "https://discord/v"


@pytest.mark.asyncio
async def test_messages_hold_the_wikinsider_form_fields_and_a_noted_button():
    cap = Capture()
    wikinsider.note_buy(cap, BASE, bid_id=12, title="Ana de Armas", price=8800, shiny=False, rarity="L",
                        auction_id="a1")
    wikinsider.note_sale(cap, BASE, listing_id=7, title="Ana de Armas", price=9600, shiny=False, rarity="L",
                         auction_id="a2", bought=8800)
    buy, sale = cap.sent
    assert buy["title"] == "Achat à noter : Ana de Armas" and buy["topic"] is Topic.WIKINSIDER and buy["live"]
    assert dict(buy["columns"]) == {"Nom exact": "`Ana de Armas`", "Prix": "`8800`", "Shiny": "non",
                                    "Type": "Marché", "Rareté": "L",
                                    "Lien de l'enchère": f"{BASE}/marketplace/a1"}
    assert "Noter un achat" in buy["description"] and buy["actions"] == {"kind": "wikinsider", "bid": 12}
    assert dict(sale["columns"])["Achetée"] == "8800 WB (+800)" and dict(sale["columns"])["Prix"] == "`9600`"
    assert "Noter une vente" in sale["description"] and sale["actions"] == {"kind": "wikinsider", "listing": 7}
    view = message_view(sale["actions"])
    assert view is not None and [b.custom_id for b in view.children] == ["wm:noted:7"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_real_sale_of_a_position_is_to_be_noted(tmp_path):
    ended = datetime.now(UTC) - timedelta(minutes=10)

    def handler(request):
        if request.url.path == "/api/marketplace/s1":
            return httpx.Response(200, json={"auction": {
                "id": "s1", "card": {"id": "c1", "wikipedia_title": "Zidane", "rarity": "L", "is_shiny": True},
                "is_shiny": True, "status": "settled_sold", "end_at": ended.isoformat(), "base_amount": 5000,
                "final_price": 6100, "winner_id": "someone"}})
        raise AssertionError(f"route inattendue {request.url}")

    client, notifier = make_client(tmp_path, handler, dry_run=False)
    ctx = await make_context(tmp_path, client, notifier)
    cap = Capture()
    db = ctx.db
    bid_id = await db.record_bid(auction_id="b1", card_id="c1", title="Zidane", rarity="L", is_shiny=True,
                                 amount=4200, reference_price=6000, reason="revente ~6000 WB", night=False,
                                 dry_run=False, status="won", end_at=ended - timedelta(days=1))
    listing = await db.schedule_listing(owned_card_id="o1", card_id="c1", title="Zidane", rarity="L",
                                        base_amount=5000, duration_minutes=60, post_at=ended - timedelta(hours=1),
                                        target_end=ended, reason="revente", bid_id=bid_id)
    await db.update_listing(listing, status="posted", auction_id="s1")
    inv = InventoryManager(ctx)
    inv.notifier = cap  # type: ignore[assignment]
    await inv.settle_listings()
    notes = [m for m in cap.sent if m.get("topic") is Topic.WIKINSIDER]
    assert [m["title"] for m in notes] == ["Vente à noter : Zidane"]
    columns = dict(notes[0]["columns"])
    assert columns["Prix"] == "`6100`" and columns["Shiny"] == "oui" and columns["Achetée"] == "4200 WB (+1900)"
    await db.close()


def test_only_shiny_and_legendary_cards_are_noted():
    assert worth_noting(False, "L") and worth_noting(True, "C") and worth_noting(True, None)
    assert not worth_noting(False, "UR") and not worth_noting(False, None)
    cap = Capture()
    wikinsider.note_buy(cap, BASE, bid_id=1, title="Fer", price=300, shiny=False, rarity="UR", auction_id="a")
    wikinsider.note_sale(cap, BASE, listing_id=2, title="Fer", price=400, shiny=False, rarity="UR", auction_id="b")
    assert cap.sent == []
    wikinsider.note_buy(cap, BASE, bid_id=3, title="Fer", price=900, shiny=True, rarity="UR", auction_id="c")
    assert [m["title"] for m in cap.sent] == ["Achat à noter : Fer"]
