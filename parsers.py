"""Turn captured JSON into models.

Instagram serves several shapes for the same data (old GraphQL `edge_*` nodes,
newer `/api/v1/` flat fields, and server-rendered bootstrap blobs), and which
one you get varies by endpoint and by week. So nothing here walks a fixed path.

Instead we recursively search every captured payload for the dict that carries
the post, then read whichever field variant that dict happens to have. Every
field is optional: a rename costs one column, not the job.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .models import Comment, Post, Profile

# Keys that mark a dict as "this is a media node", used to avoid matching an
# unrelated dict that happens to have a `code` key.
MEDIA_HINTS = (
    "taken_at",
    "taken_at_timestamp",
    "like_count",
    "edge_media_preview_like",
    "edge_liked_by",
    "media_type",
    "is_video",
    "image_versions2",
    "display_url",
    "edge_media_to_caption",
)

# Keys that ONLY ever appear on a media node. Used to tell posts from comments.
# Deliberately narrower than MEDIA_HINTS: `like_count` and `edge_liked_by` are
# ambiguous (comments carry them too) so they must not appear here.
STRICT_MEDIA_KEYS = (
    "shortcode",
    "code",
    "taken_at_timestamp",
    "display_url",
    "image_versions2",
    "media_type",
    "is_video",
    "product_type",
    "edge_media_to_caption",
    "edge_media_preview_like",
    "edge_media_to_parent_comment",
    "edge_media_to_comment",
    "video_view_count",
    "play_count",
)

SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,30}$")


# --- generic helpers ---------------------------------------------------------


def walk(node: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside `node`."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _count(node: dict, *keys: str) -> Optional[int]:
    """Read the first present key, transparently unwrapping {"count": n}."""
    for key in keys:
        if key not in node:
            continue
        value = node[key]
        if isinstance(value, dict) and "count" in value:
            found = _int(value["count"])
        else:
            found = _int(value)
        if found is not None:
            return found
    return None


def _iso(value: Any) -> Optional[str]:
    """Epoch seconds -> ISO8601. Passes through strings unchanged."""
    if isinstance(value, str):
        return value or None
    seconds = _int(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _looks_like_media(node: dict) -> bool:
    return any(hint in node for hint in MEDIA_HINTS)


def _shortcode_of(node: dict) -> Optional[str]:
    for key in ("shortcode", "code"):
        value = node.get(key)
        if isinstance(value, str) and SHORTCODE_RE.match(value):
            return value
    return None


# --- field extraction --------------------------------------------------------


def _username_of(node: dict) -> Optional[str]:
    for key in ("owner", "user"):
        holder = node.get(key)
        if isinstance(holder, dict):
            name = holder.get("username")
            if isinstance(name, str) and name:
                return name
    name = node.get("username")
    return name if isinstance(name, str) and name else None


def _caption_of(node: dict) -> Optional[str]:
    caption = node.get("caption")
    if isinstance(caption, str) and caption:
        return caption
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text:
            return text

    edges = (node.get("edge_media_to_caption") or {}).get("edges")
    if isinstance(edges, list) and edges:
        text = ((edges[0] or {}).get("node") or {}).get("text")
        if isinstance(text, str) and text:
            return text
    return None


def _media_type_of(node: dict) -> Optional[str]:
    if node.get("product_type") == "clips" or node.get("is_reel"):
        return "reel"

    typename = node.get("__typename")
    if isinstance(typename, str):
        if "Sidecar" in typename:
            return "carousel"
        if "Video" in typename:
            return "video"
        if "Image" in typename:
            return "image"

    media_type = _int(node.get("media_type"))
    if media_type is not None:
        return {1: "image", 2: "video", 8: "carousel"}.get(media_type)

    if isinstance(node.get("is_video"), bool):
        return "video" if node["is_video"] else "image"
    return None


def media_pk_of(node: dict) -> Optional[str]:
    """The numeric media id used by the comments endpoint.

    GraphQL serves it as "<media>_<owner>" in `id` and bare in `pk`; either is
    acceptable, we keep the media half.
    """
    for key in ("pk", "id"):
        value = node.get(key)
        if isinstance(value, (str, int)):
            candidate = str(value).split("_")[0]
            if candidate.isdigit():
                return candidate
    return None


def post_from_node(node: dict, shortcode: str) -> Post:
    return Post(
        shortcode=shortcode,
        media_pk=media_pk_of(node),
        owner_username=_username_of(node),
        caption=_caption_of(node),
        taken_at=_iso(node.get("taken_at_timestamp") or node.get("taken_at")),
        media_type=_media_type_of(node),
        like_count=_count(
            node, "edge_media_preview_like", "edge_liked_by", "like_count"
        ),
        comment_count=_count(
            node,
            "edge_media_to_parent_comment",
            "edge_media_to_comment",
            "comment_count",
        ),
        view_count=_count(
            node, "video_view_count", "video_play_count", "play_count", "view_count"
        ),
        repost_count=_count(node, "media_repost_count", "reshare_count"),
        # share_count stays None - see models.Post.
    )


def _score(node: dict) -> int:
    """Prefer the richest node when a payload contains several copies."""
    return sum(1 for hint in MEDIA_HINTS if hint in node)


def find_post(payloads: list[Any], shortcode: str) -> Optional[Post]:
    """Search every payload for the best node describing `shortcode`."""
    best: Optional[dict] = None
    best_score = -1
    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in walk(data):
            if _shortcode_of(node) != shortcode or not _looks_like_media(node):
                continue
            score = _score(node)
            if score > best_score:
                best, best_score = node, score
    return post_from_node(best, shortcode) if best is not None else None


def extract_posts(payloads: list[Any]) -> list[Post]:
    """Every post described anywhere in the payloads, best node per shortcode.

    A profile timeline response carries complete metrics for a dozen posts at
    once. Pulling only the shortcodes out of it - and then re-fetching each post
    individually - throws away data we have already paid a request for, so this
    harvests all of them.
    """
    best: dict[str, tuple[int, dict]] = {}

    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in walk(data):
            if not _looks_like_media(node):
                continue
            code = _shortcode_of(node)
            if not code:
                continue
            score = _score(node)
            if code not in best or score > best[code][0]:
                best[code] = (score, node)

    return [post_from_node(node, code) for code, (_rank, node) in best.items()]


# --- profiles ----------------------------------------------------------------


# Fields worth having on a profile. Used to rank candidate nodes - a payload
# typically carries the same user several times at different levels of detail.
PROFILE_FIELDS = (
    "edge_followed_by",
    "follower_count",
    "edge_owner_to_timeline_media",
    "media_count",
    "biography",
    "full_name",
    "is_private",
    "is_verified",
    "id",
    "pk",
)


def find_profile(payloads: list[Any], username: str) -> Optional[Profile]:
    """Best available profile for `username`, or None if the name never appears.

    Deliberately lenient. The posts-feed query carries the owner with only
    username/id/full_name/is_private, while follower and post counts live in a
    separate request that may not have been captured. Storing the partial row
    beats storing nothing - consistent with every other field being Optional.
    """
    target = username.lower().lstrip("@")
    best: Optional[dict] = None
    best_score = -1

    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in walk(data):
            name = node.get("username")
            if not isinstance(name, str) or name.lower() != target:
                continue
            score = sum(1 for key in PROFILE_FIELDS if key in node)
            if score > best_score:
                best, best_score = node, score

    if best is None:
        return None

    return Profile(
        username=best.get("username") or target,
        user_id=str(best.get("id") or best.get("pk") or "") or None,
        full_name=best.get("full_name") or None,
        followers=_count(best, "edge_followed_by", "follower_count"),
        post_count=_count(best, "edge_owner_to_timeline_media", "media_count"),
        is_private=best.get("is_private")
        if isinstance(best.get("is_private"), bool)
        else None,
    )


def extract_shortcodes(payloads: list[Any]) -> list[str]:
    """Every post shortcode in the payloads, in first-seen order."""
    seen: dict[str, None] = {}
    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in walk(data):
            if not _looks_like_media(node):
                continue
            code = _shortcode_of(node)
            if code:
                seen.setdefault(code, None)
    return list(seen)


def extract_end_cursor(payloads: list[Any]) -> Optional[str]:
    """Pagination cursor, if the payload carries one and says there is more."""
    for payload in payloads:
        data = getattr(payload, "data", payload)
        for node in walk(data):
            info = node.get("page_info")
            if isinstance(info, dict) and info.get("has_next_page"):
                cursor = info.get("end_cursor")
                if isinstance(cursor, str) and cursor:
                    return cursor
            if node.get("more_available") and isinstance(node.get("next_max_id"), str):
                return node["next_max_id"]
    return None


# --- comments ----------------------------------------------------------------


def _comment_from_node(
    node: dict, shortcode: str, parent_id: Optional[str]
) -> Optional[Comment]:
    raw_id = node.get("id") or node.get("pk")
    if raw_id is None:
        return None
    text = node.get("text")
    if not isinstance(text, str):
        return None
    # A Giphy comment has no words at all. Recording that explicitly stops an
    # empty `text` from reading like a scraping failure.
    media_kind = "gif" if isinstance(node.get("giphy_media_info"), dict) else None
    return Comment(
        id=str(raw_id),
        shortcode=shortcode,
        parent_id=parent_id,
        author=_username_of(node),
        text=text,
        like_count=_count(node, "edge_liked_by", "comment_like_count", "like_count"),
        created_at=_iso(node.get("created_at") or node.get("created_at_utc")),
        media_kind=media_kind,
    )


def _comment_containers(data: Any) -> tuple[list[dict], bool]:
    """Comment nodes taken from explicit containers rather than by walking.

    Walking every dict looking for "has an id and a text field" also matches the
    post's own caption, which the comments endpoint returns as a sibling of the
    comment list - so the caption was being stored as a comment on every post.
    Reading the declared containers instead is exact.

    Returns (nodes, saw_container). The flag matters: the last page of a
    paginated thread carries an *empty* comments list plus the caption, and
    treating "empty" as "no container here" sent us back to walking - which
    picked the caption straight back up.
    """
    found: list[dict] = []
    saw_container = False

    for node in walk(data):
        listed = node.get("comments")
        if isinstance(listed, list):
            saw_container = True
            found.extend(item for item in listed if isinstance(item, dict))

        for key in ("edge_media_to_parent_comment", "edge_media_to_comment"):
            container = node.get(key)
            if isinstance(container, dict) and isinstance(container.get("edges"), list):
                saw_container = True
                found.extend(
                    edge["node"]
                    for edge in container["edges"]
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
                )

    return found, saw_container


def _is_comment_node(node: dict) -> bool:
    has_id = "id" in node or "pk" in node
    has_text = isinstance(node.get("text"), str)
    # Media nodes also carry an id and text-ish fields, so exclude anything
    # unambiguously media-shaped. Note this uses STRICT_MEDIA_KEYS, not
    # MEDIA_HINTS: a GraphQL comment has `edge_liked_by` for its own likes, and
    # checking the wider set would silently discard every comment.
    is_media = any(key in node for key in STRICT_MEDIA_KEYS)
    return has_id and has_text and not is_media


def parse_comments(
    payloads: list[Any],
    shortcode: str,
    include_replies: bool = False,
) -> list[Comment]:
    """Pull comments out of any payload shape, deduped by comment id."""
    found: dict[str, Comment] = {}

    for payload in payloads:
        data = getattr(payload, "data", payload)
        # Declared containers first; only fall back to walking the whole payload
        # when a shape turns up that has none.
        nodes, saw_container = _comment_containers(data)
        if not saw_container:
            nodes = [n for n in walk(data) if _is_comment_node(n)]

        # Belt and braces: never accept the post's own caption, whichever path
        # produced the nodes.
        caption_ids = {
            str(node["caption"].get("pk") or node["caption"].get("id"))
            for node in walk(data)
            if isinstance(node.get("caption"), dict)
            and (node["caption"].get("pk") or node["caption"].get("id"))
        }
        if caption_ids:
            nodes = [
                n
                for n in nodes
                if str(n.get("pk") or n.get("id")) not in caption_ids
            ]

        for node in nodes:
            if not _is_comment_node(node):
                continue
            comment = _comment_from_node(node, shortcode, None)
            if comment is None:
                continue
            found.setdefault(comment.id, comment)

            if not include_replies:
                continue
            replies = node.get("preview_comments") or (
                node.get("edge_threaded_comments") or {}
            ).get("edges")
            for reply_node in walk(replies):
                if not _is_comment_node(reply_node):
                    continue
                reply = _comment_from_node(reply_node, shortcode, comment.id)
                if reply is not None and reply.id != comment.id:
                    found[reply.id] = reply

    return list(found.values())
