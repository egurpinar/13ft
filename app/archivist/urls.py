"""URL helpers: turning user input into a fetchable URL, and archive keys."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from. They are removed
# before fetching (so the publisher does not learn which newsletter or share
# link you used) and before computing the archive key (so the same article
# reached from two links is stored under one key).
TRACKING_PREFIXES = ("utm_", "mc_", "st_", "pk_", "mtm_", "hsa_")
TRACKING_PARAMS = frozenset(
    {
        "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
        "igshid", "mkt_tok", "_hsenc", "_hsmi", "oly_anon_id", "oly_enc_id", "vero_id",
        "smid", "smtyp", "cmpid", "ocid", "sr_share", "ns_mchannel", "ns_source",
        "ns_campaign", "ns_linkname", "ns_fee", "ref_src", "ref_url",
    }
)
# Deliberately conservative: generic names such as "sid" or "ref" are kept,
# because some sites use them to identify the article itself.

_SCHEME_RE = re.compile(r"^(https?):/*(.*)$", re.IGNORECASE | re.DOTALL)
_HOSTLIKE_RE = re.compile(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/|\?|#|$)", re.UNICODE)


def is_tracking_param(name):
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def strip_tracking(url):
    """Remove known tracking query parameters and the fragment."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def parse_target(raw):
    """Turn user input ("example.com/a", "https:/example.com/a", ...) into a URL.

    Returns None when the input does not look like a web address. Some servers
    and proxies collapse "//" to "/" in paths, so "https:/host" is accepted.
    """
    if not raw:
        return None
    text = raw.strip().lstrip("/")
    match = _SCHEME_RE.match(text)
    if match:
        text = f"{match.group(1).lower()}://{match.group(2)}"
    elif _HOSTLIKE_RE.match(text):
        text = "https://" + text
    else:
        return None
    if not urlsplit(text).hostname:
        return None
    return text


def archive_key(url):
    """Stable key for "the same article": lowercase host, no tracking, no fragment."""
    parts = urlsplit(strip_tracking(url))
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    # http and https copies of an article are the same article, hence "https".
    return urlunsplit(("https", netloc, path, query, ""))
