"""Decide how a user-supplied file may be served (Phase 2 hardening).

``/getFile`` hands back files that arrived from an upload. Flask infers the
Content-Type from the filename, so an upload named ``evil.html`` (or a
``.svg`` carrying script) was previously returned **inline** from the
application's own origin — active content on the origin that holds the bearer
token and the session cookie.

The rule here is allow-list based: only the media types the product actually
publishes are served inline. Everything else is forced to a download with an
opaque ``application/octet-stream`` type, so a browser never renders it.

This module is pure (no Flask, no I/O) so the policy is directly testable.
"""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

#: Extensions served inline, with the type used for them. An explicit table
#: (rather than "anything that starts with image/") because the browser's
#: mimetypes database also maps things like ``.svg`` — which can execute
#: script — under the image/ prefix.
INLINE_TYPES: dict[str, str] = {
    # images
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    # video
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    # audio
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

#: Used for anything not in :data:`INLINE_TYPES`.
OPAQUE_TYPE = "application/octet-stream"


def extension_of(filename: str) -> str:
    """Lower-cased extension (with dot) of a filename or path."""
    return PurePosixPath(str(filename or "").replace("\\", "/")).suffix.lower()


def should_serve_inline(filename: str) -> bool:
    """True only for an explicitly approved, non-executable media type."""
    return extension_of(filename) in INLINE_TYPES


def content_type_for(filename: str) -> str:
    """The Content-Type to send.

    An approved media type keeps its real type (players need it, and the
    provider fetchers check it). Anything else is deliberately opaque.
    """
    extension = extension_of(filename)
    if extension in INLINE_TYPES:
        return INLINE_TYPES[extension]
    return OPAQUE_TYPE


def disposition_for(filename: str) -> str:
    """``inline`` for approved media, ``attachment`` for everything else."""
    return "inline" if should_serve_inline(filename) else "attachment"


def guessed_type(filename: str) -> str:
    """What the browser/OS mimetypes database would infer (for diagnostics)."""
    return mimetypes.guess_type(str(filename or ""))[0] or OPAQUE_TYPE
