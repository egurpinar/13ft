"""Jinja templates for the reader and library pages.

Both pages work without JavaScript and are served with a Content Security
Policy that forbids scripts, so the article text can never execute code.
Light/dark follows the operating system setting via ``prefers-color-scheme``.
"""

READER_CSP = (
    "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
LIBRARY_CSP = READER_CSP


BASE_CSS = """
:root {
  --bg: #fbfaf7; --surface: #ffffff; --text: #1f1d1a; --muted: #6b665e; --line: #e4e0d8;
  --accent: #7a3fd1; --accent-text: #ffffff; --warn-bg: #fff6e0; --warn-line: #e7c36a; --warn-text: #5c4400;
  --code-bg: #f1eee8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17161a; --surface: #1f1e23; --text: #e9e6e1; --muted: #a39e96; --line: #34323a;
    --accent: #b18cff; --accent-text: #17161a; --warn-bg: #2d2614; --warn-line: #8a6d24; --warn-text: #f3dd9f;
    --code-bg: #26252b;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--accent); }
.bar { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--surface); font-size: 0.9rem; }
.bar .brand { font-weight: 700; color: var(--text); text-decoration: none; margin-right: auto; }
.bar a { text-decoration: none; }
.wrap { max-width: 44rem; margin: 0 auto; padding: 24px 16px 64px; }
.muted { color: var(--muted); }
.notice { background: var(--warn-bg); border: 1px solid var(--warn-line); color: var(--warn-text);
  border-radius: 8px; padding: 12px 14px; margin: 18px 0; font-size: 0.95rem; }
.notice ul { margin: 6px 0 0; padding-left: 20px; }
.button { display: inline-block; background: var(--accent); color: var(--accent-text); border: 0;
  border-radius: 6px; padding: 7px 14px; font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }
.button.secondary { background: transparent; color: var(--accent); border: 1px solid var(--line); }
"""

READER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{{ snap.title or snap.url }}</title>
<link rel="icon" href="/favicon.ico" type="image/png">
<style>
""" + BASE_CSS + """
article { font-family: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
  font-size: 1.15rem; line-height: 1.65; }
.kicker { font: 600 0.8rem/1.2 system-ui, sans-serif; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 8px; }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 12px; text-wrap: balance; }
.meta { font: 0.9rem/1.4 system-ui, sans-serif; color: var(--muted); margin: 0 0 24px; }
.content h2 { font-size: 1.4rem; line-height: 1.3; margin: 1.8em 0 0.5em; }
.content h3, .content h4 { font-size: 1.15rem; margin: 1.5em 0 0.4em; }
.content p { margin: 0 0 1.1em; }
.content img { max-width: 100%; height: auto; border-radius: 4px; }
.content figure { margin: 1.4em 0; }
.content figcaption { font: 0.85rem/1.4 system-ui, sans-serif; color: var(--muted); margin-top: 6px; }
.content blockquote { margin: 1.2em 0; padding: 0 0 0 1em; border-left: 3px solid var(--line); color: var(--muted); }
.content pre { background: var(--code-bg); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 0.9rem; }
.content code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 0.9em; }
.content .table-wrap { overflow-x: auto; margin: 1.2em 0; }
.content table { border-collapse: collapse; font: 0.9rem/1.4 system-ui, sans-serif; }
.content th, .content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
.content a { overflow-wrap: anywhere; }
.provenance { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--line);
  font: 0.85rem/1.5 system-ui, sans-serif; color: var(--muted); overflow-wrap: anywhere; }
.provenance ol, .provenance ul { padding-left: 20px; margin: 6px 0; }
.outcome-complete { color: var(--text); font-weight: 600; }
@media (max-width: 600px) { article { font-size: 1.05rem; } h1 { font-size: 1.6rem; } }
</style>
</head>
<body>
<nav class="bar">
  <a class="brand" href="/">13ft</a>
  <a href="/library">Library</a>
  <a href="{{ snap.url }}" rel="noopener noreferrer">Original page ↗</a>
  {% if snap.has_raw_html %}<a href="/read/{{ snap.id }}/original">Saved layout</a>{% endif %}
  <a href="/?url={{ snap.url | urlencode }}&amp;refresh=1">Fetch again</a>
</nav>
<main class="wrap">
<article>
  {% if snap.site_name %}<p class="kicker">{{ snap.site_name }}</p>{% endif %}
  <h1>{{ snap.title or "Untitled" }}</h1>
  <p class="meta">
    {% if snap.byline %}By {{ snap.byline }} · {% endif %}
    {% if snap.published %}{{ snap.published }} · {% endif %}
    {{ snap.word_count }} words · about {{ reading_minutes }} min
  </p>
  {% if snap.warnings %}
  <div class="notice" role="note">
    <strong>This copy may be incomplete.</strong>
    <ul>{% for w in snap.warnings %}<li>{{ w.message }}</li>{% endfor %}</ul>
  </div>
  {% endif %}
  <div class="content">
{{ snap.content_html | safe }}
  </div>
</article>
<footer class="provenance">
  <p>Saved {{ snap.fetched_at | when }} using <strong>{{ method_label }}</strong>{% if snap.source_url and snap.source_url != snap.url %}, from <a href="{{ snap.source_url }}" rel="noopener noreferrer">{{ snap.source_url }}</a>{% endif %}.
  Text found by {{ "the page's JSON-LD metadata" if snap.content_source == "json-ld" else "main-content extraction" }}.</p>
  {% if snap.attempts %}
  <details><summary>How this copy was obtained</summary>
    <ol>{% for a in snap.attempts %}<li><span class="outcome-{{ a.outcome }}">{{ a.label }}</span>: {{ a.outcome_text }}{% if a.detail %} ({{ a.detail }}){% endif %}</li>{% endfor %}</ol>
  </details>
  {% endif %}
  {% if versions | length > 1 %}
  <details><summary>{{ versions | length }} saved copies of this article</summary>
    <ul>{% for v in versions %}<li>{% if v.id == snap.id %}<strong>{{ v.fetched_at | when }}</strong> (this copy){% else %}<a href="/read/{{ v.id }}">{{ v.fetched_at | when }}</a>{% endif %} · {{ v.word_count }} words · {{ labels.get(v.method, v.method) }}</li>{% endfor %}</ul>
  </details>
  {% endif %}
</footer>
</main>
</body>
</html>
"""

LIBRARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Library · 13ft</title>
<link rel="icon" href="/favicon.ico" type="image/png">
<style>
""" + BASE_CSS + """
h1 { font-size: 1.5rem; margin: 0 0 16px; }
form.search { display: flex; gap: 8px; margin-bottom: 8px; }
form.search input { flex: 1; min-width: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); color: var(--text); font: inherit; }
.count { font-size: 0.9rem; margin: 0 0 16px; }
ul.items { list-style: none; margin: 0; padding: 0; }
ul.items li { padding: 14px 0; border-bottom: 1px solid var(--line); display: flex; gap: 12px; align-items: flex-start; }
ul.items .info { flex: 1; min-width: 0; }
ul.items .title { font-weight: 600; text-decoration: none; color: var(--text); overflow-wrap: anywhere; }
ul.items .title:hover { color: var(--accent); }
ul.items .sub { font-size: 0.85rem; color: var(--muted); margin-top: 2px; overflow-wrap: anywhere; }
.badge { display: inline-block; font-size: 0.75rem; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--warn-line); background: var(--warn-bg); color: var(--warn-text); }
form.delete button { background: none; border: 0; color: var(--muted); cursor: pointer; font: inherit; font-size: 0.85rem; padding: 2px 4px; }
form.delete button:hover { color: var(--accent); }
.pager { display: flex; justify-content: space-between; margin-top: 20px; }
.empty { padding: 32px 0; color: var(--muted); }
</style>
</head>
<body>
<nav class="bar">
  <a class="brand" href="/">13ft</a>
  <a href="/library">Library</a>
</nav>
<main class="wrap">
  <h1>Library</h1>
  <form class="search" method="get" action="/library" role="search">
    <input type="search" name="q" value="{{ q }}" placeholder="Search titles, sites and full text" aria-label="Search the library">
    <button class="button" type="submit">Search</button>
  </form>
  <p class="count muted">{% if q %}{{ total }} match{{ "" if total == 1 else "es" }} for “{{ q }}”{% else %}{{ total }} saved article{{ "" if total == 1 else "s" }}{% endif %}</p>
  {% if items %}
  <ul class="items">
    {% for it in items %}
    <li>
      <div class="info">
        <a class="title" href="/read/{{ it.id }}">{{ it.title or it.url }}</a>
        <div class="sub">{{ it.site_name or it.url_key }} · saved {{ it.fetched_at | when }} · {{ it.word_count }} words · {{ labels.get(it.method, it.method) }}
          {% if not it.complete %} <span class="badge">may be incomplete</span>{% endif %}</div>
      </div>
      <form class="delete" method="post" action="/library/{{ it.id }}/delete">
        <button type="submit" title="Delete this saved copy">Delete</button>
      </form>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty">{% if q %}Nothing matches that search.{% else %}Nothing saved yet. Articles you read through 13ft are kept here.{% endif %}</p>
  {% endif %}
  {% if pages > 1 %}
  <div class="pager">
    {% if page > 1 %}<a class="button secondary" href="/library?q={{ q | urlencode }}&amp;page={{ page - 1 }}">← Newer</a>{% else %}<span></span>{% endif %}
    <span class="muted">Page {{ page }} of {{ pages }}</span>
    {% if page < pages %}<a class="button secondary" href="/library?q={{ q | urlencode }}&amp;page={{ page + 1 }}">Older →</a>{% else %}<span></span>{% endif %}
  </div>
  {% endif %}
</main>
</body>
</html>
"""
