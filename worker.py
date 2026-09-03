"""The worker loop.

Claim a job -> run it -> ack or nack -> repeat. That is the whole model; there
is nothing hidden. Because claiming is atomic, you can run this on more than one
machine against the same database without them stepping on each other.

Shutdown is graceful: SIGINT/SIGTERM finishes nothing new, and the in-flight job
is returned to the queue rather than left locked.
"""

from __future__ import annotations

import signal
import time
from typing import Optional

from .browser import CheckpointRequired, browser_context, has_session_cookie
from .config import Settings
from .db import init as db_init
from .handlers import HANDLERS
from .models import KIND_POST, KIND_PROFILE, TerminalJobError
from .queue import SQLiteQueue
from .ratelimit import DailyCapReached, RateLimiter, seconds_until_utc_midnight
from .runtime import RunContext


class _Shutdown:
    """Flips to True on the first signal so the loop can exit cleanly."""

    def __init__(self) -> None:
        self.requested = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass  # not on the main thread, or unsupported on this platform

    def _handle(self, signum, frame) -> None:  # noqa: ANN001
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print("\nShutdown requested - finishing the current job, press again to force.")


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(
    settings: Settings,
    once: bool = False,
    kinds: Optional[list[str]] = None,
    max_jobs: Optional[int] = None,
) -> int:
    """Run the worker. Returns a process exit code."""
    settings.ensure_dirs()
    kinds = kinds or [KIND_PROFILE, KIND_POST]

    conn = db_init(settings.db_path)
    queue = SQLiteQueue(conn, max_attempts=settings.max_attempts)
    limiter = RateLimiter(
        conn,
        min_delay=settings.min_delay,
        max_delay=settings.max_delay,
        daily_cap=settings.daily_cap,
    )

    reclaimed = queue.reclaim_expired(settings.lease_seconds)
    if reclaimed:
        _log(f"Reclaimed {reclaimed} job(s) from a previous run.")

    if not settings.storage_state_path.exists():
        _log("No saved session found. Run `instascrape login` first.")
        return 2

    shutdown = _Shutdown()
    processed = 0
    exit_code = 0

    with browser_context(settings, use_state=True) as (_browser, browser):
        if not has_session_cookie(browser):
            _log("Saved session has no cookie. Run `instascrape login` again.")
            return 2

        ctx = RunContext(
            settings=settings,
            conn=conn,
            queue=queue,
            limiter=limiter,
            browser=browser,
        )

        _log(
            f"Worker {settings.worker_id} started. "
            f"{limiter.remaining_today()} of {settings.daily_cap} left today."
        )

        while not shutdown.requested:
            if max_jobs is not None and processed >= max_jobs:
                break

            job = queue.claim(kinds, settings.worker_id)

            if job is None:
                if once:
                    soonest = queue.next_retry_at()
                    if soonest is not None:
                        wait = max(0, soonest - int(time.time()))
                        _log(
                            f"Nothing runnable yet - a job is waiting out its retry "
                            f"backoff for another {wait}s "
                            f"(at {time.strftime('%H:%M:%S', time.localtime(soonest))}). "
                            f"Re-run then, or `instascrape retry` to clear the backoff now."
                        )
                    else:
                        _log("Queue empty - nothing to do.")
                    break
                time.sleep(settings.poll_interval)
                continue

            handler = HANDLERS.get(job.kind)
            if handler is None:
                queue.nack(job.id, f"no handler for kind '{job.kind}'", retry=False)
                continue

            _log(f"job {job.id} [{job.kind}] attempt {job.attempts} -> {job.payload}")

            try:
                summary = handler(ctx, job.payload)
                queue.ack(job.id)
                processed += 1
                _log(f"job {job.id} done: {summary}")

            except TerminalJobError as exc:
                queue.nack(job.id, str(exc), retry=False)
                _log(f"job {job.id} will never succeed: {exc}")

            except CheckpointRequired as exc:
                # A human has to clear this. Put the job back untouched and stop.
                queue.nack(job.id, str(exc), retry=True)
                _log(f"STOPPING - {exc}")
                exit_code = 3
                break

            except DailyCapReached as exc:
                queue.nack(job.id, str(exc), retry=True)
                if once:
                    _log(f"{exc}")
                    break
                wait = seconds_until_utc_midnight()
                _log(f"{exc} - sleeping {wait / 3600:.1f}h.")
                slept = 0.0
                while slept < wait and not shutdown.requested:
                    time.sleep(min(30.0, wait - slept))
                    slept += 30.0

            except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
                queue.nack(job.id, f"{type(exc).__name__}: {exc}", retry=True)
                _log(f"job {job.id} failed, will retry: {type(exc).__name__}: {exc}")

            if once and max_jobs is None:
                break

    _log(f"Worker stopped after {processed} job(s).")
    conn.close()
    return exit_code
