"""Pacing and daily caps.

The whole point of this module is to be slow. Instagram's abuse detection keys
on regularity and volume, so requests get a randomized gap rather than a fixed
one, and a hard daily ceiling that survives process restarts (it lives in the
database, not in memory).

There is deliberately no CAPTCHA handling anywhere in this project. When a
checkpoint appears the worker stops and asks for a human.
"""

from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime, timezone


class DailyCapReached(Exception):
    """Raised when the day's budget is spent. The worker sleeps until UTC midnight."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def seconds_until_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow.timestamp() + 86_400) - now.timestamp()


class RateLimiter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        min_delay: float,
        max_delay: float,
        daily_cap: int,
        counter_name: str = "posts_scraped",
    ) -> None:
        self.conn = conn
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.daily_cap = daily_cap
        self.counter_name = counter_name
        self._last_action: float | None = None

    # --- daily budget --------------------------------------------------------

    def used_today(self) -> int:
        row = self.conn.execute(
            "SELECT day, value FROM counters WHERE name = ?", (self.counter_name,)
        ).fetchone()
        if row is None or row["day"] != _today():
            return 0
        return int(row["value"])

    def remaining_today(self) -> int:
        return max(0, self.daily_cap - self.used_today())

    def check_budget(self) -> None:
        if self.remaining_today() <= 0:
            raise DailyCapReached(
                f"daily cap of {self.daily_cap} reached; resets at UTC midnight"
            )

    def record_action(self) -> None:
        """Count one unit of work against today's budget."""
        today = _today()
        self.conn.execute(
            """
            INSERT INTO counters (name, day, value) VALUES (?, ?, 1)
            ON CONFLICT (name) DO UPDATE SET
                value = CASE WHEN counters.day = excluded.day
                             THEN counters.value + 1 ELSE 1 END,
                day = excluded.day
            """,
            (self.counter_name, today),
        )
        self._last_action = time.monotonic()

    # --- pacing --------------------------------------------------------------

    def next_delay(self) -> float:
        return random.uniform(self.min_delay, self.max_delay)

    def wait(self) -> float:
        """Sleep out the randomized gap since the previous action.

        Returns the number of seconds actually slept, so the worker can log it.
        """
        target = self.next_delay()
        if self._last_action is None:
            return 0.0
        elapsed = time.monotonic() - self._last_action
        remaining = target - elapsed
        if remaining <= 0:
            return 0.0
        time.sleep(remaining)
        return remaining
