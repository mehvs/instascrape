"""The job queue.

A worker is just: claim a job -> do it -> ack/nack -> repeat. Claiming happens
inside a `BEGIN IMMEDIATE` transaction so two workers can never take the same
row, which is what makes it safe to run more than one worker against one
database.

All queue access goes through the `JobQueue` protocol. Swapping SQLite for a
hosted Postgres later means writing one new class that satisfies this protocol
- handler code never touches SQL.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional, Protocol

from .models import (
    STATUS_DEAD,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Job,
)


class JobQueue(Protocol):
    def put(self, kind: str, payload: dict[str, Any], dedupe_key: Optional[str] = None) -> Optional[int]: ...
    def claim(self, kinds: list[str], worker_id: str) -> Optional[Job]: ...
    def ack(self, job_id: int) -> None: ...
    def nack(self, job_id: int, error: str, retry: bool = True) -> None: ...
    def reclaim_expired(self, lease_seconds: int) -> int: ...
    def stats(self) -> dict[str, int]: ...
    def next_retry_at(self) -> Optional[int]: ...


def _backoff_seconds(attempts: int) -> int:
    """60s, 2m, 4m, 8m ... capped at 1h."""
    return min(60 * (2 ** max(0, attempts - 1)), 3600)


class SQLiteQueue:
    """SQLite-backed implementation of JobQueue."""

    def __init__(self, conn: sqlite3.Connection, max_attempts: int = 5) -> None:
        self.conn = conn
        self.max_attempts = max_attempts

    # --- writing -------------------------------------------------------------

    def put(
        self,
        kind: str,
        payload: dict[str, Any],
        dedupe_key: Optional[str] = None,
    ) -> Optional[int]:
        """Enqueue a job. Returns the new job id, or None if `dedupe_key`
        matched a job that already exists."""
        now = int(time.time())
        cur = self.conn.execute(
            """
            INSERT INTO jobs (kind, payload, status, run_after, dedupe_key,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            (kind, json.dumps(payload), STATUS_PENDING, now, dedupe_key, now, now),
        )
        return cur.lastrowid if cur.rowcount else None

    # --- claiming ------------------------------------------------------------

    def claim(self, kinds: list[str], worker_id: str) -> Optional[Job]:
        """Atomically take the oldest runnable job of one of `kinds`."""
        now = int(time.time())
        placeholders = ",".join("?" for _ in kinds)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                f"""
                SELECT id, kind, payload, attempts
                FROM jobs
                WHERE status IN ('{STATUS_PENDING}', '{STATUS_FAILED}')
                  AND run_after <= ?
                  AND kind IN ({placeholders})
                ORDER BY run_after ASC, id ASC
                LIMIT 1
                """,
                (now, *kinds),
            ).fetchone()

            if row is None:
                self.conn.execute("COMMIT")
                return None

            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, attempts = attempts + 1,
                    locked_by = ?, locked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_RUNNING, worker_id, now, now, row["id"]),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            attempts=row["attempts"] + 1,
        )

    # --- finishing -----------------------------------------------------------

    def ack(self, job_id: int) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, locked_by = NULL, locked_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_DONE, now, job_id),
        )

    def nack(self, job_id: int, error: str, retry: bool = True) -> None:
        """Return a job to the queue with backoff, or bury it.

        `retry=False` buries immediately - used for failures that can never
        succeed, like a username that does not exist.
        """
        now = int(time.time())
        row = self.conn.execute(
            "SELECT attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        attempts = row["attempts"] if row else self.max_attempts

        if not retry or attempts >= self.max_attempts:
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, locked_by = NULL, locked_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_DEAD, error[:2000], now, job_id),
            )
            return

        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, locked_by = NULL, locked_at = NULL,
                last_error = ?, run_after = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                STATUS_FAILED,
                error[:2000],
                now + _backoff_seconds(attempts),
                now,
                job_id,
            ),
        )

    # --- recovery ------------------------------------------------------------

    def reclaim_expired(self, lease_seconds: int) -> int:
        """Put jobs from crashed workers back on the queue.

        A worker that is killed mid-job leaves its row in 'running' forever.
        Anything locked longer than the lease is assumed dead.
        """
        now = int(time.time())
        cutoff = now - lease_seconds
        cur = self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, locked_by = NULL, locked_at = NULL,
                last_error = 'reclaimed after lease expiry', updated_at = ?
            WHERE status = ? AND locked_at IS NOT NULL AND locked_at < ?
            """,
            (STATUS_PENDING, now, STATUS_RUNNING, cutoff),
        )
        return cur.rowcount

    # --- introspection -------------------------------------------------------

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def next_retry_at(self) -> Optional[int]:
        """Epoch seconds of the soonest job still waiting out its backoff.

        Without this, a queue holding only backing-off jobs looks identical to
        an empty one, and the worker reports "nothing to do" when in fact work
        is pending.
        """
        row = self.conn.execute(
            """
            SELECT MIN(run_after) AS soonest
            FROM jobs
            WHERE status IN (?, ?) AND run_after > ?
            """,
            (STATUS_PENDING, STATUS_FAILED, int(time.time())),
        ).fetchone()
        return int(row["soonest"]) if row and row["soonest"] is not None else None
