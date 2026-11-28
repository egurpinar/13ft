"""Optional source: render the page in headless Chromium with paywall scripts blocked.

Some sites only build the article with JavaScript, so a plain HTTP fetch gets
an empty shell. Rendering in a real browser engine fixes that, and refusing
to load scripts from paywall vendors stops the overlay from being injected.

Off by default because it needs Playwright and a Chromium download (several
hundred MB) and takes a few seconds per page. Enable with ``HEADLESS=on``
after ``pip install -r requirements-headless.txt && playwright install chromium``.
Not available in the Alpine Docker image (Playwright needs glibc).
"""
from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlsplit

from . import netguard

# Script hosts of common paywall / subscription vendors. A starting list, not a
# complete one: extend it with BLOCK_SCRIPT_HOSTS="host1,host2".
PAYWALL_SCRIPT_HOSTS = (
    "piano.io",        # Piano (Composer, ID, VX)
    "tinypass.com",    # Piano's older domain
    "cxense.com",      # Piano DMP (formerly Cxense)
    "poool.fr",        # Poool
    "pelcro.com",      # Pelcro
    "laterpay.net",    # LaterPay
    "steadyhq.com",    # Steady
    "evolok.net",      # Evolok
    "sophi.io",        # Sophi
    "zephr.com",       # Zephr
)
# Resource types that are never needed to read the text.
SKIPPED_RESOURCE_TYPES = {"image", "media", "font"}

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def enabled():
    if os.environ.get("HEADLESS", "off").strip().lower() not in ("1", "on", "true", "yes"):
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def blocked_script_hosts():
    extra = [h.strip().lower() for h in os.environ.get("BLOCK_SCRIPT_HOSTS", "").split(",") if h.strip()]
    return tuple(PAYWALL_SCRIPT_HOSTS) + tuple(extra)


def is_blocked_host(host, blocked=None):
    host = (host or "").lower().rstrip(".")
    return any(host == b or host.endswith("." + b) for b in (blocked or blocked_script_hosts()))


@lru_cache(maxsize=512)
def _host_allowed(scheme, host, port):
    try:
        netguard.check_url(f"{scheme}://{host}:{port}/" if port else f"{scheme}://{host}/")
    except netguard.BlockedTarget:
        return False
    return True


def _proxy_settings():
    server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not server:
        return None
    settings = {"server": server}
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        settings["bypass"] = bypass
    return settings


def fetch_rendered(url, timeout=30):
    """Return (html, final_url) after rendering *url*, or raise on failure."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    netguard.check_url(url)
    blocked = blocked_script_hosts()

    def route_handler(route):
        request = route.request
        parts = urlsplit(request.url)
        if parts.scheme not in ("http", "https"):
            return route.continue_()
        if request.resource_type in SKIPPED_RESOURCE_TYPES or is_blocked_host(parts.hostname, blocked):
            return route.abort()
        if not _host_allowed(parts.scheme, parts.hostname or "", parts.port):
            return route.abort()
        return route.continue_()

    launch = {"headless": True}
    if os.environ.get("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]
    proxy = _proxy_settings()
    if proxy:
        launch["proxy"] = proxy

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch)
        try:
            context = browser.new_context(
                user_agent=BROWSER_USER_AGENT,
                locale="en-US",
                service_workers="block",
            )
            page = context.new_page()
            page.route("**/*", route_handler)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeout:
                pass  # long-polling pages never go idle; what we have is enough
            # Redirects followed inside the browser are not routed one by one,
            # so check where we ended up before handing the content back.
            netguard.check_url(page.url)
            return page.content(), page.url
        finally:
            browser.close()
