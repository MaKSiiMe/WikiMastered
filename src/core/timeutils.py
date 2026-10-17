"""Heure UTC et de Paris, fenêtres horaires, attentes."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def set_timezone(name: str) -> None:
    global PARIS
    PARIS = ZoneInfo(name)


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(PARIS)


def in_window(moment: datetime, start: time, end: time) -> bool:
    """Vrai si l'heure locale de `moment` est dans [start, end[, fenêtre éventuellement à cheval sur minuit."""
    t = moment.astimezone(PARIS).time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def local_dt(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=PARIS)


def next_local(at: time, after: datetime | None = None) -> datetime:
    after = (after or now_local()).astimezone(PARIS)
    candidate = local_dt(after.date(), at)
    return candidate if candidate > after else local_dt(after.date() + timedelta(days=1), at)


def random_in_window(day: date, start: time, end: time, rng: random.Random | None = None) -> datetime:
    rng = rng or random
    begin = local_dt(day, start)
    finish = local_dt(day, end)
    if finish <= begin:
        finish += timedelta(days=1)
    return begin + timedelta(seconds=rng.uniform(0, (finish - begin).total_seconds()))


async def sleep_until(moment: datetime) -> None:
    delay = (moment - utcnow()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
