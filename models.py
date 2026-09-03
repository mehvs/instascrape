"""Plain data carriers passed between handlers, parsers and the repository.

Deliberately dumb: no persistence logic, no SQL. Every scraped field is
Optional because Instagram's payload shape changes without notice - a missing
field degrades one column rather than failing the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# --- Queue -------------------------------------------------------------------

KIND_PROFILE = "profile"
KIND_POST = "post"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"


@dataclass
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int = 0


# --- Scraped entities --------------------------------------------------------


@dataclass
class Profile:
    username: str
    user_id: Optional[str] = None
    full_name: Optional[str] = None
    followers: Optional[int] = None
    post_count: Optional[int] = None
    is_private: Optional[bool] = None


@dataclass
class Post:
    shortcode: str
    # Numeric media id. Needed to call the comments endpoint, so we store it
    # rather than re-deriving it on every comment fetch.
    media_pk: Optional[str] = None
    owner_username: Optional[str] = None
    caption: Optional[str] = None
    taken_at: Optional[str] = None
    media_type: Optional[str] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    view_count: Optional[int] = None
    # Public reshare count (`media_repost_count`): how many people reposted
    # this. This is NOT the Insights "shares" metric, which also counts DM and
    # story sends - it is the closest public equivalent. Kept as its own field
    # so the two are never conflated.
    repost_count: Optional[int] = None
    # Instagram does not expose share counts publicly; only Insights on posts
    # you own has them. The column exists so the schema is stable if that ever
    # changes, but nothing writes to it today.
    share_count: Optional[int] = None


@dataclass
class Comment:
    id: str
    shortcode: str
    author: Optional[str] = None
    text: Optional[str] = None
    like_count: Optional[int] = None
    created_at: Optional[str] = None
    parent_id: Optional[str] = None
    # 'gif' when the comment is a Giphy sticker rather than words. Those have a
    # genuinely empty `text`, and without this an empty row looks like a bug.
    media_kind: Optional[str] = None


@dataclass
class PostResult:
    """What a post handler produces, regardless of whether the numbers came
    from an intercepted API payload or the DOM fallback."""

    post: Post
    comments: list[Comment] = field(default_factory=list)
    source: str = "api"  # "api" | "dom"


class TerminalJobError(Exception):
    """Raised when a job can never succeed - a 404 username, a private account.

    The worker records these and moves on instead of burning retries.
    """
