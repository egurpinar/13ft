# 13 Feet Ladder

<img src="https://raw.githubusercontent.com/wasi-master/13ft/refs/heads/main/logo.png" align="right"
     alt="13 Feet Ladder Logo by @mcmikemn" width="150">

![build](https://github.com/wasi-master/13ft/actions/workflows/build.yaml/badge.svg)
[![Docker Pulls](https://img.shields.io/docker/pulls/wasimaster/13ft)](https://hub.docker.com/r/wasimaster/13ft)
[![runs](https://img.shields.io/badge/dynamic/json?url=https://github.com/ipitio/backage/raw/refs/heads/index/wasi-master/13ft/13ft.json&query=$.downloads&logo=github&logoColor=959da5&label=ghcr%20pulls&labelColor=333a41)](https://github.com/wasi-master/13ft/pkgs/container/13ft)

A site similar to [12ft.io](https://12ft.io) but is self hosted and works with websites that 12ft.io doesn't work with.

## What is this?

This is a simple self hosted server that has a simple but powerful interface to block ads, paywalls, and other nonsense. Specially for sites like medium, new york times which have paid articles that you normally cannot read. Now I do want you to support the creators you benefit from but if you just wanna see one single article and move on with your day then this might be helpful

## How does it work?

It pretends to be GoogleBot (Google's web crawler) and gets the same content that google will get. Google gets the whole page so that the content of the article can be indexed properly and this takes advantage of that.

The server now displays a loading/status page while it fetches the target article and will automatically try multiple bypass methods (multi-source fallbacks) if the primary approach fails. This improves compatibility with sites that employ stronger paywalls or blocking behaviour. Feature courtesy of [@JRS1986](https://github.com/JRS1986)

## This fork: reader view, personal archive, hardening

This branch turns 13ft into a self-hosted replacement for archive.ph for one person.

- **Reader view.** The article is extracted with [Trafilatura](https://github.com/adbar/trafilatura), or taken from the page's JSON-LD `articleBody` when that is fuller, and shown on a clean page with no publisher scripts. This removes script-driven overlays and recovers text that a paywall only hides with CSS.
- **It checks whether the copy is complete.** It checks for a "subscribe to continue" prompt after a short text, and compares against the publisher's own `wordCount`. If the page has only a teaser, the web archives are asked next. If no source gives a full copy, the longest one is kept and the reader page says why it may be incomplete.
- **Personal archive.** Every article is saved to a local SQLite file, with full-text search, all saved copies of each article, and the saved page in its original layout. Opening a link you've read before is instant, and the copy survives if the publisher edits or removes the page.
- **Safer to run.**
  - It listens on `127.0.0.1` by default.
  - It refuses to fetch private or local network addresses, re-checked on every redirect.
  - It only answers to `localhost`, IP addresses and names listed in `ALLOWED_HOSTS`, which blocks DNS rebinding.
  - The reader and library pages forbid scripts through a Content Security Policy.
  - The saved original layout is served in a CSP sandbox, so a publisher's scripts can never read your library.
- **Tracking parameters removed.** `utm_*`, newsletter IDs (`st_*`, `mc_*`) and similar are stripped before the publisher is contacted.

### Sources, and how it stays fast

Sources run in rounds. The sources in a round run **at the same time**, and the first complete article ends the search, so you never wait for a slower source. The next round only starts if no source in the current one had the full text.

| Round | Name (`SOURCES`) | What it does |
| --- | --- | --- |
| 1 | `googlebot` | Requests the page with Google's crawler user agent (13ft's original method). |
| 1 | `clean` | An ordinary browser request with no cookies and a search-engine referrer. This resets "N free articles" meters kept in cookies. |
| 1 | `freedium` | Medium articles only, through Freedium (upstream behaviour). |
| 2 | `wayback` | The latest Internet Archive snapshot (raw `id_` copy). |
| 2 | `archive_today` | The latest archive.today snapshot, if one exists and no CAPTCHA is shown. |
| 3 | `headless` | Optional. Renders the page in headless Chromium with known paywall-vendor scripts (Piano/Tinypass, Poool, Pelcro, …) blocked. Off unless `HEADLESS=on`. |

Each request gets 5 s to connect and at most 20 s between bytes, and each round at most 25 s; whatever has arrived by then is used. An article that's already in your library opens instantly.

What to expect, honestly:
- **Metered paywalls and overlay paywalls** (the text is in the page, then hidden) usually work.
- **Server-side "hard" paywalls** send only a teaser and verify Googlebot by reverse DNS. These only work when a public archive already has a full snapshot.
- **Sites behind bot protection** (Cloudflare, DataDome, …) are not bypassed. This fork deliberately does not solve CAPTCHAs or challenges.

The "How this copy was obtained" section at the bottom of each reader page lists what each source returned.

### Run it

**On a Mac** (one command in Terminal; needs Python 3.9 or newer, which macOS offers to install if it's missing):

```sh
cd "$(mktemp -d)" && curl -fsSL https://github.com/egurpinar/13ft/archive/refs/heads/reader-archive.tar.gz | tar xz && bash 13ft-reader-archive/deploy/macos/install.sh
```

It installs into `~/Library/Application Support/13ft`, starts when you log in (a LaunchAgent), restarts itself if it ever crashes, and opens **http://localhost:8013** (port 8013 because macOS's AirPlay Receiver uses 5000). Only your Mac can reach it. When idle it uses no CPU and about 50 MB of memory. Run the same command again to update; `bash ~/Library/Application\ Support/13ft/install.sh --uninstall` removes it and keeps your library.

**Anywhere else** (Linux, Windows):

```sh
python -m pip install -r requirements.txt
cd app && python portable.py          # http://127.0.0.1:5000
```

Or with Docker (builds this checkout; the port is published on 127.0.0.1 only and the archive lives in a named volume):

```sh
docker compose up -d --build
```

Optional headless source (not in the Alpine image, because Playwright needs glibc):

```sh
python -m pip install -r requirements-headless.txt
python -m playwright install chromium
HEADLESS=on python app/portable.py
```

### Ways to open an article

- Paste the link into the box on the home page. Pasting is enough, there's no need to press Read. Text with a link in it, such as "Look at this https://…", works too.
- **As an app in the Dock.** In Safari: File → Add to Dock. In Chrome: ⋮ → Cast, save, and share → Install page as app. It gets the 13ft icon and opens in its own window.
- Put the server address in front of any link: `http://localhost:8013/https://example.com/story`.
- **Bookmarklet.** Create a bookmark with this URL, then click it on any article:
  `javascript:location.href='http://localhost:8013/'+location.href`
- **Address-bar keyword.** Add a site search in Chrome/Edge (Settings → Search engine → Manage → Site search) or a keyword bookmark in Firefox:
  - URL: `http://localhost:8013/?url=%s`
  - Shortcut: `rt`

  Then type `rt <link>` in the address bar.

These addresses use the Mac installer's port, 8013. If you started it by hand, use `http://127.0.0.1:5000` instead.

### Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `5000` (Mac installer: `8013`) | Listen address. Use `0.0.0.0` to serve other devices, and then set `ALLOWED_HOSTS`. |
| `ALLOWED_HOSTS` | *(empty)* | Extra host names the server answers to, e.g. `reader.local`. `localhost` and IP addresses always work. |
| `DATA_DIR` | `./data` (`/data` in Docker) | Where `archive.sqlite3` is kept. |
| `SOURCES` | `googlebot,clean,wayback,archive_today,headless` | Which sources to use; each one runs in its round (see the table above). |
| `MIN_WORDS` | `150` | Below this, a copy counts as "short" and the next source is tried. |
| `FETCH_CONCURRENCY` | `2` | Articles fetched at the same time; more requests wait their turn. |
| `HEADLESS` | `off` | `on` enables the headless-browser source (needs Playwright). |
| `CHROMIUM_PATH` | *(Playwright's)* | Use a specific Chromium binary for the headless source. |
| `BLOCK_SCRIPT_HOSTS` | *(empty)* | Extra script hosts to block in the headless source, comma-separated. |
| `STORE_RAW_HTML` | `1` | Keep the fetched page (compressed) for the "Saved layout" view. `0` keeps only the extracted text. |
| `ALLOW_PRIVATE_TARGETS` | `0` | `1` lets the server fetch private or local addresses (e.g. pages on your LAN). |

The `api/index.py` / `vercel.json` deployment from upstream still exists. It is not recommended with this fork: the archive would not persist there, and a public instance is what got 12ft.io taken down.

### A note on legality

Getting around a paywall usually breaks the publisher's terms of service. Depending on where you live, it can also raise copyright questions: US publishers have argued that paywall-bypass tools violate DMCA §1201. This fork is meant for private, single-user use on your own machine. It doesn't defeat logins, CAPTCHAs or bot protection, and it doesn't share content with anyone. Check your local law, and consider subscribing to the outlets you rely on.


## How do I use it?

### Using Docker

Requirements:

- docker
- Docker Compose (available as `docker compose`)

First, clone the repo to your machine then run the following commands:

```sh
git clone https://github.com/wasi-master/13ft.git
cd 13ft
docker compose up
```

The image is also available from [DockerHub](https://hub.docker.com/r/wasimaster/13ft "docker pull wasimaster/13ft") or [ghcr.io](https://github.com/wasi-master/13ft/pkgs/container/13ft "docker pull ghcr.io/wasi-master/13ft:0.2.3") so the command `docker pull wasimaster/13ft` also works.

### Standard Python script

First, make sure you have [python](https://python.org) installed on your machine. Next, clone the git repo. Then go to a terminal (`Command Prompt` on Windows, `Terminal` on Mac) and run the following command:

From the git cloned directory on your computer:

```sh
cd app/
python -m pip install -r requirements.txt
```

If that doesn't work retry but replace `python` with `py`, then try `python3`, then try `py3`

Then run `portable.py`, click [this link](https://realpython.com/run-python-scripts/) for a tutorial on how to run python scripts.

```sh
python portable.py
```

Or you can also run via gunicorn as follows:

```sh
python -m gunicorn 'portable:app'
```

Then open the link shown in the terminal in the browser and you'll be able to use this script

### Installation using venv and running under specific bind address / port

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
FLASK_APP=app/portable.py flask run --host=127.0.0.1 --port=9982
```

## Using as a Bookmarklet in Chrome:

You can create a bookmarklet that performs the URL transformation by writing a small JavaScript snippet. Below is the JavaScript code for your bookmarklet:

```javascript
javascript: (function () {window.location.href="https://localhost:5000/" + encodeURIComponent(window.location.href);})();
```

You can replace https://localhost:5000/ with your own 13ft instance if desired.

Steps:

1. Open Bookmarks Manager:

2. Click on the three dots (menu) in the top-right corner of Chrome.
   Go to Bookmarks > Bookmark manager, or simply press Ctrl+Shift+O on Windows/Linux or Cmd+Option+B on Mac.
   Create a New Bookmark:

3. In the Bookmark Manager, click the three-dot menu in the top-right corner of the window and select Add new bookmark.
   Enter Bookmark Details: - Name: Enter a name for your bookmarklet, such as "13ft-ize". This name will show as a bookmark title in the bookmarks bar - URL: Paste the JavaScript code provided above into the URL field.
4. Click Save.

Using the Bookmarklet:

Navigate to the page whose URL you want to use 13ft on.

Click on the bookmarklet you saved in your bookmarks bar. The browser will redirect you to the 13ft version of the URL using your service.

To show Bookmarks in Chrome, click the icon with three horizontal bars in the top right corner to open options. 2. In options, hover over "Bookmarks" to display a second menu where you can click the "Show bookmarks bar" text to toggle the bar on or off.

Instructions courtesy of [@barakplasma](https://github.com/barakplasma)

## Customizing listening host and port, Systemd / Reverse-proxy example

### Systemd Service

```
/lib/systemd/system/13ft.service
```

```
[Unit]
Description=13ft Flask Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
Restart=on-failure
RestartSec=10
User=www-data
Group=www-data
Environment=APP_PATH=/var/www/paywall-break
Environment=FLASK_APP=app/portable.py

ExecStart=/bin/bash -c "cd ${APP_PATH};${APP_PATH}/venv/bin/flask run --host=127.0.0.1 --port=22113"

# Make sure stderr/stdout is captured in the systemd journal.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Reverse Proxy

```
<VirtualHost *:22114>
    ErrorLog ${APACHE_LOG_DIR}/13ft-error.log
    CustomLog ${APACHE_LOG_DIR}/13ft-access.log combined

    ProxyRequests Off

    SSLEngine on
    SSLCertificateFile      /etc/ssl/certs/ssl-cert-snakeoil.pem
    SSLCertificateKeyFile /etc/ssl/private/ssl-cert-snakeoil.key
    Header always set Strict-Transport-Security "max-age=63072000"
    SSLProtocol             all -SSLv3 -TLSv1 -TLSv1.1

    SSLHonorCipherOrder     off
    SSLSessionTickets       off

    Protocols h2 http/1.1

    <Proxy *>
        Order deny,allow
        Allow from all
    </Proxy>


    ProxyPass / http://127.0.0.1:22113/
    ProxyPassReverse / http://127.0.0.1:22113/


</VirtualHost>
```

## Localisation

The UI strings can be localised by setting the `LOCALE` environment variable before starting the app. Locale files live in `app/locales/<locale>.json`.

Built-in locales:

- `en` (default)
- `de`
- `fr`
- `ko`

Examples:

```sh
# from app
LOCALE=de python portable.py

# or with gunicorn
LOCALE=fr python -m gunicorn 'portable:app'
```

To add a new locale, copy `app/locales/en.json` to a new file such as `app/locales/es.json` and translate the values.

## Screenshots

### Step 1

![step 1 screenshot](screenshots/step-1.png)
Go to the website at the url shown in the console

### Step 2

![step 2 screenshot](screenshots/step-2.png)
Click on the input box

### Step 3

![step 3 screenshot](screenshots/step-3.png)
Paste your desired url

### Step 4

![step 4 screenshot](screenshots/step-4.gif)
Voilà you now have bypassed the paywall and ads

### Alternative method

You can also append the url at the end of the link and it will also work. (e.g if your server is running at `http://127.0.0.1:5000` then you can go to `http://127.0.0.1:5000/https://example.com` and it will read out the contents of `https://example.com`)

This feature was implemented by [@atcasanova](https://github.com/atcasanova)

### Logo attribution

Logo courtesy of [@mcmikemn](https://github.com/mcmikemn)

## Automated tests

From the repository root, install the application dependencies and run:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The suite uses Python's standard-library `unittest` and Flask's test client.
External HTTP calls are mocked; the tests do not contact article sites or archive services.
Coverage includes routes, error responses, localization, HTML processing, cache expiry,
archive fallbacks, and server-sent progress events. This does not validate live third-party availability.

The GitHub Actions build workflow (`.github/workflows/build.yaml`) runs the suite with pytest on every push
and pull request, before the Docker image is built. A test failure stops the workflow before anything is pushed.
