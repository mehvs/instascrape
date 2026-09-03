"""Job handlers, keyed by job kind."""

from ..models import KIND_POST, KIND_PROFILE
from . import post, profile

HANDLERS = {
    KIND_PROFILE: profile.handle,
    KIND_POST: post.handle,
}

__all__ = ["HANDLERS", "post", "profile"]
