import ipaddress
import json
import os
import flask
import re
import requests
import uuid
import threading
import time
import math
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urljoin, urlparse, urlsplit, quote, unquote
from flask import request, Response
from bs4 import BeautifulSoup

from archivist import extract, headless, netguard, pages
from archivist import store as archive_store
from archivist import urls as urltools

app = flask.Flask(__name__)

# Outbound requests go through the network guard (public http(s) targets only,
# checked on every redirect hop). Tests replace this with a fake.
http_get = netguard.guarded_get

_DEFAULT_STRINGS = {
    "heading": "Paste a link to read it",
    "label": "Paste any article link. It opens as a clean page and is saved to your library.",
    "submit": "Read",
    "toggle_dark_mode": "Toggle Dark Mode",
    "status_heading": "Fetching Article",
    "error_title": "Something went wrong",
    "retry_button": "Try another link",
    "elapsed_template": "{seconds}s elapsed",
    "connection_lost_error": "Connection to server lost. The page may be taking too long to load.",
    "step_connect": "Connecting to website...",
    "step_fetch": "Downloading page content...",
    "step_detect": "Checking for anti-bot challenges...",
    "step_fallback_freedium": "Trying Freedium (Medium bypass)...",
    "step_fallback_org": "Trying archive.org snapshot...",
    "step_fallback_ph": "Trying archive.today / archive.ph snapshot...",
    "step_fallback_clean": "Trying a fresh browser request (no cookies)...",
    "step_fallback_headless": "Rendering in a headless browser with paywall scripts blocked...",
    "library_link": "Library",
    "paste_button": "Paste link from clipboard",
    "recent_heading": "Recently read",
    "library_all": "All saved articles",
    "step_queued": "Waiting for the previous article to finish...",
    "low_memory_error": (
        "This server is short on memory right now ({available} MB free, {needed} MB "
        "wanted), so nothing was fetched. Something else may be busy (on a Pi-hole, "
        "a camera live view); try again in a few minutes."
    ),
    "blocked_target_error": (
        "Refusing to fetch this address: it is not an http(s) URL, or it points "
        "to a private or local network. Set ALLOW_PRIVATE_TARGETS=1 if you "
        "really want to read pages from your own network."
    ),
    "nothing_found_error": "No readable copy of the article could be retrieved.",
    "step_process": "Processing article...",
    "step_cleanup": "Cleaning up & preparing view...",
    "step_done": "Done!",
    "invalid_url": "Invalid URL",
    "medium_challenge_error": (
        "Medium served an anti-bot challenge and Freedium / archive.org / "
        "archive.today all came back empty. The article may be too new to "
        "have been archived yet."
    ),
    "challenge_error": (
        "The site served an anti-bot challenge (Cloudflare or similar) "
        "and no usable snapshot was found on archive.org or archive.today. "
        "This site is actively blocking automated requests."
    ),
    "timeout_error": "The website took too long to respond (30s timeout). Try again later.",
    "connection_error": "Could not connect to the website. Check the URL and try again.",
    "request_error": "Failed to fetch the page.",
    "unexpected_error": "Unexpected error while fetching the page.",
}


def get_locale():
    locale = os.environ.get("LOCALE", "en")
    if not re.match(r"^[a-zA-Z0-9_-]+$", locale):
        locale = "en"
    return locale


@lru_cache(maxsize=None)
def load_strings_for_locale(locale):
    strings = dict(_DEFAULT_STRINGS)
    locale_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
    locale_file = os.path.join(locale_dir, f"{locale}.json")

    if locale != "en":
        try:
            with open(locale_file, "r", encoding="utf-8") as file:
                strings.update(json.load(file))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            strings = dict(_DEFAULT_STRINGS)
            locale = "en"

    strings["lang"] = locale
    return strings


def load_strings():
    return dict(load_strings_for_locale(get_locale()))


googlebot_headers = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.119 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
}

jobs = {}
jobs_lock = threading.Lock()


class UserFacingError(Exception):
    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message

# The home page. Kept under the upstream name; the template lives in archivist/pages.py.
html = pages.HOME_TEMPLATE


def build_steps(strings):
    # Ordered the way fetch_article() tries sources, so the progress bar only
    # moves forward. Each source is tried only if the previous ones did not
    # return a complete article.
    return [
        {"id": "queued", "label": strings["step_queued"]},
        {"id": "connect", "label": strings["step_connect"]},
        {"id": "fallback_freedium", "label": strings["step_fallback_freedium"]},
        {"id": "fetch", "label": strings["step_fetch"]},
        {"id": "fallback_clean", "label": strings["step_fallback_clean"]},
        {"id": "fallback_org", "label": strings["step_fallback_org"]},
        {"id": "fallback_ph", "label": strings["step_fallback_ph"]},
        {"id": "fallback_headless", "label": strings["step_fallback_headless"]},
        {"id": "process", "label": strings["step_process"]},
        {"id": "cleanup", "label": strings["step_cleanup"]},
        {"id": "done", "label": strings["step_done"]},
    ]


RECENT_ON_HOME = 8


def render_main_page(autostart=None):
    strings = load_strings()
    ui_strings = {
        "error_title": strings["error_title"],
        "retry_button": strings["retry_button"],
        "elapsed_template": strings["elapsed_template"],
        "connection_lost_error": strings["connection_lost_error"],
    }
    try:
        recent, _total = archive_store.get_store().search("", RECENT_ON_HOME)
    except Exception:  # the home page must work even if the archive cannot be opened
        app.logger.exception("Could not read the archive")
        recent = []
    return flask.render_template_string(
        html,
        steps=build_steps(strings),
        ui_strings=ui_strings,
        autostart=autostart,
        recent=recent,
        **strings,
    )


def process_html_document(html_content, original_url):
    """
    Parses HTML, injects the <base> tag to fix relative links, and applies
    site-specific modifications (like stripping paywall JS) where needed.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    parsed_url = urlparse(original_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    # Handle paths that are not root, e.g., "https://x.com/some/path/w.html"
    if parsed_url.path and not parsed_url.path.endswith("/"):
        base_url = urljoin(base_url, parsed_url.path.rsplit("/", 1)[0] + "/")

    base_tag = soup.find("base")
    if base_tag:
        # Always overwrite — sites may have <base href="/"> which would resolve
        # root-relative asset paths to the 13ft server instead of the original domain.
        base_tag["href"] = base_url
    else:
        new_base_tag = soup.new_tag("base", href=base_url)
        if soup.head:
            soup.head.insert(0, new_base_tag)
        else:
            head_tag = soup.new_tag("head")
            head_tag.insert(0, new_base_tag)
            soup.insert(0, head_tag)

    domain = parsed_url.netloc.lower()

    # --- GENERAL FIXES ---

    # Strip anti-hotlinking, anti-embedding, and frame-busting scripts.
    for script in soup.find_all("script", src=False):
        content = script.get_text()
        
        # Target the specific hostname check
        if "location.hostname" in content and ("location.href" in content or "location.replace" in content):
            script.decompose()
            
        # Optional: Catch common frame-busting scripts if you are viewing this via an iframe
        elif "top.location" in content or "window.top" in content:
            script.decompose()

    # --- SITE SPECIFIC FIXES ---

    # The Seattle Times
    if "seattletimes.com" in domain:
        # Seattle Times uses specific JS bundles to trigger the paywall modal and ads.
        # Removing these scripts prevents the paywall from locking the screen.
        for script in soup.find_all("script", src=True):
            if "st-user-messaging" in script["src"] or "st-advertising" in script["src"]:
                script.decompose()

        # Ensure the body isn't locked from scrolling by JS-injected inline styles
        if soup.body:
            current_style = soup.body.get("style", "")
            soup.body["style"] = f"{current_style}; overflow: auto !important; position: static !important;"

    # --- FAVICON OVERRIDE ---
    # Remove any existing favicon links from the source page and inject the
    # 13ft favicon so the browser tab always shows the 13ft logo.
    for link_tag in soup.find_all("link", rel=lambda r: r and any(
            v.lower() in ("icon", "shortcut icon", "apple-touch-icon",
                          "apple-touch-icon-precomposed", "mask-icon")
            for v in (r if isinstance(r, list) else [r])
    )):
        link_tag.decompose()

    favicon_tag = soup.new_tag("link", rel="icon", href="/favicon.ico", type="image/png")
    if soup.head:
        soup.head.append(favicon_tag)

    return str(soup)



CHALLENGE_SIGNATURES = [
    "just a moment",
    "checking your browser",
    "cloudflare",
    "cf-challenge",
    "ddos protection",
    "attention required",
    "enable javascript and cookies to continue",
    "please verify you are a human",
    # Imperva / Incapsula block pages (e.g. bizjournals.com). These are also
    # captured by archive.org, so an archived snapshot can silently be a block
    # page rather than the article — detect it and fall through to an error.
    "_incapsula_resource",
    "incapsula incident",
    "request unsuccessful",
    # DataDome block pages (e.g. nytimes.com, ft.com, wsj.com).
    "datadome",
    "enable js and disable any ad blocker",
]


def is_challenge_page(html_text):
    if not html_text:
        return False
    lower = html_text[:8000].lower()
    hits = sum(1 for sig in CHALLENGE_SIGNATURES if sig in lower)
    if hits >= 1 and len(html_text) < 20000:
        return True
    return hits >= 2


def set_step(job_id, step):
    if job_id and job_id in jobs:
        jobs[job_id]['step'] = step


REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ARCHIVE_PH_MIRRORS = ["archive.ph", "archive.today", "archive.is", "archive.li"]

FREEDIUM_MIRRORS = ["freedium-mirror.cfd", "freedium.cfd"]

# The original Freedium layout wrapped the post in <div id="main-content">, but
# the current mirror renders it inside a <main> element instead. Its error pages
# (e.g. "Article not found") still ship an empty <article> skeleton, so <article>
# on its own is not proof that the post was rendered.
FREEDIUM_CONTENT_MARKERS = ("main-content", "<main")

# SvelteKit streams the failure into the page body instead of using a non-200
# status, so the machine-readable error code is what marks a miss.
FREEDIUM_ERROR_MARKERS = ("ARTICLE_NOT_FOUND",)


def freedium_has_article(html_text):
    """True when a Freedium response actually contains the rendered post."""
    if not html_text or len(html_text) < 2000:
        return False
    if any(marker in html_text for marker in FREEDIUM_ERROR_MARKERS):
        return False
    return any(marker in html_text for marker in FREEDIUM_CONTENT_MARKERS)


def is_medium_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == "medium.com" or host.endswith(".medium.com")
    except Exception:
        return False


def fetch_via_freedium(url, job_id=None):
    """Freedium is a Medium-specific paywall bypass service."""
    set_step(job_id, 'fallback_freedium')
    clean_url = url.split('?')[0]
    for mirror in FREEDIUM_MIRRORS:
        try:
            freedium_url = f"https://{mirror}/{clean_url}"
            resp = http_get(
                freedium_url,
                headers=REAL_BROWSER_HEADERS,
                timeout=30,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            resp.encoding = resp.apparent_encoding
            text = resp.text
            if is_challenge_page(text):
                continue
            if not freedium_has_article(text):
                continue
            return text, resp.url
        except Exception:
            continue
    return None, None


def fetch_via_archive_org(url, job_id=None):
    set_step(job_id, 'fallback_org')
    wayback_api = "https://archive.org/wayback/available"
    try:
        meta = http_get(wayback_api, params={"url": url}, timeout=15).json()
        snapshot = meta.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None, None
        archived_url = snapshot["url"]
        if archived_url.startswith("http://"):
            archived_url = "https://" + archived_url[len("http://"):]
        if "/web/" in archived_url and "id_/" not in archived_url:
            archived_url = archived_url.replace(
                archived_url.split("/web/")[1].split("/")[0],
                archived_url.split("/web/")[1].split("/")[0] + "id_",
                1,
            )
        resp = http_get(archived_url, headers=googlebot_headers, timeout=30)
        resp.encoding = resp.apparent_encoding
        return resp.text, resp.url
    except Exception:
        return None, None


def fetch_via_archive_ph(url, job_id=None):
    """Try archive.today / archive.ph / archive.is — all mirrors of the same service."""
    set_step(job_id, 'fallback_ph')
    for mirror in ARCHIVE_PH_MIRRORS:
        try:
            newest_url = f"https://{mirror}/newest/{quote(url, safe=':/')}"
            resp = http_get(
                newest_url,
                headers=REAL_BROWSER_HEADERS,
                timeout=20,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue

            final = resp.url
            if not re.search(r"https?://archive\.(ph|today|is|li)/[A-Za-z0-9]{4,}", final):
                continue
            if re.search(r"archive\.(ph|today|is|li)/newest/", final):
                continue

            text = resp.text
            if is_challenge_page(text):
                continue

            lower_head = text[:5000].lower()
            if "no results" in lower_head or "0 captures" in lower_head:
                continue

            return text, final
        except Exception:
            continue
    return None, None


# --------------------------------------------------------------------------
# Additional sources
# --------------------------------------------------------------------------

class SourceMiss(Exception):
    """A source answered, but not with anything usable (e.g. HTTP 403)."""


ERROR_STATUSES = (401, 402, 403, 404, 407, 410, 429, 451)


def decode_html(response):
    """Use the charset the server declared; otherwise detect it from the bytes."""
    content_type = (response.headers.get("content-type") or "").lower()
    if "charset=" not in content_type:
        response.encoding = response.apparent_encoding
    return response.text


def _direct_fetch(url, headers):
    response = http_get(url, headers=headers, timeout=30)
    if response.status_code in ERROR_STATUSES or response.status_code >= 500:
        raise SourceMiss(f"HTTP {response.status_code}")
    return decode_html(response), response.url


def fetch_via_googlebot(url, job_id=None):
    """13ft's original method: ask for the page the way Google's crawler would."""
    set_step(job_id, 'fetch')
    return _direct_fetch(url, googlebot_headers)


def fetch_via_clean(url, job_id=None):
    """An ordinary browser request with no cookies, arriving from a search.

    Metered paywalls count free articles in cookies; a request without any
    starts the count from zero. Some sites also open articles to visitors
    who arrive from a search engine.
    """
    set_step(job_id, 'fallback_clean')
    headers = dict(REAL_BROWSER_HEADERS)
    headers["Referer"] = "https://www.google.com/"
    return _direct_fetch(url, headers)


def fetch_via_headless(url, job_id=None):
    set_step(job_id, 'fallback_headless')
    return headless.fetch_rendered(url)


# name -> (function name in this module, label shown to the user, text for a miss)
SOURCES = {
    "freedium": ("fetch_via_freedium", "Freedium (Medium)", "no copy available"),
    "googlebot": ("fetch_via_googlebot", "Googlebot request", "no usable response"),
    "clean": ("fetch_via_clean", "Fresh browser request", "no usable response"),
    "wayback": ("fetch_via_archive_org", "Wayback Machine", "no usable snapshot"),
    "archive_today": ("fetch_via_archive_ph", "archive.today", "no usable snapshot, or blocked by a CAPTCHA"),
    "headless": ("fetch_via_headless", "Headless browser", "no usable response"),
}
SOURCE_LABELS = {name: spec[1] for name, spec in SOURCES.items()}
DEFAULT_SOURCES = "googlebot,clean,wayback,archive_today,headless"
OUTCOME_TEXT = {
    "complete": "full article",
    "partial": "incomplete article",
    "challenge": "bot-protection or CAPTCHA page",
    "no_article": "no article text found",
    "empty": "nothing usable",
    "error": "request failed",
    "blocked": "refused by the network guard",
}


def configured_sources(url):
    """Sources to try for *url*, in order (``SOURCES`` env var, comma-separated)."""
    names = [n.strip().lower() for n in os.environ.get("SOURCES", DEFAULT_SOURCES).split(",")]
    names = [n for n in dict.fromkeys(names) if n in SOURCES]
    if is_medium_url(url):
        if "freedium" not in names:
            names.insert(0, "freedium")
    else:
        names = [n for n in names if n != "freedium"]
    if "headless" in names and not headless.enabled():
        names.remove("headless")
    return names


def min_words():
    try:
        return max(1, int(os.environ.get("MIN_WORDS", extract.DEFAULT_MIN_WORDS)))
    except ValueError:
        return extract.DEFAULT_MIN_WORDS


def _attempt(name, outcome, detail=""):
    return {
        "source": name,
        "label": SOURCE_LABELS.get(name, name),
        "outcome": outcome,
        "outcome_text": OUTCOME_TEXT.get(outcome, outcome),
        "detail": detail,
    }


def _describe_request_error(error):
    if isinstance(error, requests.exceptions.Timeout):
        return "timed out"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "could not connect"
    if isinstance(error, requests.exceptions.TooManyRedirects):
        return "too many redirects"
    return type(error).__name__


def summarize_attempts(attempts):
    parts = []
    for a in attempts:
        text = f"{a['label']}: {a['outcome_text']}"
        if a["detail"]:
            text += f" ({a['detail']})"
        parts.append(text)
    return "Tried " + "; ".join(parts) + "." if parts else ""


def _int_env(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# How many articles may be fetched and extracted at the same time. Extraction
# is CPU- and memory-heavy for a small board; FETCH_CONCURRENCY=1 makes a
# second request wait ("queued") instead of competing with the first.
FETCH_SLOTS = threading.BoundedSemaphore(_int_env("FETCH_CONCURRENCY", 2, minimum=1))


def memory_available_mb(meminfo="/proc/meminfo"):
    """MemAvailable in MB, or None where /proc/meminfo does not exist (macOS, Windows)."""
    try:
        with open(meminfo, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def check_memory(strings):
    """Refuse to start a fetch when MIN_FREE_MB is set and memory is short."""
    needed = _int_env("MIN_FREE_MB", 0)
    if not needed:
        return
    available = memory_available_mb()
    if available is not None and available < needed:
        raise UserFacingError(strings["low_memory_error"].format(available=available, needed=needed))


def resolve_target(raw_url, strings):
    """User input -> fetchable URL without tracking parameters, or UserFacingError."""
    target = urltools.parse_target(raw_url)
    if not target:
        raise UserFacingError(strings["invalid_url"])
    target = urltools.strip_tracking(target)
    try:
        netguard.check_url(target)
    except netguard.BlockedTarget:
        raise UserFacingError(strings["blocked_target_error"]) from None
    return target


def fetch_article(url, strings, job_id=None, refresh=False):
    """Fetch, extract and archive the article at *url*; return the snapshot id.

    Sources are tried in order. The first one that yields a complete-looking
    article wins; otherwise the best partial copy is kept, with the reasons it
    looks incomplete shown on the reader page. An article already in the
    archive is returned as is unless *refresh* is set.
    """
    target = resolve_target(url, strings)
    key = urltools.archive_key(target)
    store = archive_store.get_store()
    if not refresh:
        existing = store.latest_for(key)
        if existing:
            return existing["id"]

    if not FETCH_SLOTS.acquire(blocking=False):
        set_step(job_id, 'queued')  # only shown when another fetch really is running
        FETCH_SLOTS.acquire()
    try:
        set_step(job_id, 'connect')
        check_memory(strings)
        return _fetch_and_save(target, key, strings, job_id, store)
    finally:
        FETCH_SLOTS.release()


def _fetch_and_save(target, key, strings, job_id, store):
    threshold = min_words()
    attempts = []
    best = None
    for name in configured_sources(target):
        function_name, _label, miss_text = SOURCES[name]
        try:
            html_text, source_url = globals()[function_name](target, job_id)
        except SourceMiss as miss:
            attempts.append(_attempt(name, "empty", str(miss)))
            continue
        except netguard.BlockedTarget as blocked:
            attempts.append(_attempt(name, "blocked", blocked.reason))
            continue
        except requests.exceptions.RequestException as error:
            attempts.append(_attempt(name, "error", _describe_request_error(error)))
            continue
        except Exception as error:  # e.g. headless browser crashed: try the next source
            app.logger.warning("Source %s failed for %s: %r", name, target, error)
            attempts.append(_attempt(name, "error", type(error).__name__))
            continue

        if not html_text:
            attempts.append(_attempt(name, "empty", miss_text))
            continue
        if is_challenge_page(html_text):
            attempts.append(_attempt(name, "challenge"))
            continue
        article = extract.extract_article(html_text, target, threshold)
        if article is None:
            attempts.append(_attempt(name, "no_article"))
            continue
        attempts.append(_attempt(name, "complete" if article.complete else "partial", f"{article.word_count} words"))
        if best is None or article.score > best[1].score:
            best = (name, article, html_text, source_url)
        if article.complete:
            break

    if best is None:
        raise UserFacingError(f"{strings['nothing_found_error']} {summarize_attempts(attempts)}".strip())

    set_step(job_id, 'process')
    name, article, html_text, source_url = best
    keep_raw = os.environ.get("STORE_RAW_HTML", "1").strip().lower() not in ("0", "false", "no", "off")
    set_step(job_id, 'cleanup')
    return store.save(
        url=target,
        url_key=key,
        source_url=source_url or target,
        method=name,
        title=article.title,
        byline=article.byline,
        published=article.published,
        site_name=article.site_name,
        content_source=article.content_source,
        content_html=article.content_html,
        content_text=article.content_text,
        word_count=article.word_count,
        complete=article.complete,
        warnings=article.warnings,
        attempts=attempts,
        raw_html=html_text if keep_raw else None,
    )


def fetch_worker(job_id, url, strings, refresh=False):
    try:
        snapshot_id = fetch_article(url, strings, job_id, refresh=refresh)
        with jobs_lock:
            jobs[job_id]['result'] = f"/read/{snapshot_id}"
            jobs[job_id]['step'] = 'done'
    except UserFacingError as error:
        with jobs_lock:
            jobs[job_id]['error'] = error.user_message
            jobs[job_id]['step'] = 'error'
    except Exception:
        app.logger.exception("Unexpected error while fetching %s", url)
        with jobs_lock:
            jobs[job_id]['error'] = strings["unexpected_error"]
            jobs[job_id]['step'] = 'error'


# --------------------------------------------------------------------------
# Request hygiene for a server that runs on your own machine
# --------------------------------------------------------------------------

def _allowed_hosts():
    return {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}


@app.before_request
def check_host_header():
    """Answer only to localhost, IP addresses and names listed in ALLOWED_HOSTS.

    Without this, a malicious web page could use DNS rebinding to point its
    own domain at 127.0.0.1 and read your library from its own origin.
    """
    hostname = (urlsplit("//" + (request.host or "")).hostname or "").lower()
    allowed = _allowed_hosts()
    if "*" in allowed or hostname in allowed or hostname == "localhost":
        return None
    try:
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass
    return Response(
        f"Host name '{hostname}' is not allowed. Add it to the ALLOWED_HOSTS "
        "environment variable (comma-separated), e.g. ALLOWED_HOSTS=raspberrypi.local",
        status=403,
        mimetype="text/plain",
    )


def is_cross_site_request():
    """True for form posts that another website made your browser send."""
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("Origin")
    if origin:
        return urlsplit(origin).netloc.lower() != (request.host or "").lower()
    return False  # non-browser clients (curl, scripts)


@app.template_filter("ago")
def format_age(value):
    """'5 min ago', '3 h ago', '2 d ago', then the date."""
    try:
        then = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)} d ago"
    return then.strftime("%Y-%m-%d")


@app.template_filter("when")
def format_timestamp(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logo.png")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            data = f.read()
        return Response(data, mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    return "", 404


MANIFEST = {
    "name": "13ft Reader",
    "short_name": "13ft",
    "description": "Paste a link, read the article, keep a copy.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#282c3a",
    "theme_color": "#282c3a",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.route("/manifest.webmanifest")
def web_manifest():
    """Lets phones add 13ft to the home screen with its own icon."""
    return Response(json.dumps(MANIFEST), mimetype="application/manifest+json")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/")
def main_page():
    url = request.args.get("url", "").strip()
    autostart = {"url": url, "refresh": request.args.get("refresh") == "1"} if url else None
    return render_main_page(autostart)


@app.route("/status")
def status_stream():
    raw_url = request.args.get("url", "")
    url = unquote(raw_url)
    refresh = request.args.get("refresh") == "1"

    if not url:
        return "Missing URL", 400

    strings = load_strings()
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'step': 'starting', 'result': None, 'error': None}

    thread = threading.Thread(target=fetch_worker, args=(job_id, url, strings, refresh))
    thread.daemon = True
    thread.start()

    def generate():
        last_step = None
        try:
            while True:
                with jobs_lock:
                    job = dict(jobs[job_id]) if job_id in jobs else None
                if not job:
                    break

                current = job['step']
                if current != last_step:
                    last_step = current
                    if current == 'done':
                        yield f"event: step\ndata: {json.dumps({'step': 'done', 'redirect': job['result']})}\n\n"
                        break
                    elif current == 'error':
                        yield f"event: error_msg\ndata: {json.dumps({'message': job['error']})}\n\n"
                        break
                    else:
                        yield f"event: step\ndata: {json.dumps({'step': current})}\n\n"

                time.sleep(0.2)
        finally:
            with jobs_lock:
                jobs.pop(job_id, None)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route("/article", methods=["POST"])
def show_article():
    """Form fallback for browsers without JavaScript: fetch, then redirect."""
    link = flask.request.form["link"]
    strings = load_strings()
    try:
        snapshot_id = fetch_article(link, strings)
    except UserFacingError as error:
        return error.user_message, 400
    except Exception:
        app.logger.exception("Unexpected error while fetching %s", link)
        return strings["unexpected_error"], 500
    return flask.redirect(f"/read/{snapshot_id}", code=303)


def _snapshot_or_404(snapshot_id):
    snapshot = archive_store.get_store().get(snapshot_id)
    if snapshot is None:
        flask.abort(404)
    return snapshot


@app.route("/read/<int:snapshot_id>")
def read_snapshot(snapshot_id):
    snapshot = _snapshot_or_404(snapshot_id)
    body = flask.render_template_string(
        pages.READER_TEMPLATE,
        snap=snapshot,
        versions=archive_store.get_store().versions(snapshot["url_key"]),
        method_label=SOURCE_LABELS.get(snapshot["method"], snapshot["method"]),
        labels=SOURCE_LABELS,
        reading_minutes=max(1, math.ceil(snapshot["word_count"] / 230)),
    )
    return Response(body, mimetype="text/html", headers={
        "Content-Security-Policy": pages.READER_CSP,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


@app.route("/read/<int:snapshot_id>/original")
def read_snapshot_original(snapshot_id):
    """The saved page in its original layout, isolated from this server.

    The ``sandbox`` CSP gives the page an opaque origin, so publisher scripts
    (if enabled with ?js=1) cannot read your library or anything else on this
    server. Scripts are off by default, which also keeps paywall overlays off.
    """
    snapshot = _snapshot_or_404(snapshot_id)
    raw = archive_store.get_store().raw_html(snapshot_id)
    if raw is None:
        flask.abort(404)
    # Wayback "id_" snapshots are the publisher's own HTML, so relative links
    # belong to the publisher; other sources are resolved against where they came from.
    base = snapshot["url"] if snapshot["method"] == "wayback" else (snapshot["source_url"] or snapshot["url"])
    sandbox = "sandbox allow-popups allow-popups-to-escape-sandbox"
    if request.args.get("js") == "1":
        sandbox += " allow-scripts"
    return Response(process_html_document(raw, base), mimetype="text/html", headers={
        "Content-Security-Policy": sandbox,
        "Referrer-Policy": "no-referrer",
    })


LIBRARY_PAGE_SIZE = 50


@app.route("/library")
def library():
    query = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    items, total = archive_store.get_store().search(query, LIBRARY_PAGE_SIZE, (page - 1) * LIBRARY_PAGE_SIZE)
    body = flask.render_template_string(
        pages.LIBRARY_TEMPLATE,
        items=items,
        total=total,
        q=query,
        page=page,
        pages=max(1, math.ceil(total / LIBRARY_PAGE_SIZE)),
        labels=SOURCE_LABELS,
    )
    return Response(body, mimetype="text/html", headers={
        "Content-Security-Policy": pages.LIBRARY_CSP,
        "Referrer-Policy": "no-referrer",
    })


@app.route("/library/<int:snapshot_id>/delete", methods=["POST"])
def delete_snapshot(snapshot_id):
    if is_cross_site_request():
        return "Cross-site request refused", 403
    if not archive_store.get_store().delete(snapshot_id):
        flask.abort(404)
    return flask.redirect("/library", code=303)


def _raw_request_target():
    """The path and query exactly as the browser sent them."""
    raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
    if not raw:
        raw = request.full_path.rstrip("?")
    return raw[1:] if raw.startswith("/") else raw


@app.route("/<path:path>", methods=["GET"])
def get_article(path):
    """``http://127.0.0.1:5000/https://example.com/story`` style links.

    An article already in the library opens straight away; otherwise the
    progress page starts fetching it.
    """
    strings = load_strings()
    target = urltools.parse_target(_raw_request_target())
    if not target:
        return strings["invalid_url"], 400
    target = urltools.strip_tracking(target)
    existing = archive_store.get_store().latest_for(urltools.archive_key(target))
    if existing:
        return flask.redirect(f"/read/{existing['id']}", code=302)
    return render_main_page({"url": target, "refresh": False})


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or 5000)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f" * Listening on {host}: other devices on your network can reach this server.")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
