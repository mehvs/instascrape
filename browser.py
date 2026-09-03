"""Playwright session management.

Auth is persisted to storage_state.json so login happens once rather than per
job. This module never sees, stores or transmits a password: `interactive_login`
opens a real browser window and waits for *you* to sign in by hand, then saves
only the resulting session cookies.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Iterator, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import Settings

INSTAGRAM = "https://www.instagram.com"

# URL fragments that mean "a human needs to look at this account".
CHECKPOINT_MARKERS = (
    "/challenge",
    "/accounts/suspended",
    "/accounts/disabled",
    "/checkpoint",
)


class CheckpointRequired(Exception):
    """Instagram is showing a challenge, checkpoint or suspension screen.

    We stop here on purpose. Solving these automatically is out of scope.
    """


class NotLoggedIn(Exception):
    """No valid session. Run `instascrape login`."""


def _launch_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]


@contextlib.contextmanager
def browser_context(
    settings: Settings,
    headless: Optional[bool] = None,
    use_state: bool = True,
) -> Iterator[tuple[Browser, BrowserContext]]:
    """Yield a configured browser + context, cleaning both up afterwards."""
    settings.ensure_dirs()
    is_headless = settings.headless if headless is None else headless

    state_path = settings.storage_state_path
    storage_state = (
        str(state_path) if use_state and state_path.exists() else None
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=is_headless, args=_launch_args())
        context = browser.new_context(
            storage_state=storage_state,
            viewport={
                "width": settings.viewport_width,
                "height": settings.viewport_height,
            },
            locale=settings.locale,
            timezone_id=settings.timezone,
        )
        context.set_default_navigation_timeout(settings.nav_timeout_ms)
        context.set_default_timeout(settings.nav_timeout_ms)
        try:
            yield browser, context
        finally:
            with contextlib.suppress(Exception):
                context.close()
            with contextlib.suppress(Exception):
                browser.close()


def has_session_cookie(context: BrowserContext) -> bool:
    """Instagram sets `sessionid` only once you are actually signed in."""
    return any(
        c.get("name") == "sessionid" and c.get("value")
        for c in context.cookies(INSTAGRAM)
    )


def save_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))


# Chromium network errors that mean "try again in a moment", not "this is
# broken". A WiFi handoff, a VPN toggling, or a dropped connection produces one
# of these mid-navigation and has nothing to do with the page or the account.
TRANSIENT_NET_ERRORS = (
    "ERR_NETWORK_CHANGED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_EMPTY_RESPONSE",
    "Timeout",
)


def is_transient_network_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_NET_ERRORS)


def goto(page: Page, url: str, attempts: int = 3, wait_until: str = "domcontentloaded"):
    """Navigate, retrying transient network failures.

    Without this, a one-second WiFi blip costs a whole job and a slot of the
    daily budget. Non-transient errors are re-raised immediately - we only want
    to absorb connectivity noise, not hide real failures.
    """
    delay = 2.0
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return page.goto(url, wait_until=wait_until)
        except Exception as exc:
            if not is_transient_network_error(exc):
                raise
            last = exc
            if attempt < attempts:
                print(
                    f"  network blip ({str(exc).splitlines()[0][:80]}) - "
                    f"retry {attempt}/{attempts - 1} in {delay:.0f}s"
                )
                time.sleep(delay)
                delay *= 2.5

    raise last  # type: ignore[misc]


def assert_no_checkpoint(page: Page) -> None:
    url = page.url or ""
    if any(marker in url for marker in CHECKPOINT_MARKERS):
        raise CheckpointRequired(
            f"Instagram is showing a challenge screen at {url}\n"
            "Open the account in a normal browser, clear the challenge by hand, "
            "then re-run `instascrape login`."
        )


# Pages Instagram shows mid-login that are NOT failures: an emailed code, 2FA,
# or the "save your login info?" prompt that appears once you are already in.
VERIFY_MARKERS = (
    "/challenge",
    "/auth_platform/codeentry",
    "/accounts/login/two_factor",
    "/two_factor",
    "/accounts/onetap",
    "/accounts/suspended",
)


def _current_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def interactive_login(settings: Settings, timeout_seconds: int = 600) -> bool:
    """Open a visible browser and wait for the user to sign in themselves.

    Returns True once a session cookie appears, False on timeout. The password
    is typed by the user into Instagram's own page - it never passes through
    this process.

    We poll the cookie jar from Python rather than checking `document.cookie` in
    the page: Instagram's `sessionid` is HttpOnly, so JavaScript cannot see it
    and a JS-based check would never fire. We also keep waiting through
    verification screens instead of treating "navigated away from /accounts/
    login" as success - that used to close the window mid-verification.
    """
    settings.ensure_dirs()
    with browser_context(settings, headless=False, use_state=True) as (_, context):
        page = context.new_page()
        page.goto(f"{INSTAGRAM}/accounts/login/", wait_until="domcontentloaded")

        if has_session_cookie(context):
            print("Already signed in - refreshing saved session.")
            save_state(context, settings.storage_state_path)
            return True

        print(
            "\n  A browser window is open.\n"
            "  Sign in there yourself. If Instagram emails or texts you a code,\n"
            "  enter it in that same window - this will keep waiting.\n"
            "  Do not close the window.\n"
            f"  Waiting up to {timeout_seconds // 60} minutes...\n"
        )

        deadline = time.monotonic() + timeout_seconds
        announced = False

        while time.monotonic() < deadline:
            # The cookie is the only reliable signal that login actually worked.
            if has_session_cookie(context):
                page.wait_for_timeout(2000)  # let any final redirect settle
                save_state(context, settings.storage_state_path)
                print(f"\nSigned in. Session saved to {settings.storage_state_path}")
                return True

            if not context.pages or page.is_closed():
                print("\nBrowser window was closed before sign-in completed.")
                return False

            url = _current_url(page)
            if not announced and any(marker in url for marker in VERIFY_MARKERS):
                print(
                    "  Instagram is asking you to verify. Enter the code in the\n"
                    "  browser window - still waiting, nothing has failed.\n"
                )
                announced = True

            page.wait_for_timeout(1500)

        print(
            "\nTimed out waiting for sign-in.\n"
            "Nothing was saved. Re-run `instascrape login` to try again "
            "(use --timeout to allow longer)."
        )
        return False


def check_session(settings: Settings) -> bool:
    """Load the saved state and confirm Instagram still accepts it."""
    if not settings.storage_state_path.exists():
        return False
    with browser_context(settings, headless=True, use_state=True) as (_, context):
        if not has_session_cookie(context):
            return False
        page = context.new_page()
        page.goto(f"{INSTAGRAM}/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        return "/accounts/login" not in (page.url or "")
