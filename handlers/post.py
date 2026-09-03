"""Post jobs: normally just the comment thread.

Instagram post pages turned out to serve no usable JSON at all - the profile
timeline response already carries every metric for a dozen posts at once. So
the profile crawl stores the metrics, and this handler has two paths:

1. **Fast path** (the normal one): the crawl stored a `media_pk`, so we call the
   comments endpoint directly from a parked instagram.com page. No post page is
   ever loaded.
2. **Slow path**: `instascrape add post <shortcode>` for a post no crawl has
   seen. Loads the page, captures whatever it serves, parses metrics from the
   payload, and falls back to a deliberately strict DOM read.

Comments come from Instagram's own endpoint via `fetch` inside the page, so the
request carries the session cookies.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from playwright.sync_api import Page

from .. import parsers
from ..browser import assert_no_checkpoint, goto
from ..capture import PayloadCapture
from ..models import Comment, Post, PostResult, TerminalJobError
from ..repo import media_pk_for, record_raw_payload, upsert_comments, upsert_post
from ..runtime import RunContext

POST_URL = "https://www.instagram.com/p/{shortcode}/"

# Long-standing public web app id, used only as a fallback when we cannot read
# the real one out of the page.
FALLBACK_APP_ID = "936619743392459"

MISSING_MARKERS = (
    "Sorry, this page isn't available",
    "the link you followed may be broken",
    "Esta página no está disponible",
)

# Fetch executed inside the page, so it carries the logged-in session.
JS_FETCH = """
async ([url, appId]) => {
  try {
    const res = await fetch(url, {
      headers: { 'X-IG-App-ID': appId, 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'include',
    });
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    return null;
  }
}
"""


def _app_id(page: Page) -> str:
    try:
        html = page.content()
    except Exception:
        return FALLBACK_APP_ID
    match = re.search(r'"APP_ID"\s*:\s*"(\d+)"', html) or re.search(
        r'"X-IG-App-ID"\s*:\s*"(\d+)"', html
    )
    return match.group(1) if match else FALLBACK_APP_ID


def _dismiss_overlays(page: Page) -> None:
    """Close the cookie banner and the 'log in' interstitial if they appear.

    Declining optional cookies is the privacy-preserving default.
    """
    for selector in (
        "button:has-text('Decline optional cookies')",
        "button:has-text('Only allow essential cookies')",
        "button:has-text('Not Now')",
        "div[role='dialog'] button[aria-label='Close']",
    ):
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=1200):
                element.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            continue


def _media_id(payloads: list[Any], shortcode: str) -> Optional[str]:
    """The numeric media pk for `shortcode`, from any captured payload."""
    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in parsers.walk(data):
            if parsers._shortcode_of(node) != shortcode:
                continue
            found = parsers.media_pk_of(node)
            if found:
                return found
    return None


def _counts_from_dom(page: Page) -> tuple[Optional[int], Optional[int]]:
    """Last-resort like/comment counts scraped off the rendered page.

    Deliberately strict. An earlier version regex-matched digits out of the
    whole article text and stored 2,078 for a post with 505,311 likes - a
    plausible-looking number that was simply wrong, with nothing to flag it.
    Now we only accept a value from an element that explicitly labels itself,
    and return None whenever we are not confident. A visible NULL beats a
    silent lie.
    """
    likes = comments = None

    # Instagram exposes the like total in an aria-label / link text such as
    # "505,311 likes". Require the word adjacent to the number, anchored.
    for selector in (
        "a[href$='/liked_by/']",
        "section span:has-text('likes')",
        "[aria-label$='likes']",
    ):
        try:
            text = page.locator(selector).first.inner_text(timeout=2000)
        except Exception:
            continue
        match = re.fullmatch(r"\s*([\d.,]+)\s+likes?\s*", text or "", re.IGNORECASE)
        if match:
            digits = re.sub(r"[.,]", "", match.group(1))
            if digits.isdigit():
                likes = int(digits)
                break

    try:
        text = page.locator("a[href$='/comments/'], span").filter(
            has_text=re.compile(r"^View all [\d.,]+ comments?$", re.IGNORECASE)
        ).first.inner_text(timeout=2000)
        match = re.search(r"([\d.,]+)", text or "")
        if match:
            digits = re.sub(r"[.,]", "", match.group(1))
            if digits.isdigit():
                comments = int(digits)
    except Exception:
        pass

    return likes, comments


def _fetch_comments(
    page: Page, media_id: str, shortcode: str, limit: int, include_replies: bool
) -> list[Comment]:
    """Page through the comments endpoint until `limit` is reached."""
    app_id = _app_id(page)
    collected: dict[str, Comment] = {}
    next_min_id: Optional[str] = None

    while len(collected) < limit:
        url = (
            f"/api/v1/media/{media_id}/comments/"
            f"?can_support_threading=true&permalink_enabled=false"
        )
        if next_min_id:
            url += f"&min_id={next_min_id}"

        try:
            data = page.evaluate(JS_FETCH, [url, app_id])
        except Exception:
            break
        if not data:
            break

        batch = parsers.parse_comments([data], shortcode, include_replies)
        new = [c for c in batch if c.id not in collected]
        for comment in new:
            collected[comment.id] = comment

        next_min_id = data.get("next_min_id") if isinstance(data, dict) else None
        if not next_min_id or not new:
            break

        page.wait_for_timeout(900)  # do not hammer

    return list(collected.values())[:limit]


def scrape_post(ctx: RunContext, shortcode: str) -> PostResult:
    settings = ctx.settings
    page = ctx.new_page()
    capture = PayloadCapture(page, settings.raw_dir, shortcode)

    try:
        goto(page, POST_URL.format(shortcode=shortcode))
        assert_no_checkpoint(page)
        _dismiss_overlays(page)
        page.wait_for_timeout(2500)

        payloads = capture.flush()

        if not payloads:
            body = (page.content() or "")[:20000]
            if any(marker in body for marker in MISSING_MARKERS):
                raise TerminalJobError(f"post {shortcode} does not exist or was removed")

        for payload in payloads:
            if payload.path is not None:
                record_raw_payload(
                    ctx.conn, shortcode, payload.url, str(payload.path)
                )

        post = parsers.find_post(payloads, shortcode)
        source = "api"

        if post is None or post.like_count is None:
            likes, comment_count = _counts_from_dom(page)
            if post is None:
                post = Post(shortcode=shortcode, like_count=likes, comment_count=comment_count)
                source = "dom"
            else:
                post.like_count = post.like_count if post.like_count is not None else likes
                post.comment_count = (
                    post.comment_count if post.comment_count is not None else comment_count
                )
                source = "api+dom"

        comments: list[Comment] = parsers.parse_comments(
            payloads, shortcode, settings.fetch_replies
        )

        media_id = _media_id(payloads, shortcode)
        if media_id and len(comments) < settings.max_comments:
            fetched = _fetch_comments(
                page,
                media_id,
                shortcode,
                settings.max_comments,
                settings.fetch_replies,
            )
            merged = {c.id: c for c in comments}
            for comment in fetched:
                merged[comment.id] = comment
            comments = list(merged.values())

        return PostResult(
            post=post, comments=comments[: settings.max_comments], source=source
        )

    finally:
        capture.detach()
        try:
            page.close()
        except Exception:
            pass


def _comments_only(ctx: RunContext, shortcode: str, media_pk: str) -> str:
    """Fetch just the comment thread, with no post page load at all.

    The profile crawl already stored this post's metrics and media id, so the
    only thing left is the comments - and those come from a JSON endpoint we
    call ourselves. Reusing one parked instagram.com page means a 12-post crawl
    costs 1 navigation instead of 13.
    """
    settings = ctx.settings
    page = ctx.api_page()

    comments = _fetch_comments(
        page,
        media_pk,
        shortcode,
        settings.max_comments,
        settings.fetch_replies,
    )
    stored = upsert_comments(ctx.conn, comments)
    return f"{shortcode}: {stored} comment(s) via api (no page load)"


def handle(ctx: RunContext, payload: dict) -> str:
    """Job entry point. Returns a one-line summary for the log."""
    shortcode = payload.get("shortcode")
    if not shortcode:
        raise TerminalJobError("post job is missing 'shortcode'")

    ctx.limiter.check_budget()
    ctx.limiter.wait()

    # Fast path: the profile crawl already gave us the metrics and the media id.
    media_pk = media_pk_for(ctx.conn, shortcode)
    if media_pk:
        summary = _comments_only(ctx, shortcode, media_pk)
        ctx.limiter.record_action()
        return summary

    # Slow path, for `instascrape add post <shortcode>` on a post no crawl has
    # seen: load the page, capture whatever it serves, parse metrics + comments.
    result = scrape_post(ctx, shortcode)
    ctx.limiter.record_action()

    upsert_post(ctx.conn, result.post, source=result.source)
    stored = upsert_comments(ctx.conn, result.comments)

    post = result.post
    return (
        f"{shortcode}: likes={post.like_count} comments={post.comment_count} "
        f"views={post.view_count} stored_comments={stored} via={result.source}"
    )
