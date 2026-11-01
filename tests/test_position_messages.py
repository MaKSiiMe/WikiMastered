"""Message unique d'une position : créé à l'achat, modifié à chaque étape, republié en bas du salon à la vente."""

from __future__ import annotations

import json

import httpx
import pytest

from src.core.notifier import DiscordNotifier, Topic, position_thread

HOOK = "https://discord.test/api/webhooks/1/a"
BID = {"id": 7, "title": "Albert Camus", "rarity": "L", "is_shiny": 1}


class Kv:
    def __init__(self):
        self.data = {}

    async def kv_get(self, key, default=None):
        return self.data.get(key, default)

    async def kv_set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))  # comme la base : une copie


def webhook_notifier(calls: list) -> DiscordNotifier:
    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path.removeprefix("/api/webhooks/1/a"),
                      dict(request.url.params), body))
        if request.method == "POST":
            return httpx.Response(200, json={"id": str(100 + len(calls))})
        return httpx.Response(204 if request.method == "DELETE" else 200, json={})

    n = DiscordNotifier("", dry_run=False, channel_webhooks={"ventes": HOOK})
    n._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n.store = Kv()
    return n


@pytest.mark.asyncio
async def test_position_message_is_edited_then_reposted_when_sold():
    calls: list = []
    n = webhook_notifier(calls)
    n.start()
    n.success("Position achetée !", "Albert Camus", topic=Topic.SALES, url="https://fr.wikipedia.org/wiki/Albert_Camus",
              thread=position_thread(BID, "🎯 achetée 2238 WB", head="Achetée **2238 WB**", ring=True))
    n.info("Carte mise en vente", "x", topic=Topic.SALES,
           thread=position_thread({**BID, "is_shiny": 0}, "💰 en vente à 2574 WB, fin 03:02"))
    n.success("Revente réussie", "x", topic=Topic.SALES,
              thread=position_thread(BID, "✅ vendue 2845 WB (+607)", head="vendue **2845 WB**", ring=True))
    await n.close()

    assert [(method, path) for method, path, *_ in calls] == [
        ("POST", ""), ("PATCH", "/messages/101"), ("DELETE", "/messages/101"), ("POST", "")]
    assert calls[0][2] == {"wait": "true"}  # pour connaître l'identifiant du message
    first, edited, last = calls[0][3]["embeds"][0], calls[1][3]["embeds"][0], calls[3][3]["embeds"][0]
    assert first["title"] == "Albert Camus (L ✨)" and first["url"].endswith("Albert_Camus")
    assert first["description"].startswith("Achetée **2238 WB**\n\n`") and "🎯 achetée 2238 WB" in first["description"]
    assert edited["title"] == "Albert Camus (L ✨)"  # le titre de l'achat reste, même sans shiny à la vente
    assert edited["description"].count("\n`") == 2 and "💰 en vente" in edited["description"]
    assert last["description"].startswith("vendue **2845 WB**") and last["description"].count("\n`") == 3
    assert last["url"].endswith("Albert_Camus") and last["color"] == 0x22C55E
    assert n.store.data["discord:position:7"]["message"] == {"via": "webhook", "url": HOOK, "id": "104"}


@pytest.mark.asyncio
async def test_lost_message_is_posted_again():
    calls: list = []
    n = webhook_notifier(calls)
    n.store.data["discord:position:7"] = {"lines": ["`01:00` 🎯 achetée"], "card": "Albert Camus (L)",
                                          "message": {"via": "webhook", "url": HOOK, "id": "55"}}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(404, json={"message": "Unknown Message"})
        return httpx.Response(200, json={"id": "56"})

    n._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n.start()
    n.info("Carte mise en vente", "x", topic=Topic.SALES, thread=position_thread(BID, "💰 en vente"))
    await n.close()
    assert [m for m, _ in calls] == ["PATCH", "DELETE", "POST"]
    assert n.store.data["discord:position:7"]["message"]["id"] == "56"


@pytest.mark.asyncio
async def test_position_message_goes_through_the_bot_with_its_buttons():
    posted, edited, deleted = [], [], []
    n = DiscordNotifier(HOOK, dry_run=False)
    n.store = Kv()

    async def with_buttons(actions, embed, url, mention):
        posted.append(actions)
        return f"9{len(posted)}"

    async def bot_edit(url, message_id, embed, actions):
        edited.append((message_id, actions))
        return True

    async def bot_delete(url, message_id):
        deleted.append(message_id)
        return True

    n.with_buttons, n.bot_edit, n.bot_delete = with_buttons, bot_edit, bot_delete
    n.start()
    n.success("Position achetée !", "", thread=position_thread(BID, "🎯 achetée", ring=True))
    n.info("Revente sans acheteur", "", actions={"kind": "relist", "listing": 3},
           thread=position_thread(BID, "↩️ sans acheteur"))
    n.success("Revente réussie", "", thread=position_thread(BID, "✅ vendue", ring=True))
    await n.close()
    assert posted == [None, None]  # sans boutons, mais par le bot : modifiable ensuite
    assert edited == [("91", {"kind": "relist", "listing": 3})]
    assert deleted == ["91"]
