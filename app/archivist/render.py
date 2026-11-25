"""Convert extracted article content into safe HTML.

Nothing from the publisher's markup is passed through. Trafilatura's XML
output is walked element by element and re-emitted from a short allowlist;
every text node and attribute is escaped, and only http(s) links and images
survive. The reader page is also served with a script-free Content Security
Policy, so this is defence in depth, not the only line.
"""
from __future__ import annotations

import re
from html import escape

_HEADINGS = {"h1": "h2", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4"}
_HIGHLIGHT = {"#b": "strong", "#i": "em", "#u": "u", "#t": "code", "#sub": "sub", "#sup": "sup"}
_BLOCK_TAGS = {"p", "head", "list", "quote", "code", "table", "graphic", "div"}


def safe_url(value):
    value = (value or "").strip()
    if value.lower().startswith(("http://", "https://")) and not any(c in value for c in "\r\n\t"):
        return value
    return None


def _attr(value):
    return escape(value, quote=True)


def _image(el):
    src = safe_url(el.get("src"))
    if not src:
        return ""
    alt = el.get("alt") or ""
    caption = el.get("title") or ""
    img = f'<img src="{_attr(src)}" alt="{_attr(alt)}" loading="lazy" referrerpolicy="no-referrer">'
    if caption:
        return f"<figure>{img}<figcaption>{escape(caption)}</figcaption></figure>"
    return f"<figure>{img}</figure>"


def _inline_element(el):
    tag = el.tag
    if tag == "hi":
        inner = _mixed(el)
        out = _HIGHLIGHT.get(el.get("rend", ""))
        return f"<{out}>{inner}</{out}>" if out else inner
    if tag == "ref":
        inner = _mixed(el)
        href = safe_url(el.get("target"))
        if href:
            return f'<a href="{_attr(href)}" rel="noopener noreferrer nofollow">{inner}</a>'
        return inner
    if tag == "lb":
        return "<br>"
    if tag == "graphic":
        return _image(el)
    if tag == "del":
        return f"<del>{_mixed(el)}</del>"
    if tag in _BLOCK_TAGS or tag in ("item", "row", "cell"):
        return block(el)
    return _mixed(el)  # unknown element: keep its text only


def _mixed(el):
    """Text of *el* plus its children, with children rendered recursively."""
    parts = [escape(el.text or "")]
    for child in el:
        if isinstance(child.tag, str):  # skip comments / processing instructions
            parts.append(_inline_element(child))
        parts.append(escape(child.tail or ""))
    return "".join(parts)


def _has_content(el):
    return bool("".join(el.itertext()).strip()) or el.find(".//graphic") is not None


def block(el):
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if tag == "head":
        out = _HEADINGS.get(el.get("rend", "h2"), "h3")
        return f"<{out}>{_mixed(el)}</{out}>" if _has_content(el) else ""
    if tag == "p":
        return f"<p>{_mixed(el)}</p>" if _has_content(el) else ""
    if tag == "list":
        out = "ol" if el.get("rend") == "ol" else "ul"
        items = "".join(f"<li>{_mixed(item)}</li>" for item in el if item.tag == "item")
        return f"<{out}>{items}</{out}>" if items else ""
    if tag == "item":
        return f"<li>{_mixed(el)}</li>"
    if tag == "quote":
        return f"<blockquote>{_mixed(el)}</blockquote>" if _has_content(el) else ""
    if tag == "code":
        return f"<pre><code>{escape(''.join(el.itertext()))}</code></pre>"
    if tag == "graphic":
        return _image(el)
    if tag == "table":
        rows = []
        for row in el.iter("row"):
            cells = []
            for cell in row:
                if cell.tag != "cell":
                    continue
                out = "th" if cell.get("role") == "head" else "td"
                cells.append(f"<{out}>{_mixed(cell)}</{out}>")
            if cells:
                rows.append("<tr>" + "".join(cells) + "</tr>")
        return f'<div class="table-wrap"><table>{"".join(rows)}</table></div>' if rows else ""
    if tag == "lb":
        return ""
    return f"<p>{_mixed(el)}</p>" if _has_content(el) else ""


def blocks_to_html(elements):
    return "\n".join(html for html in (block(el) for el in elements) if html)


def block_text(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def text_to_html(text):
    """Plain text (e.g. JSON-LD articleBody) to paragraphs."""
    text = text.replace("\r\n", "\n").strip()
    chunks = re.split(r"\n\s*\n", text) if "\n\n" in text else text.split("\n")
    return "\n".join(f"<p>{escape(chunk.strip())}</p>" for chunk in chunks if chunk.strip())
