"""Outbound request guard.

13ft fetches whatever URL it is given. Without a guard, any web page you visit
could point your browser at ``http://127.0.0.1:5000/http://192.168.1.1/...``
and make the server send requests into your home network. This module only
lets requests go to public http(s) addresses and re-checks every redirect hop,
because a public URL can redirect to a private one.

Remaining gap: the address is checked when the URL is resolved, and the HTTP
library resolves it again when it connects (a "DNS rebinding" window). For a
single-user tool that is an acceptable trade-off; set ``ALLOW_PRIVATE_TARGETS=1``
to switch the guard off entirely (for example to read pages on your own LAN).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import requests

MAX_REDIRECTS = 10
_DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedTarget(ValueError):
    """Raised when a URL points at a scheme or address we refuse to fetch."""

    def __init__(self, url, reason):
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


def private_targets_allowed():
    return os.environ.get("ALLOW_PRIVATE_TARGETS", "").strip().lower() in ("1", "true", "yes", "on")


def is_public_ip(address):
    """True for globally routable unicast addresses."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host, port):
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return {info[4][0] for info in infos}


def check_url(url):
    """Raise BlockedTarget unless *url* is http(s) and resolves to public addresses only."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise BlockedTarget(url, "Malformed URL") from None
    if parts.scheme not in _DEFAULT_PORTS:
        raise BlockedTarget(url, "Only http and https URLs are supported")
    host = parts.hostname
    if not host:
        raise BlockedTarget(url, "URL has no host name")
    if private_targets_allowed():
        return

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(host):
            raise BlockedTarget(url, "Refusing to fetch a private or local address")
        return

    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise BlockedTarget(url, "Refusing to fetch a private or local address")

    try:
        addresses = resolve(host, port or _DEFAULT_PORTS[parts.scheme])
    except (socket.gaierror, UnicodeError):
        # Not resolvable from here. The request itself will either fail the same
        # way or go through a configured HTTP proxy, which resolves it instead.
        return
    if any(not is_public_ip(address) for address in addresses):
        raise BlockedTarget(url, "Refusing to fetch a private or local address")


def guarded_get(url, *, headers=None, timeout=30, params=None, allow_redirects=True):
    """``requests.get`` with :func:`check_url` applied to the URL and every redirect.

    A fresh session is used per call, so cookies set during a redirect chain are
    kept for that chain but never carried over to the next article. That also
    resets "N free articles" meters that live in cookies.
    """
    session = requests.Session()
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            response = session.get(
                current,
                headers=headers,
                timeout=timeout,
                params=params,
                allow_redirects=False,
            )
            params = None  # already encoded into response.url
            if allow_redirects and response.is_redirect:
                current = urljoin(response.url, response.headers["location"])
                response.close()
                continue
            return response
        raise requests.exceptions.TooManyRedirects(f"Exceeded {MAX_REDIRECTS} redirects", response=response)
    finally:
        session.close()
