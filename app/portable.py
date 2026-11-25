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
from datetime import datetime
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
    "heading": "Enter Website Link",
    "label": "Link of the website you want to remove paywall for:",
    "submit": "Submit",
    "toggle_dark_mode": "Toggle Dark Mode",
    "status_heading": "Fetching Article",
    "error_title": "Something went wrong",
    "retry_button": "Try Another URL",
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

html = """
<!DOCTYPE html>
<html lang="{{ lang }}">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>13ft Ladder</title>
    <link rel="icon" href="/favicon.ico" type="image/png">
    <link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600&display=swap" rel="stylesheet" async>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Open Sans', sans-serif;
            background-color: #FFF;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 90vh;
            transition: background-color 0.3s, color 0.3s;
        }

        h1 {
            font-size: 1.5rem;
            margin-bottom: 20px;
            text-align: center;
            color: #333;
        }

        label {
            display: block;
            margin-bottom: 10px;
            font-weight: bold;
        }

        input[type=text] {
            padding: 10px;
            margin-bottom: 10px;
            border: 1px solid #ccc;
            border-radius: 5px;
            width: 100%;
            font-size: 1rem;
        }

        input[type="submit"] {
            padding: 16px;
            background-color: #a327f0;
            color: #fff;
            border: none;
            border-radius: 5px;
            width: 100%;
            text-transform: uppercase;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }

        input[type="submit"]:hover {
            background-color: #821fc0;
        }

        .dark-mode-toggle {
            position: absolute;
            top: 10px;
            right: 10px;
        }

        .library-link {
            position: absolute;
            top: 14px;
            left: 16px;
            color: #a327f0;
            font-weight: 600;
            text-decoration: none;
        }

        body.dark-mode .library-link { color: #b18cff; }

        .dark-mode-toggle input {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border: 0;
        }

        .dark-mode-toggle label {
            cursor: pointer;
            width: 54px;
            height: 28px;
            background: #cbd5e1;
            display: block;
            border-radius: 100px;
            position: relative;
            transition: background-color 0.3s, border-color 0.3s;
            border: 1px solid #b8c2cc;
        }

        .dark-mode-toggle label:hover {
            background: #b8c2cc;
            border-color: #a1b0be;
        }

        .dark-mode-toggle input:focus-visible + label {
            outline: 2px solid #a327f0;
            outline-offset: 3px;
        }

        body.dark-mode .dark-mode-toggle input:focus-visible + label {
            outline-color: #7359f8;
        }

        .dark-mode-toggle .thumb {
            position: absolute;
            top: 2px;
            left: 2px;
            width: 22px;
            height: 22px;
            background: #fff;
            border-radius: 50%;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), background-color 0.3s, box-shadow 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
        }

        .dark-mode-toggle input:checked+label {
            background: #312e81;
            border-color: #4338ca;
        }

        .dark-mode-toggle input:checked+label:hover {
            background: #2e2a78;
            border-color: #3b379d;
        }

        .dark-mode-toggle input:checked+label .thumb {
            transform: translateX(26px);
            background: #1e1b4b;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4), 0 0 8px rgba(99, 102, 241, 0.4);
        }

        .dark-mode-toggle .icon {
            position: absolute;
            width: 14px;
            height: 14px;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.3s ease;
        }

        .dark-mode-toggle .icon.sun {
            color: #ea580c;
            opacity: 1;
            transform: scale(1) rotate(0deg);
        }

        .dark-mode-toggle .icon.moon {
            color: #4f46e5;
            opacity: 0;
            transform: scale(0) rotate(-90deg);
        }

        .dark-mode-toggle input:checked+label .icon.sun {
            opacity: 0;
            transform: scale(0) rotate(90deg);
        }

        .dark-mode-toggle input:checked+label .icon.moon {
            color: #c7d2fe;
            opacity: 1;
            transform: scale(1) rotate(0deg);
        }

        @media only screen and (max-width: 600px) {
            form, .status-container { padding: 10px; }
            h1 { font-size: 1.2rem; }
        }

        body.dark-mode { background-color: #191a21; color: #FFF; }
        body.dark-mode h1 { color: #FFF; }
        body.dark-mode input[type=text] { background-color: #555; border: 1px solid #777; color: #FFF; }
        body.dark-mode input[type="submit"] { background-color: #7359f8; }
        body.dark-mode input[type="submit"]:hover { background-color: #5c47c6; }
        body.dark-mode .status-container { background: #23243a; }
        body.dark-mode .step { color: #aaa; }
        body.dark-mode .step.active { color: #fff; }
        body.dark-mode .step.done { color: #7359f8; }
        body.dark-mode .error-box { background: #3a1c1c; border-color: #ff6b6b; color: #ffaaaa; }

        /* Status page styles */
        .status-container {
            max-width: 480px;
            width: 100%;
            padding: 30px;
            text-align: center;
        }

        .status-container h1 { margin-bottom: 30px; }

        .url-display {
            font-size: 0.85rem;
            color: #888;
            margin-bottom: 24px;
            word-break: break-all;
        }

        .progress-bar-track {
            width: 100%;
            height: 6px;
            background: #e0e0e0;
            border-radius: 3px;
            overflow: hidden;
            margin-bottom: 28px;
        }

        body.dark-mode .progress-bar-track { background: #444; }

        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #a327f0, #7359f8);
            border-radius: 3px;
            transition: width 0.5s ease;
        }

        .steps { text-align: left; margin-bottom: 24px; }

        .step {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 8px 0;
            color: #bbb;
            font-size: 0.95rem;
            transition: color 0.3s;
        }

        .step.active { color: #333; font-weight: 600; }
        .step.done { color: #a327f0; }

        .step-icon {
            width: 24px;
            height: 24px;
            flex-shrink: 0;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .spinner {
            width: 18px;
            height: 18px;
            border: 2px solid #e0e0e0;
            border-top-color: #a327f0;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        body.dark-mode .spinner { border-color: #444; border-top-color: #7359f8; }

        @keyframes spin { to { transform: rotate(360deg); } }

        .checkmark { color: #a327f0; font-weight: bold; font-size: 1.1rem; }

        .elapsed {
            font-size: 0.85rem;
            color: #999;
            margin-top: 4px;
        }

        .error-box {
            background: #fff0f0;
            border: 1px solid #ffcccc;
            border-radius: 8px;
            padding: 16px;
            margin-top: 16px;
            color: #cc3333;
            font-size: 0.9rem;
            text-align: left;
        }

        .error-box strong { display: block; margin-bottom: 6px; }

        .retry-btn {
            display: inline-block;
            margin-top: 16px;
            padding: 10px 24px;
            background: #a327f0;
            color: #fff;
            border: none;
            border-radius: 5px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
        }

        .retry-btn:hover { background: #821fc0; }
    </style>
</head>

<body>
    <a class="library-link" href="/library">{{ library_link }}</a>
    <div class="dark-mode-toggle">
        <input type="checkbox" id="dark-mode-toggle">
        <label for="dark-mode-toggle" title="{{ toggle_dark_mode }}">
            <span class="thumb">
                <svg class="icon sun" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="5"></circle>
                    <line x1="12" y1="1" x2="12" y2="3"></line>
                    <line x1="12" y1="21" x2="12" y2="23"></line>
                    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
                    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
                    <line x1="1" y1="12" x2="3" y2="12"></line>
                    <line x1="21" y1="12" x2="23" y2="12"></line>
                    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
                    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
                </svg>
                <svg class="icon moon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                </svg>
            </span>
        </label>
    </div>

    <div id="form-view">
        <form id="url-form">
            <h1>{{ heading }}</h1>
            <label for="link">{{ label }}</label>
            <input type="text" id="link" name="link" required autofocus>
            <input type="submit" value="{{ submit }}">
        </form>
    </div>

    <div id="status-view" style="display:none;">
        <div class="status-container">
            <h1>{{ status_heading }}</h1>
            <div class="url-display" id="status-url"></div>
            <div class="progress-bar-track">
                <div class="progress-bar-fill" id="progress-fill"></div>
            </div>
            <div class="steps" id="steps-list"></div>
            <div class="elapsed" id="elapsed-time"></div>
            <div id="error-area"></div>
        </div>
    </div>

    <script>
        const toggleSwitch = document.getElementById('dark-mode-toggle');
        const currentTheme = localStorage.getItem('theme') || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

        if (currentTheme === "dark") {
            document.body.classList.add("dark-mode");
            toggleSwitch.checked = true;
        }

        toggleSwitch.addEventListener('change', function () {
            if (this.checked) {
                document.body.classList.add("dark-mode");
                localStorage.setItem('theme', 'dark');
            } else {
                document.body.classList.remove("dark-mode");
                localStorage.setItem('theme', 'light');
            }
        });

        const UI_STRINGS = {{ ui_strings | tojson }};
        const STEPS = {{ steps | tojson }};

        function renderSteps(activeIdx) {
            const container = document.getElementById('steps-list');
            container.innerHTML = '';
            STEPS.forEach((step, i) => {
                const div = document.createElement('div');
                div.className = 'step' + (i < activeIdx ? ' done' : i === activeIdx ? ' active' : '');
                const icon = document.createElement('div');
                icon.className = 'step-icon';
                if (i < activeIdx) {
                    icon.innerHTML = '<span class="checkmark">&#10003;</span>';
                } else if (i === activeIdx) {
                    icon.innerHTML = '<div class="spinner"></div>';
                } else {
                    icon.innerHTML = '<span style="color:#ddd;">&#9675;</span>';
                }
                const text = document.createElement('span');
                text.textContent = step.label;
                div.appendChild(icon);
                div.appendChild(text);
                container.appendChild(div);
            });
        }

        function showStatus(url) {
            document.getElementById('form-view').style.display = 'none';
            document.getElementById('status-view').style.display = 'flex';
            document.getElementById('status-url').textContent = url;
            document.getElementById('error-area').innerHTML = '';
            renderSteps(0);
            document.getElementById('progress-fill').style.width = '0%';
        }

        function showError(message) {
            const area = document.getElementById('error-area');
            area.innerHTML = '';
            const box = document.createElement('div');
            box.className = 'error-box';
            const strong = document.createElement('strong');
            strong.textContent = UI_STRINGS.error_title;
            box.appendChild(strong);
            box.appendChild(document.createTextNode(message));
            area.appendChild(box);
            const retry = document.createElement('a');
            retry.href = '/';
            retry.className = 'retry-btn';
            retry.textContent = UI_STRINGS.retry_button;
            area.appendChild(retry);
        }

        const AUTOSTART = {{ autostart | tojson }};

        document.getElementById('url-form').addEventListener('submit', function(e) {
            e.preventDefault();
            const link = document.getElementById('link').value.trim();
            if (!link) return;
            startFetch(link, false);
        });

        if (AUTOSTART && AUTOSTART.url) {
            document.getElementById('link').value = AUTOSTART.url;
            startFetch(AUTOSTART.url, AUTOSTART.refresh);
        }

        function startFetch(link, refresh) {
            showStatus(link);

            const startTime = Date.now();
            const elapsedEl = document.getElementById('elapsed-time');
            const progressEl = document.getElementById('progress-fill');

            const timer = setInterval(() => {
                const sec = ((Date.now() - startTime) / 1000).toFixed(0);
                elapsedEl.textContent = UI_STRINGS.elapsed_template.replace('{seconds}', sec);
            }, 500);

            const evtSource = new EventSource('/status?url=' + encodeURIComponent(link) + (refresh ? '&refresh=1' : ''));

            evtSource.addEventListener('step', function(e) {
                const data = JSON.parse(e.data);
                const idx = STEPS.findIndex(s => s.id === data.step);
                if (idx >= 0) {
                    renderSteps(idx);
                    const pct = Math.min(((idx + 1) / STEPS.length) * 100, 95);
                    progressEl.style.width = pct + '%';
                }
                if (data.redirect) {
                    evtSource.close();
                    clearInterval(timer);
                    renderSteps(STEPS.length);
                    progressEl.style.width = '100%';
                    setTimeout(() => {
                        window.location.href = data.redirect;
                    }, 400);
                }
            });

            evtSource.addEventListener('error_msg', function(e) {
                evtSource.close();
                clearInterval(timer);
                const data = JSON.parse(e.data);
                showError(data.message);
            });

            evtSource.onerror = function() {
                evtSource.close();
                clearInterval(timer);
                showError(UI_STRINGS.connection_lost_error);
            };
        }

        window.addEventListener('pageshow', function(event) {
            if (event.persisted) {
                document.getElementById('form-view').style.display = '';
                document.getElementById('status-view').style.display = 'none';
                document.getElementById('link').value = '';
            }
        });
    </script>
</body>

</html>
"""


def build_steps(strings):
    # Ordered the way fetch_article() tries sources, so the progress bar only
    # moves forward. Each source is tried only if the previous ones did not
    # return a complete article.
    return [
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


def render_main_page(autostart=None):
    strings = load_strings()
    ui_strings = {
        "error_title": strings["error_title"],
        "retry_button": strings["retry_button"],
        "elapsed_template": strings["elapsed_template"],
        "connection_lost_error": strings["connection_lost_error"],
    }
    return flask.render_template_string(
        html,
        steps=build_steps(strings),
        ui_strings=ui_strings,
        autostart=autostart,
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
    set_step(job_id, 'connect')
    target = resolve_target(url, strings)
    key = urltools.archive_key(target)
    store = archive_store.get_store()
    if not refresh:
        existing = store.latest_for(key)
        if existing:
            return existing["id"]

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
        jobs[job_id] = {'step': 'queued', 'result': None, 'error': None}

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
