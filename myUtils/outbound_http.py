"""One policy for fetching a user-supplied URL (Phase 2 hardening).

Media can be pulled from a stored public URL (the ``share`` backend registers
a URL rather than an object key). Two different callers did that with two
different levels of care, so this module is the single implementation:

* **scheme** must be HTTPS;
* **host** must resolve only to globally routable addresses — a name that
  points at loopback, a private range, or the cloud metadata endpoint is
  refused, which is what stops a stored URL from being used to probe the
  host's own network (SSRF);
* **redirects** are followed manually and every hop is re-validated, because
  the first host being public says nothing about where it redirects;
* **size** and **total time** are bounded, so a hostile or broken endpoint
  cannot fill the disk or hold a worker forever.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

#: Redirect hops allowed before giving up.
MAX_REDIRECTS = 5


class OutboundUrlError(ValueError):
    """The URL, or something it redirected to, is not safe to fetch."""


def resolve_public_addresses(host: str, port: int = 443) -> list[str]:
    """Return the host's addresses, or raise if any is not globally routable.

    All resolved addresses are checked: a name with both a public and a
    private record must not be usable to reach the private one.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundUrlError(f"host does not resolve: {host!r}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise OutboundUrlError(f"host resolved to no addresses: {host!r}")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address.split("%")[0])
        except ValueError as exc:
            raise OutboundUrlError(f"unparsable address for {host!r}") from exc
        if not parsed.is_global:
            raise OutboundUrlError(
                f"host {host!r} resolves to a non-public address ({address}); refusing"
            )
    return addresses


def assert_public_https_url(url: str) -> str:
    """Validate scheme/host/addresses and return the URL unchanged."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError as exc:
        raise OutboundUrlError("malformed URL") from exc
    if parts.scheme != "https":
        raise OutboundUrlError("only https URLs may be fetched")
    host = (parts.hostname or "").strip()
    if not host:
        raise OutboundUrlError("URL has no host")
    if parts.username or parts.password:
        raise OutboundUrlError("URL must not carry credentials")
    port = parts.port or 443
    if port != 443:
        raise OutboundUrlError("only the default https port is allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise OutboundUrlError("URL host is not public")
    resolve_public_addresses(host, port)
    return url


def fetch_public_bytes(
    url: str,
    *,
    max_bytes: int = 512 * 1024 * 1024,
    timeout: float = 30.0,
    total_deadline: float = 300.0,
    session=None,
) -> bytes:
    """Fetch ``url``, validating every hop, bounded by size and total time."""
    http = session or requests
    deadline = time.monotonic() + total_deadline
    current = assert_public_https_url(url)
    for _hop in range(MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise OutboundUrlError("fetch exceeded its total time budget")
        with http.get(current, timeout=timeout, stream=True, allow_redirects=False) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise OutboundUrlError("redirect without a Location header")
                current = assert_public_https_url(urljoin(current, location))
                continue
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            declared_bytes = int(declared) if declared and str(declared).isdigit() else None
            if declared_bytes is not None and declared_bytes > max_bytes:
                raise OutboundUrlError(
                    f"object is larger than the {max_bytes}-byte limit"
                )
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_bytes:
                    raise OutboundUrlError(
                        f"object exceeded the {max_bytes}-byte limit while streaming"
                    )
                if time.monotonic() > deadline:
                    raise OutboundUrlError("fetch exceeded its total time budget")
                chunks.append(chunk)
            if received == 0:
                raise OutboundUrlError("object was empty")
            # A truncated body must never be written out as if it were whole.
            # The previous implementation compared against Content-Length; that
            # check has to survive any rewrite of this function.
            if declared_bytes is not None and received != declared_bytes:
                raise OutboundUrlError(
                    f"object was incomplete: got {received} of {declared_bytes} bytes"
                )
            return b"".join(chunks)
    raise OutboundUrlError("too many redirects")


def download_public_file(
    url: str,
    destination: Path,
    *,
    max_bytes: int = 512 * 1024 * 1024,
    timeout: float = 30.0,
    total_deadline: float = 300.0,
    session=None,
) -> Path:
    """Fetch ``url`` to ``destination`` atomically after full validation."""
    payload = fetch_public_bytes(
        url, max_bytes=max_bytes, timeout=timeout,
        total_deadline=total_deadline, session=session,
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        temporary.write_bytes(payload)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
