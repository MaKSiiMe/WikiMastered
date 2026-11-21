"""Temps réel du site (src/core/realtime.py) : protocole Phoenix des canaux privés, relevé du 21/11 dans son code."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core import realtime as rt_module
from src.core.models import Auction
from src.core.realtime import Realtime
from src.workers.auction_sniper import LISTEN_AFTER_END, Target
from tests.test_sniper_outcomes import sale, start


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        return await self.inbox.get()

    def push(self, message: dict) -> None:
        self.inbox.put_nowait(json.dumps(message))


def fake_client(token: dict) -> SimpleNamespace:
    settings = SimpleNamespace(supabase_anon_key="anon", supabase_url="")
    return SimpleNamespace(settings=settings,
                           auth_session=lambda: SimpleNamespace(ref="proj", data={"access_token": token["value"]}))


async def settle_loop() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_private_channels_are_joined_and_broadcasts_delivered(monkeypatch):
    monkeypatch.setattr(rt_module, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(rt_module, "TOKEN_CHECK_SECONDS", 0.01)
    socket, urls, token, got = FakeSocket(), [], {"value": "t1"}, []

    async def connect(url):
        urls.append(url)
        return socket

    realtime = Realtime(fake_client(token), connect=connect)
    realtime.subscribe("auction:a1", lambda event, payload: got.append((event, payload)))
    task = asyncio.create_task(realtime.run())
    await settle_loop()
    assert urls == ["wss://proj.supabase.co/realtime/v1/websocket?apikey=anon&vsn=1.0.0"]
    join = socket.sent[0]
    assert join["topic"] == "realtime:auction:a1" and join["event"] == "phx_join"
    assert join["payload"]["config"]["private"] is True and join["payload"]["access_token"] == "t1"
    socket.push({"topic": "realtime:auction:a1", "event": "phx_reply", "payload": {"status": "ok"}, "ref": "1"})
    socket.push({"topic": "realtime:auction:a1", "event": "broadcast",
                 "payload": {"type": "broadcast", "event": "BID", "payload": {"bid": {"amount": 120}}}})
    socket.push({"topic": "realtime:autre", "event": "broadcast", "payload": {"event": "BID", "payload": {}}})
    await settle_loop()
    assert got == [("BID", {"bid": {"amount": 120}})] and realtime.events == 1
    token["value"] = "t2"  # session renouvelée : jeton renvoyé sur les canaux ouverts
    await asyncio.sleep(0.05)
    assert any(m["event"] == "access_token" and m["payload"] == {"access_token": "t2"} for m in socket.sent)
    assert any(m["topic"] == "phoenix" and m["event"] == "heartbeat" for m in socket.sent)
    realtime.unsubscribe("auction:a1")
    await settle_loop()
    assert socket.sent[-1]["event"] == "phx_leave" and not realtime.watching("auction:a1")
    task.cancel()


@pytest.mark.asyncio
async def test_refused_subscriptions_switch_realtime_off():
    socket = FakeSocket()

    async def connect(_url):
        return socket

    realtime = Realtime(fake_client({"value": "t"}), connect=connect)
    for n in range(3):
        realtime.subscribe(f"auction:{n}", lambda e, p: None)
    task = asyncio.create_task(realtime.run())
    await settle_loop()
    for n in range(3):
        socket.push({"topic": f"realtime:auction:{n}", "event": "phx_reply",
                     "payload": {"status": "error", "response": {"reason": "Unauthorized"}}})
    await settle_loop()
    assert realtime.disabled == "abonnements refusés par le site"
    realtime.subscribe("auction:x", lambda e, p: None)
    assert not realtime.watching("auction:x")
    task.cancel()


class RecordingRealtime:
    def __init__(self):
        self.handlers: dict = {}
        self.left: list[str] = []
        self.events = 0
        self.connected, self.disabled = True, ""

    def subscribe(self, name, handler):
        self.handlers[name] = handler

    def unsubscribe(self, name):
        self.left.append(name)
        self.handlers.pop(name, None)


@pytest.mark.asyncio
async def test_sniper_rereads_an_auction_outbid_by_someone_else_first(tmp_path):
    sniper, db, _ = await start(tmp_path, {}, [])
    sniper.ctx.realtime = RecordingRealtime()
    auction = Auction.model_validate(sale("a1", "Ada", 60))
    sniper._listen(auction)
    handler = sniper.ctx.realtime.handlers["auction:a1"]
    now = datetime.now(UTC)
    sniper._targets["a1"] = Target(auction, now, recheck=True)
    handler("BID", {"bid": {"amount": 30, "bidder_id": sniper.api.user_id}})  # sa propre mise : rien
    handler("UPDATE", {})
    assert sniper._pushed == []
    later = (now + timedelta(minutes=2)).isoformat()
    handler("BID", {"bid": {"amount": 40, "bidder_id": "rival"}, "end_at": later})
    handler("BID", {"bid": {"amount": 50, "bidder_id": "rival"}, "end_at": later})
    assert sniper._pushed == ["a1"] and sniper._listened["a1"] == datetime.fromisoformat(later)
    assert sniper._pushed_target() is sniper._targets["a1"] and sniper._pushed_target() is None
    sniper._forget_listened(datetime.fromisoformat(later) + LISTEN_AFTER_END + timedelta(seconds=1))
    assert sniper.ctx.realtime.left == ["auction:a1"] and sniper._listened == {}
    await db.close()


@pytest.mark.asyncio
async def test_wishlist_notification_makes_the_listings_read_now(tmp_path):
    sniper, db, _ = await start(tmp_path, {}, [])
    sniper.ctx.realtime = RecordingRealtime()
    sniper._listen_notifications()
    handler = sniper.ctx.realtime.handlers[f"notifications:{sniper.api.user_id}"]
    sniper._last_read[("wishlist", "")] = datetime.now(UTC)
    handler("INSERT", {"record": {"type": "marketplace_outbid"}})
    assert sniper._last_read[("wishlist", "")] > datetime(2000, 1, 1, tzinfo=UTC)
    handler("INSERT", {"record": {"type": "marketplace_wishlist_listed", "data": {"auction_id": "x"}}})
    assert sniper._last_read[("wishlist", "")] == datetime.min.replace(tzinfo=UTC)
    await db.close()
