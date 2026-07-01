#!/usr/bin/env python3
"""Local SOCKS5 proxy forwarder for authenticated remote proxies.

Chromium/Playwright does not support SOCKS5 proxy authentication.
This script runs a local unauthenticated SOCKS5 server that forwards
all connections through an authenticated remote SOCKS5 proxy.

Usage:
    python proxy_forwarder.py --remote "socks5://user:pass@host:port" --local-port 1080
"""
from __future__ import annotations

import argparse
import logging
import socket
import struct
import threading

import socks

log = logging.getLogger(__name__)


def _parse_proxy_url(url: str) -> dict:
    """Parse socks5://user:pass@host:port into components."""
    url = url.replace("socks://", "socks5://", 1)
    if not url.startswith("socks5://"):
        raise ValueError(f"Not a SOCKS5 URL: {url}")
    rest = url[len("socks5://"):]
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        username, password = creds.split(":", 1)
    else:
        hostport = rest
        username = password = None
    host, port = hostport.rsplit(":", 1)
    return {"host": host, "port": int(port), "username": username, "password": password}


def _handle_client(client_sock: socket.socket, remote_cfg: dict):
    """Handle a single SOCKS5 client by forwarding through remote proxy."""
    try:
        # Connect to remote SOCKS5 proxy
        remote = socks.socksocket()
        remote.set_proxy(
            socks.SOCKS5,
            remote_cfg["host"],
            remote_cfg["port"],
            username=remote_cfg["username"],
            password=remote_cfg["password"],
        )
        remote.settimeout(30)

        # Read SOCKS5 handshake from client
        header = client_sock.recv(2)
        if len(header) < 2:
            return
        version, nmethods = struct.unpack("BB", header)
        methods = client_sock.recv(nmethods)

        # Reply: no auth required
        client_sock.sendall(b"\x05\x00")

        # Read SOCKS5 request
        req = client_sock.recv(4)
        if len(req) < 4:
            return
        ver, cmd, _, atyp = struct.unpack("BBBB", req)

        if atyp == 1:  # IPv4
            addr = socket.inet_ntoa(client_sock.recv(4))
        elif atyp == 3:  # Domain
            length = client_sock.recv(1)[0]
            addr = client_sock.recv(length).decode()
        elif atyp == 4:  # IPv6
            addr = socket.inet_ntop(socket.AF_INET6, client_sock.recv(16))
        else:
            return

        port = struct.unpack("!H", client_sock.recv(2))[0]

        # Connect through remote proxy
        remote.connect((addr, port))

        # Reply success
        client_sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

        # Bidirectional relay
        def relay(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    src.close()
                except Exception:
                    pass
                try:
                    dst.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=relay, args=(client_sock, remote), daemon=True)
        t2 = threading.Thread(target=relay, args=(remote, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    except Exception as e:
        log.error("Connection error: %s", e)
        try:
            client_sock.close()
        except Exception:
            pass


def run_server(remote_url: str, local_port: int = 1080):
    """Run the local SOCKS5 forwarder server."""
    cfg = _parse_proxy_url(remote_url)
    log.info("Forwarding local:%d -> %s@%s:%d", local_port,
             cfg["username"] or "(no auth)", cfg["host"], cfg["port"])

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(100)
    log.info("Local SOCKS5 proxy listening on 127.0.0.1:%d", local_port)

    while True:
        client, addr = server.accept()
        t = threading.Thread(target=_handle_client, args=(client, cfg), daemon=True)
        t.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", required=True, help="Remote SOCKS5 proxy URL")
    parser.add_argument("--local-port", type=int, default=1080)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    run_server(args.remote, args.local_port)
