"""Crawl a profile and fan out one post job per shortcode found.

This is the entry point for the whole pipeline: you add a username, this
handler turns it into N post jobs, and the worker chews through them at its own
pace. Scrolling is how the profile grid paginates, so we scroll and re-flush the
capture buffer until we have enough shortcodes or the page stops producing new
ones.
"""

from __future__ import annotations

from playwright.sync_api import Page

from .. import parsers
from ..browser import assert_no_checkpoint, goto
from ..capture import PayloadCapture
from ..models import KIND_POST, TerminalJobError
from ..repo import has_comments, record_raw_payload, upsert_post, upsert_profile
from ..runtime import RunContext

PROFILE_URL = "https://www.instagram.com/{username}/"

MISSING_MARKERS = (
    "Sorry, this page isn't available",
    "user not found",
)

PRIVATE_MARKERS = (
    "This account is private",
    "This Account is Private",
)

def _effective_limit(limit: int) -> float:
    """`limit <= 0` means unlimited.

    Returned as a float so `>=` comparisons work without special-casing. Note
    that 0 must be handled explicitly everywhere: `payload.get("limit") or 30`
    would quietly turn an unlimited request into 30.
    """
    return float("inf") if limit is None or limit <= 0 else float(limit)


def _dismiss_overlays(page: Page) -> None:
    for selector in (
        "button:has-text('Decline optional cookies')",
        "button:has-text('Only allow essential cookies')",
        "button:has-text('Not Now')",
    ):
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=1200):
                element.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            continue


def crawl_profile(
    ctx: RunContext, username: str, limit: int
) -> tuple[list[str], object, int]:
    """Return (shortcodes, profile-or-None, posts-stored) for a username."""
    username = username.strip().lstrip("@")
    settings = ctx.settings
    page = ctx.new_page()
    capture = PayloadCapture(page, settings.raw_dir, f"profile-{username}")

    try:
        goto(page, PROFILE_URL.format(username=username))
        assert_no_checkpoint(page)
        _dismiss_overlays(page)
        page.wait_for_timeout(2500)

        body = (page.content() or "")[:20000]
        if any(marker in body for marker in MISSING_MARKERS):
            raise TerminalJobError(f"profile @{username} does not exist")

        payloads = capture.flush()
        profile = parsers.find_profile(payloads, username)

        if profile is not None and profile.is_private:
            upsert_profile(ctx.conn, profile)
            raise TerminalJobError(
                f"@{username} is private and this account does not follow it"
            )
        if any(marker in body for marker in PRIVATE_MARKERS):
            raise TerminalJobError(
                f"@{username} is private and this account does not follow it"
            )

        shortcodes = parsers.extract_shortcodes(payloads)

        # The grid lazy-loads. Scroll until we have enough or it stops growing.
        # With an unlimited request `target` is inf, so only the stall check
        # (three scrolls with no new posts) or max_scrolls ends the loop.
        target = _effective_limit(limit)
        stalled = 0
        for _ in range(settings.max_scrolls):
            if len(shortcodes) >= target or stalled >= 3:
                break
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(1800)
            payloads = capture.flush()
            found = parsers.extract_shortcodes(payloads)
            if len(found) > len(shortcodes):
                shortcodes = found
                stalled = 0
            else:
                stalled += 1

        for payload in payloads:
            if payload.path is not None:
                record_raw_payload(ctx.conn, None, payload.url, str(payload.path))

        if profile is None:
            profile = parsers.find_profile(payloads, username)
        if profile is not None:
            upsert_profile(ctx.conn, profile)

        # The timeline response already carries full metrics for every post it
        # returned - likes, comment counts, reposts, caption, media type. Store
        # all of them, not just the first `limit`: the request is already paid
        # for, and re-fetching a post page to get the same numbers costs a page
        # load, a rate-limit slot, and (as it turned out) yields worse data.
        stored = 0
        for post in parsers.extract_posts(payloads):
            upsert_post(ctx.conn, post, source="api")
            stored += 1

        capped = shortcodes if target == float("inf") else shortcodes[:limit]
        return capped, profile, stored

    finally:
        capture.detach()
        try:
            page.close()
        except Exception:
            pass


def handle(ctx: RunContext, payload: dict) -> str:
    username = (payload.get("username") or "").strip().lstrip("@")
    if not username:
        raise TerminalJobError("profile job is missing 'username'")

    # Explicit None check: `payload.get("limit") or 30` would turn 0, which
    # means unlimited, into 30.
    raw_limit = payload.get("limit")
    limit = 0 if raw_limit is None else int(raw_limit)
    refresh = bool(payload.get("refresh"))
    want_comments = payload.get("comments", True)

    ctx.limiter.check_budget()
    ctx.limiter.wait()

    shortcodes, _profile, stored = crawl_profile(ctx, username, limit)
    ctx.limiter.record_action()

    if not want_comments:
        return (
            f"@{username}: stored metrics for {stored} post(s); "
            "no comment jobs queued (--no-comments)"
        )

    queued = skipped = 0
    for shortcode in shortcodes:
        if not refresh and has_comments(ctx.conn, shortcode):
            skipped += 1
            continue
        job_id = ctx.queue.put(
            KIND_POST,
            {"shortcode": shortcode, "owner": username},
            dedupe_key=None if refresh else f"post:{shortcode}",
        )
        if job_id is None:
            skipped += 1
        else:
            queued += 1

    return (
        f"@{username}: stored metrics for {stored} post(s); "
        f"queued {queued} comment job(s), skipped {skipped}"
    )
