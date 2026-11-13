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

import hashlib
import io
import ipaddress
import os
import queue
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

#: Redirect hops allowed before giving up.
MAX_REDIRECTS = 5
_READ_SLOTS = threading.BoundedSemaphore(16)


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


def bounded_receive(reader, sink, *, total_deadline: float) -> dict:
    """Hard wall deadline even if DNS/headers/a chunk dribbles indefinitely.

    The bounded read thread can ONLY send chunks through a one-item handoff;
    all file writes stay on the owning thread. Timeout/cancellation therefore
    cannot leave an abandoned thread writing a deleted/reused staging path.
    Socket shutdown unblocks normal HTTP body reads; stalled DNS has no file
    access and consumes one of a finite number of daemon read slots.
    ``reader(handoff, stopped, register_response)`` returns transfer facts.
    """
    deadline = time.monotonic() + total_deadline
    if total_deadline <= 0 or not _READ_SLOTS.acquire(timeout=max(0, total_deadline)):
        raise OutboundUrlError("fetch exceeded its total time budget")
    messages = queue.Queue(maxsize=1)
    stopped = threading.Event()
    responses = []

    def send(kind, value):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stopped.is_set():
            raise OutboundUrlError("fetch exceeded its total time budget")
        messages.put((kind, value), timeout=remaining)

    class Handoff:
        def write(self, chunk):
            consumed = threading.Event()
            send("chunk", (chunk, consumed))
            if not consumed.wait(max(0, deadline - time.monotonic())) or stopped.is_set():
                raise OutboundUrlError("fetch exceeded its total time budget")

    def run():
        try:
            result = reader(Handoff(), stopped, responses.append)
            send("done", result)
        except BaseException as exc:
            try:
                send("error", exc)
            except (queue.Full, OutboundUrlError):
                pass
        finally:
            _READ_SLOTS.release()

    threading.Thread(target=run, name="bounded-media-read", daemon=True).start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OutboundUrlError("fetch exceeded its total time budget")
            try:
                kind, value = messages.get(timeout=remaining)
            except queue.Empty:
                raise OutboundUrlError("fetch exceeded its total time budget") from None
            if kind == "error":
                raise value
            if kind == "done":
                return value
            chunk, consumed = value
            try:
                sink.write(chunk)
            finally:
                consumed.set()
    finally:
        stopped.set()
        for response in responses:
            raw = getattr(response, "raw", response)
            connection = getattr(raw, "_connection", None)
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def _receive_public(url, sink, *, max_bytes, timeout, total_deadline, session=None,
                    head_only=False) -> dict:
    return bounded_receive(
        lambda handoff, stopped, register: _receive_public_sync(
            url, handoff, max_bytes=max_bytes, timeout=timeout, total_deadline=total_deadline,
            session=session, head_only=head_only, stopped=stopped, register_response=register),
        sink, total_deadline=total_deadline,
    )


def _receive_public_sync(url, sink, *, max_bytes, timeout, total_deadline, session=None,
                         head_only=False, stopped=None, register_response=None) -> dict:
    """One guarded request path, executed by the bounded read handoff."""
    if max_bytes <= 0 or timeout <= 0 or total_deadline <= 0:
        raise OutboundUrlError("invalid fetch budget")
    http = session or requests
    deadline = time.monotonic() + total_deadline
    current = assert_public_https_url(url)
    digest = hashlib.sha256()
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stopped.is_set():
            raise OutboundUrlError("fetch exceeded its total time budget")
        request = http.head if head_only else http.get
        with request(current, timeout=min(timeout, remaining), stream=True,
                     allow_redirects=False) as response:
            register_response(response)
            if stopped.is_set():
                raise OutboundUrlError("fetch exceeded its total time budget")
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise OutboundUrlError("redirect without a Location header")
                current = assert_public_https_url(urljoin(current, location))
                continue
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared is not None and not str(declared).isdigit():
                raise OutboundUrlError("invalid Content-Length")
            declared_bytes = int(declared) if declared is not None else None
            if declared_bytes is not None and declared_bytes > max_bytes:
                raise OutboundUrlError("object is larger than the byte limit")
            facts = {"content_type": response.headers.get("Content-Type", ""),
                     "size_bytes": declared_bytes}
            if head_only:
                if time.monotonic() > deadline:
                    raise OutboundUrlError("fetch exceeded its total time budget")
                return facts  # ETag is deliberately NOT checksum proof.
            received = 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if time.monotonic() > deadline:
                    raise OutboundUrlError("fetch exceeded its total time budget")
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_bytes:
                    raise OutboundUrlError("object exceeded the byte limit while streaming")
                sink.write(chunk)
                digest.update(chunk)
            if time.monotonic() > deadline:
                raise OutboundUrlError("fetch exceeded its total time budget")
            if received == 0:
                raise OutboundUrlError("object was empty")
            if declared_bytes is not None and received != declared_bytes:
                raise OutboundUrlError("object was incomplete")
            facts.update(size_bytes=received, sha256=digest.hexdigest())
            return facts
    raise OutboundUrlError("too many redirects")


def fetch_public_bytes(url: str, *, max_bytes: int = 512 * 1024 * 1024,
                       timeout: float = 30.0, total_deadline: float = 300.0,
                       session=None) -> bytes:
    """Compatibility API for small in-memory callers; file restores stream."""
    sink = io.BytesIO()
    _receive_public(url, sink, max_bytes=max_bytes, timeout=timeout,
                    total_deadline=total_deadline, session=session)
    return sink.getvalue()


def probe_public_url(url: str, *, max_bytes: int = 512 * 1024 * 1024,
                     timeout: float = 10.0, total_deadline: float = 20.0,
                     session=None) -> dict:
    """Bounded read-only HEAD; availability/size is not integrity evidence."""
    return _receive_public(url, None, max_bytes=max_bytes, timeout=timeout,
                           total_deadline=total_deadline, session=session, head_only=True)


def download_public_file(url: str, destination: Path, *,
                         max_bytes: int = 512 * 1024 * 1024, timeout: float = 30.0,
                         total_deadline: float = 300.0, session=None, verifier=None,
                         facts: dict | None = None) -> Path:
    """Stream/hash into an exclusive sibling, validate, then atomically install.

    ``verifier(temporary_path, facts)`` may reject bytes before exposure. On
    cancellation or failure only this invocation's temporary file is removed.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part",
                                dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as sink:
            received = _receive_public(url, sink, max_bytes=max_bytes, timeout=timeout,
                                       total_deadline=total_deadline, session=session)
            sink.flush()
            os.fsync(sink.fileno())
        if verifier is not None:
            verifier(temporary, received)
        if facts is not None:
            facts.update(received)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
