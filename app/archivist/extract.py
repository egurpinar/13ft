"""Find the article in a fetched page and judge whether it looks complete.

Two sources of article text are used:

1. Trafilatura's main-content extraction over the HTML tree [Barbaresi 2021].
   It works on the markup, not the rendered page, so text that a paywall only
   *hides* with CSS or JavaScript is still found.
2. The ``articleBody`` field of the page's JSON-LD metadata. Many news sites
   put the full text there for search engines; Google's paywalled-content
   guidelines describe this markup (``isAccessibleForFree``, ``hasPart``).

The longer of the two wins. The result is then checked for signs that the
publisher sent only a teaser: a "subscribe to continue" prompt at the end,
or a ``wordCount`` in the metadata that is clearly larger than what we got.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import trafilatura
from lxml import etree

from . import render

logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("htmldate").setLevel(logging.ERROR)

DEFAULT_MIN_WORDS = 150

ARTICLE_TYPES = {
    "article", "newsarticle", "reportagenewsarticle", "analysisnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "backgroundnewsarticle",
    "askpublicnewsarticle", "blogposting", "liveblogposting", "techarticle",
    "scholarlyarticle", "report", "socialmediaposting",
}

# Prompts that publishers place where the free part of an article ends. Only
# short blocks at the *end* of the text are tested, so a newsletter ad in the
# middle of a complete article does not trigger a false alarm.
PAYWALL_PROMPTS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bsubscribe (now )?(to|and) (continue|keep) reading\b",
    r"\bcontinue reading\b.{0,80}\b(subscri|sign in|log in|register|account|member)",
    r"\b(subscri|sign in|log in|register|account|member).{0,80}\bcontinue reading\b",
    r"\balready (a )?(subscriber|member)\b",
    r"\b(this|the) (article|story|content|piece) is (only )?(available|reserved|exclusive)\b.{0,60}\b(subscriber|member)",
    r"\bfor (paid |premium |digital )?(subscribers|members) only\b",
    r"\b(sign|log) in to (continue|read|keep reading|access)\b",
    r"\bregister (now |for free |free )?to (continue|read|keep reading)\b",
    r"\byou('ve| have) (reached|hit|used( up)?|read) (your|all( of)? your|\d+ of( your)? \d+) (free |monthly |complimentary )?(article|stories|story|articles)",
    r"\bunlock (this|the full|full|unlimited) (article|story|access)\b",
    r"\bcreate a free account to (continue|read|keep reading)\b",
    r"\bsubscribe to (read|unlock|access) (the full|this|more|the rest)\b",
    r"\bto read the (full|rest of the|complete|whole) (article|story)\b",
    r"\bsubscribe for (unlimited|full) (digital )?access\b",
    r"\bthis (article|story|content) is for (paid )?subscribers\b",
)]


@dataclass
class Article:
    title: str = ""
    byline: str = ""
    published: str = ""
    site_name: str = ""
    content_html: str = ""
    content_text: str = ""
    word_count: int = 0
    content_source: str = ""          # "trafilatura" or "json-ld"
    expected_words: int | None = None  # JSON-LD wordCount, when present
    accessible_for_free: bool | None = None
    warnings: list = field(default_factory=list)  # [{"code": ..., "message": ...}]

    def warn(self, code, message):
        self.warnings.append({"code": code, "message": message})

    @property
    def complete(self):
        return not self.warnings

    @property
    def score(self):
        """Used to pick the best copy when no source gave a complete one."""
        penalty = 0.5 if any(w["code"] in ("prompt", "expected") for w in self.warnings) else 1.0
        return self.word_count * penalty


def count_words(text):
    return len(re.findall(r"\w+", text or ""))


def looks_like_paywall_prompt(text):
    return len(text) <= 400 and any(p.search(text) for p in PAYWALL_PROMPTS)


# ---------------------------------------------------------------- JSON-LD ---

_LD_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _ld_objects(html_text):
    for match in _LD_RE.finditer(html_text):
        raw = match.group(1).strip()
        raw = re.sub(r"^\s*(<!--|<!\[CDATA\[)|(-->|\]\]>)\s*$", "", raw)
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.append(item["@graph"])
                yield item


def _types(obj):
    value = obj.get("@type", [])
    values = value if isinstance(value, list) else [value]
    return {str(v).lower() for v in values}


def _names(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        name = value.get("name")
        return [name] if isinstance(name, str) else []
    if isinstance(value, list):
        return [n for v in value for n in _names(v)]
    return []


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _to_int(value):
    try:
        number = int(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _strip_tags(text):
    if "<" not in text:
        return text
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>", "\n\n", text)
    return re.sub(r"<[^>]+>", "", text)


@dataclass
class LinkedData:
    headline: str = ""
    authors: list = field(default_factory=list)
    published: str = ""
    publisher: str = ""
    body: str = ""
    word_count: int | None = None
    accessible_for_free: bool | None = None


def read_linked_data(html_text):
    """Return the article object from the page's JSON-LD, preferring the longest body."""
    best = None
    for obj in _ld_objects(html_text):
        if not (_types(obj) & ARTICLE_TYPES):
            continue
        body = obj.get("articleBody")
        body = _strip_tags(body).strip() if isinstance(body, str) else ""
        candidate = LinkedData(
            headline=obj.get("headline") if isinstance(obj.get("headline"), str) else "",
            authors=_names(obj.get("author")),
            published=obj.get("datePublished") if isinstance(obj.get("datePublished"), str) else "",
            publisher=(_names(obj.get("publisher")) or [""])[0],
            body=body,
            word_count=_to_int(obj.get("wordCount")),
            accessible_for_free=_to_bool(obj.get("isAccessibleForFree")),
        )
        if best is None or len(candidate.body) > len(best.body):
            best = candidate
    return best


# ------------------------------------------------------------ Trafilatura ---

def _trafilatura_xml(html_text, url, favor_recall):
    xml = trafilatura.extract(
        html_text,
        url=url,
        output_format="xml",
        include_formatting=True,
        include_links=True,
        include_images=True,
        include_tables=True,
        include_comments=False,
        with_metadata=True,
        favor_recall=favor_recall,
        deduplicate=False,
    )
    if not xml:
        return None
    try:
        return etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None


def _main_blocks(doc):
    main = doc.find("main")
    return list(main) if main is not None else []


def _strip_trailing_prompts(blocks):
    """Drop paywall prompts from the end; return (kept_blocks, removed_any)."""
    removed = False
    while blocks:
        text = render.block_text(blocks[-1])
        if not text or looks_like_paywall_prompt(text):
            removed = removed or bool(text)
            blocks = blocks[:-1]
            continue
        break
    return blocks, removed


def _strip_trailing_prompt_paragraphs(text):
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    removed = False
    while paragraphs and looks_like_paywall_prompt(paragraphs[-1].strip()):
        paragraphs.pop()
        removed = True
    return "\n\n".join(p.strip() for p in paragraphs), removed


def _normalise_title(text):
    return re.sub(r"\W+", " ", text or "").strip().lower()


def _drop_title_heading(blocks, title):
    """The reader page prints the title itself; drop a leading heading that repeats it."""
    if blocks and title and blocks[0].tag == "head":
        heading = _normalise_title(render.block_text(blocks[0]))
        if heading and heading == _normalise_title(title):
            return blocks[1:]
    return blocks


def _iso_date(value):
    match = re.match(r"\d{4}-\d{2}-\d{2}", value or "")
    return match.group(0) if match else (value or "")


def extract_article(html_text, url=None, min_words=DEFAULT_MIN_WORDS):
    """Extract the article from *html_text*; None if there is no article-like text."""
    if not html_text:
        return None

    doc = _trafilatura_xml(html_text, url, favor_recall=False)
    blocks = _main_blocks(doc) if doc is not None else []
    tei_words = sum(count_words(render.block_text(b)) for b in blocks)
    if tei_words < min_words:
        recall_doc = _trafilatura_xml(html_text, url, favor_recall=True)
        recall_blocks = _main_blocks(recall_doc) if recall_doc is not None else []
        recall_words = sum(count_words(render.block_text(b)) for b in recall_blocks)
        if recall_words > tei_words:
            doc, blocks, tei_words = recall_doc, recall_blocks, recall_words

    blocks, tei_prompt = _strip_trailing_prompts(blocks)
    ld = read_linked_data(html_text)
    title = (ld.headline if ld and ld.headline else (doc.get("title", "") if doc is not None else "")).strip()
    blocks = _drop_title_heading(blocks, title)
    tei_text = "\n\n".join(t for t in (render.block_text(b) for b in blocks) if t)
    tei_words = count_words(tei_text)

    ld_text, ld_prompt = _strip_trailing_prompt_paragraphs(ld.body) if ld and ld.body else ("", False)
    ld_words = count_words(ld_text)
    ld_paragraphs = len([p for p in ld_text.split("\n\n") if p.strip()])

    # JSON-LD wins when it is clearly longer, or when it has the same text with
    # far better paragraphing (Trafilatura's own fallback sometimes returns the
    # articleBody as a single block).
    use_ld = ld_words > max(tei_words * 1.2, tei_words + 50) or (
        ld_words >= max(0.9 * tei_words, min_words) and ld_paragraphs > 2 * max(1, len(blocks))
    )

    article = Article()
    if use_ld:
        article.content_html = render.text_to_html(ld_text)
        article.content_text = ld_text
        article.content_source = "json-ld"
        prompt_seen = ld_prompt
    elif tei_words:
        article.content_html = render.blocks_to_html(blocks)
        article.content_text = tei_text
        article.content_source = "trafilatura"
        prompt_seen = tei_prompt
    else:
        return None
    article.word_count = count_words(article.content_text)

    meta = doc.attrib if doc is not None else {}
    article.title = title
    article.byline = ", ".join(ld.authors) if ld and ld.authors else meta.get("author", "")
    article.published = _iso_date(ld.published if ld and ld.published else meta.get("date", ""))
    article.site_name = (ld.publisher if ld and ld.publisher else meta.get("sitename", "")) or meta.get("hostname", "")
    if ld:
        article.expected_words = ld.word_count
        article.accessible_for_free = ld.accessible_for_free

    # A prompt after a long text is usually an overlay on top of a page that
    # still contains the whole article (a "soft" paywall); after a short text it
    # marks the end of a teaser.
    if prompt_seen and article.word_count < 2 * min_words:
        article.warn("prompt", "The page ends with a subscription prompt, so this is probably only the free part.")
    if article.expected_words and article.word_count < 0.85 * article.expected_words:
        article.warn(
            "expected",
            f"The publisher's metadata says the article has {article.expected_words} words; this copy has {article.word_count}.",
        )
    if article.word_count < min_words:
        article.warn("short", f"Only {article.word_count} words were found.")
    return article
