"""Shared fixtures for the offline test suite (no network, no DNS)."""
import json

URL = "https://example.com/news/article"
PUBLIC_IP = "93.184.215.14"


def paragraphs(count, start=0, topic="power converter"):
    return [
        f"Paragraph {i}: the {topic} discussion continues with a different detail "
        f"about topology number {i}, its switching losses and the thermal design margin."
        for i in range(start, start + count)
    ]


def page(body_paragraphs=(), extra_body="", ld=None, title="Test Story"):
    ld_html = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    body = "".join(f"<p>{p}</p>" for p in body_paragraphs)
    return (
        f"<html><head><title>{title}</title>{ld_html}</head><body>"
        f"<header><nav>Home | World | Sports</nav></header>"
        f"<article><h1>{title}</h1>{body}{extra_body}</article>"
        f"<footer>Copyright</footer></body></html>"
    )


FULL_ARTICLE = page(paragraphs(10))  # ~300 words: complete
TEASER = page(paragraphs(1), extra_body="<p>Already a subscriber? Sign in to continue reading.</p>")


class FakeResponse:
    def __init__(self, text="", status_code=200, url=URL, headers=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = headers if headers is not None else {"content-type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in (301, 302, 303, 307, 308)

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass
