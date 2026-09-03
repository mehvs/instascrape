"""Everything a handler needs, bundled into one object.

Keeping this separate avoids a circular import between the worker and the
handlers it dispatches to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from typing import Optional

from playwright.sync_api import BrowserContext, Page

from .config import Settings
from .queue import JobQueue
from .ratelimit import RateLimiter


@dataclass
class RunContext:
    settings: Settings
    conn: sqlite3.Connection
    queue: JobQueue
    limiter: RateLimiter
    browser: BrowserContext

    # A single page parked on instagram.com, reused for API calls. Created
    # lazily and kept for the life of the worker.
    _api_page: Optional[Page] = None

    def new_page(self) -> Page:
        page = self.browser.new_page()
        page.set_default_navigation_timeout(self.settings.nav_timeout_ms)
        page.set_default_timeout(self.settings.nav_timeout_ms)
        return page

    def api_page(self) -> Page:
        """A page on the instagram.com origin, for same-origin `fetch` calls.

        The comments endpoint is requested from inside the page so it inherits
        the session cookies, which means we need *a* page on that origin - but
        not a freshly loaded post page for every post. One navigation per worker
        run instead of one per job.
        """
        if self._api_page is not None and not self._api_page.is_closed():
            return self._api_page

        from .browser import INSTAGRAM, goto

        page = self.new_page()
        goto(page, f"{INSTAGRAM}/")
        page.wait_for_timeout(1500)
        self._api_page = page
        return page
