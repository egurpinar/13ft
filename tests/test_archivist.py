"""Offline tests for the archivist package (guard, URLs, extraction, rendering, store, headless)."""
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from archivist import extract, headless, netguard, render, urls  # noqa: E402
from archivist.store import ArchiveStore  # noqa: E402
from helpers import PUBLIC_IP, FakeResponse, page, paragraphs  # noqa: E402
from lxml import etree  # noqa: E402


class NetguardTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ALLOW_PRIVATE_TARGETS", None)

    def test_public_and_private_addresses(self):
        public = ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111"]
        private = ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "::1", "fe80::1%eth0", "fc00::1", "::ffff:192.168.1.1", "224.0.0.1"]
        for address in public:
            self.assertTrue(netguard.is_public_ip(address), address)
        for address in private:
            self.assertFalse(netguard.is_public_ip(address), address)

    def test_check_url_rejects_schemes_and_local_hosts(self):
        for url in ("file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "http://localhost:5000/",
                    "http://api.localhost/", "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest",
                    "http://example.com:99999/", "https:///nohost"):
            with self.subTest(url=url), self.assertRaises(netguard.BlockedTarget):
                netguard.check_url(url)

    def test_check_url_uses_dns(self):
        with patch.object(netguard, "resolve", return_value={PUBLIC_IP}):
            netguard.check_url("https://example.com/a")
        with patch.object(netguard, "resolve", return_value={PUBLIC_IP, "10.0.0.1"}):
            with self.assertRaises(netguard.BlockedTarget):
                netguard.check_url("https://rebind.example.com/")

    def test_unresolvable_host_is_left_to_the_request(self):
        with patch.object(netguard, "resolve", side_effect=netguard.socket.gaierror()):
            netguard.check_url("https://does-not-resolve.example/")

    def test_override(self):
        with patch.dict(os.environ, {"ALLOW_PRIVATE_TARGETS": "1"}):
            netguard.check_url("http://192.168.1.1/")
            with self.assertRaises(netguard.BlockedTarget):
                netguard.check_url("file:///etc/passwd")  # scheme rules still apply

    def test_redirect_to_private_address_is_blocked(self):
        hops = [FakeResponse(status_code=302, url="https://example.com/a", headers={"location": "http://192.168.1.1/admin"})]
        with patch.object(netguard, "resolve", return_value={PUBLIC_IP}), \
                patch.object(netguard.requests.Session, "get", side_effect=hops) as get:
            with self.assertRaises(netguard.BlockedTarget):
                netguard.guarded_get("https://example.com/a")
        self.assertEqual(get.call_count, 1)

    def test_redirects_are_followed_with_checks(self):
        hops = [
            FakeResponse(status_code=301, url="https://example.com/a", headers={"location": "/b"}),
            FakeResponse(text="final", url="https://example.com/b"),
        ]
        with patch.object(netguard, "resolve", return_value={PUBLIC_IP}) as resolve, \
                patch.object(netguard.requests.Session, "get", side_effect=hops) as get:
            response = netguard.guarded_get("https://example.com/a", headers={"User-Agent": "x"})
        self.assertEqual(response.text, "final")
        self.assertEqual(get.call_args_list[1].args[0], "https://example.com/b")
        self.assertFalse(get.call_args_list[1].kwargs["allow_redirects"])
        self.assertEqual(resolve.call_count, 2)

    def test_redirect_limit(self):
        loop = FakeResponse(status_code=302, url="https://example.com/a", headers={"location": "/a"})
        with patch.object(netguard, "resolve", return_value={PUBLIC_IP}), \
                patch.object(netguard.requests.Session, "get", return_value=loop):
            with self.assertRaises(netguard.requests.exceptions.TooManyRedirects):
                netguard.guarded_get("https://example.com/a")


class UrlTests(unittest.TestCase):
    def test_parse_target(self):
        cases = {
            "https://example.com/a?b=1": "https://example.com/a?b=1",
            "/https://example.com/a": "https://example.com/a",
            "https:/example.com/a": "https://example.com/a",
            "HTTP://Example.com/a": "http://Example.com/a",
            "example.com/a/b": "https://example.com/a/b",
            "www.example.co.uk": "https://www.example.co.uk",
            "  example.com  ": "https://example.com",
        }
        for raw, expected in cases.items():
            self.assertEqual(urls.parse_target(raw), expected, raw)
        for raw in ("", "not-a-url", "javascript:alert(1)", "https://", "/library"):
            self.assertIsNone(urls.parse_target(raw), raw)

    def test_strip_tracking_on_newsletter_link(self):
        link = ("https://www.ctpost.com/realestate/article/stratford-ct-largest-condo-development-raymark-22525420.php"
                "?utm_content=img&sid=67eb49c082f75f66f30cffd4&ss=P&st_rid=null&utm_source=newsletter"
                "&utm_medium=email&utm_campaign=CT_PO_MorningBriefing#comments")
        self.assertEqual(
            urls.strip_tracking(link),
            "https://www.ctpost.com/realestate/article/stratford-ct-largest-condo-development-raymark-22525420.php"
            "?sid=67eb49c082f75f66f30cffd4&ss=P",
        )

    def test_archive_key_treats_variants_as_one_article(self):
        variants = ["https://www.example.com/a/?utm_source=x", "http://example.com/a", "https://EXAMPLE.com:443/a#top"]
        self.assertEqual(len({urls.archive_key(v) for v in variants}), 1)
        self.assertNotEqual(urls.archive_key("https://example.com/a?id=1"), urls.archive_key("https://example.com/a?id=2"))
        self.assertEqual(urls.archive_key("https://example.com/a?b=2&a=1"), urls.archive_key("https://example.com/a?a=1&b=2"))


class ExtractTests(unittest.TestCase):
    URL = "https://news.example.com/story"

    def test_complete_article(self):
        article = extract.extract_article(page(paragraphs(10)), self.URL)
        self.assertTrue(article.complete)
        self.assertEqual(article.content_source, "trafilatura")
        self.assertGreater(article.word_count, 200)  # 10 paragraphs x 23 words
        self.assertEqual(article.title, "Test Story")

    def test_text_hidden_by_css_or_attributes_is_recovered(self):
        hidden = "".join(f"<p>{p}</p>" for p in paragraphs(9, start=1))
        for wrapper in (f'<div class="paywalled" style="display:none">{hidden}</div>',
                        f'<div hidden aria-hidden="true">{hidden}</div>'):
            with self.subTest(wrapper=wrapper[:40]):
                article = extract.extract_article(page(paragraphs(1), extra_body=wrapper), self.URL)
                self.assertIn("Paragraph 9", article.content_text)
                self.assertTrue(article.complete)

    def test_json_ld_body_beats_teaser(self):
        ld = {"@context": "https://schema.org", "@graph": [
            {"@type": "WebPage", "name": "ignored"},
            {"@type": ["NewsArticle"], "headline": "LD Headline", "datePublished": "2026-11-20T08:00:00-05:00",
             "author": [{"@type": "Person", "name": "A. Writer"}, {"@type": "Person", "name": "B. Editor"}],
             "publisher": {"@type": "Organization", "name": "Example Times"},
             "articleBody": "\n\n".join(paragraphs(10)), "isAccessibleForFree": "False"},
        ]}
        teaser = page(paragraphs(1), extra_body="<p>Already a subscriber? Sign in to continue reading.</p>", ld=ld)
        article = extract.extract_article(teaser, self.URL)
        self.assertEqual(article.content_source, "json-ld")
        self.assertTrue(article.complete)
        self.assertEqual(article.title, "LD Headline")
        self.assertEqual(article.byline, "A. Writer, B. Editor")
        self.assertEqual(article.published, "2026-11-20")
        self.assertEqual(article.site_name, "Example Times")
        self.assertIs(article.accessible_for_free, False)
        self.assertEqual(article.content_html.count("<p>"), 10)

    def test_short_teaser_with_prompt_is_flagged_and_prompt_removed(self):
        teaser = page(paragraphs(2), extra_body="<p>Subscribe now to continue reading this story.</p>")
        article = extract.extract_article(teaser, self.URL)
        self.assertFalse(article.complete)
        self.assertEqual({w["code"] for w in article.warnings}, {"prompt", "short"})
        self.assertNotIn("Subscribe now", article.content_text)

    def test_prompt_after_long_text_is_treated_as_overlay(self):
        article = extract.extract_article(
            page(paragraphs(16), extra_body="<p>You have reached your free article limit. Subscribe now.</p>"), self.URL)
        self.assertTrue(article.complete)
        self.assertNotIn("free article limit", article.content_text)

    def test_word_count_metadata_mismatch(self):
        ld = {"@type": "NewsArticle", "headline": "Test Story", "wordCount": "1,200"}
        article = extract.extract_article(page(paragraphs(6), ld=ld), self.URL)
        self.assertEqual(article.expected_words, 1200)
        self.assertIn("expected", [w["code"] for w in article.warnings])
        self.assertLess(article.score, article.word_count)

    def test_broken_json_ld_is_ignored(self):
        broken = page(paragraphs(10)).replace("</head>", '<script type="application/ld+json">{not json,,}</script></head>')
        self.assertTrue(extract.extract_article(broken, self.URL).complete)

    def test_no_article(self):
        self.assertIsNone(extract.extract_article("", self.URL))
        self.assertIsNone(extract.extract_article("<html><body></body></html>", self.URL))


class RenderTests(unittest.TestCase):
    def render(self, xml):
        return render.blocks_to_html(list(etree.fromstring(xml)))

    def test_markup_is_escaped_and_unsafe_urls_dropped(self):
        out = self.render(
            '<main><p>&lt;script&gt;alert(1)&lt;/script&gt; <ref target="javascript:alert(1)">bad</ref> '
            '<ref target="https://ok.example/x?a=1&amp;b=&quot;2">good</ref></p>'
            '<graphic src="data:image/svg+xml,&lt;svg onload=alert(1)&gt;" alt="x"/>'
            '<graphic src="https://img.example/a.jpg" alt="&quot; onerror=&quot;alert(1)"/></main>'
        )
        self.assertNotIn("<script", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("javascript:", out)
        self.assertIn('href="https://ok.example/x?a=1&amp;b=&quot;2"', out)
        self.assertNotIn("data:image", out)
        self.assertIn('alt="&quot; onerror=&quot;alert(1)"', out)
        self.assertEqual(out.count("<img"), 1)

    def test_structure(self):
        out = self.render(
            '<main><head rend="h1">Title</head><head rend="h3">Sub</head>'
            '<p>A <hi rend="#b">bold</hi> and <hi rend="#i">it</hi> word<lb/>next</p>'
            '<list rend="ol"><item>one</item><item>two <list rend="ul"><item>nested</item></list></item></list>'
            '<quote><p>Quoted</p></quote><code>x &lt; 1</code>'
            '<table><row><cell role="head">Vin</cell></row><row><cell>48 V</cell></row></table>'
            '<p>   </p><unknown>kept text</unknown></main>'
        )
        for fragment in ("<h2>Title</h2>", "<h3>Sub</h3>", "<strong>bold</strong>", "<em>it</em>", "word<br>next",
                         "<ol><li>one</li><li>two <ul><li>nested</li></ul></li></ol>",
                         "<blockquote><p>Quoted</p></blockquote>", "<pre><code>x &lt; 1</code></pre>",
                         "<th>Vin</th>", "<td>48 V</td>", "<p>kept text</p>"):
            self.assertIn(fragment, out)
        self.assertNotIn("<p>   </p>", out)

    def test_text_to_html(self):
        self.assertEqual(render.text_to_html("a <b>\n\nsecond"), "<p>a &lt;b&gt;</p>\n<p>second</p>")
        self.assertEqual(render.text_to_html("one\ntwo"), "<p>one</p>\n<p>two</p>")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ArchiveStore(Path(self.tmp.name) / "a.sqlite3")

    def add(self, **kw):
        base = dict(url="https://e.com/a", url_key="https://e.com/a", method="googlebot",
                    content_html="<p>x</p>", content_text="x", title="T")
        base.update(kw)
        return self.store.save(**base)

    def test_round_trip(self):
        sid = self.add(raw_html="<html>raw ü</html>", warnings=[{"code": "short", "message": "m"}],
                       attempts=[{"source": "googlebot"}], complete=False, word_count=5)
        row = self.store.get(sid)
        self.assertEqual(row["warnings"][0]["code"], "short")
        self.assertEqual(row["attempts"][0]["source"], "googlebot")
        self.assertIs(row["complete"], False)
        self.assertTrue(row["has_raw_html"])
        self.assertEqual(self.store.raw_html(sid), "<html>raw ü</html>")
        self.assertIsNone(self.store.get(sid + 100))

    def test_latest_and_versions(self):
        first = self.add(fetched_at="2026-01-01T00:00:00+00:00")
        second = self.add(fetched_at="2026-02-01T00:00:00+00:00")
        self.assertEqual(self.store.latest_for("https://e.com/a")["id"], second)
        self.assertEqual([v["id"] for v in self.store.versions("https://e.com/a")], [second, first])

    def test_full_text_search(self):
        self.assertTrue(self.store.fts)
        self.add(title="Gallium nitride", content_text="GaN half bridge layout")
        self.add(title="Other", content_text="nothing relevant", url_key="k2")
        rows, total = self.store.search("half bri")  # prefix match on every word
        self.assertEqual((total, rows[0]["title"]), (1, "Gallium nitride"))
        rows, total = self.store.search('"quoted" OR NEAR(')  # FTS syntax is neutralised, not an error
        self.assertEqual(total, 0)
        self.assertEqual(self.store.search("")[1], 2)

    def test_like_fallback(self):
        self.store.fts = False
        self.add(title="100% duty cycle", content_text="boost")
        self.add(title="Other", content_text="buck", url_key="k2")
        self.assertEqual(self.store.search("100%")[1], 1)
        self.assertEqual(self.store.search("_")[1], 0)

    def test_delete(self):
        sid = self.add(content_text="unique-word")
        self.assertTrue(self.store.delete(sid))
        self.assertFalse(self.store.delete(sid))
        self.assertEqual(self.store.search("unique")[1], 0)  # FTS index updated too


class HeadlessTests(unittest.TestCase):
    def test_vendor_host_matching(self):
        self.assertTrue(headless.is_blocked_host("cdn.tinypass.com"))
        self.assertTrue(headless.is_blocked_host("experience.piano.io"))
        self.assertFalse(headless.is_blocked_host("notpiano.io"))
        self.assertFalse(headless.is_blocked_host(None))
        with patch.dict(os.environ, {"BLOCK_SCRIPT_HOSTS": "paywall.example, other.example"}):
            self.assertTrue(headless.is_blocked_host("js.paywall.example"))

    def test_off_by_default(self):
        with patch.dict(os.environ):
            os.environ.pop("HEADLESS", None)
            self.assertFalse(headless.enabled())

    @unittest.skipUnless(shutil.which("chromium") or os.environ.get("CHROMIUM_PATH"), "needs Chromium (set CHROMIUM_PATH)")
    def test_renders_javascript_page(self):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        site = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, site)
        body = json.dumps("".join(f"<p>{p}</p>" for p in paragraphs(8)))
        Path(site, "js.html").write_text(
            "<html><head><title>JS Story</title></head><body><div id=root>Loading</div>"
            f"<script>document.getElementById('root').innerHTML = '<article><h1>JS Story</h1>' + {body} + '</article>';</script>"
            "</body></html>")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site)
        handler.log_message = lambda *a: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        proxy_vars = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")}
        with patch.dict(os.environ, {"ALLOW_PRIVATE_TARGETS": "1", **proxy_vars}):
            headless._host_allowed.cache_clear()
            html, final = headless.fetch_rendered(f"http://127.0.0.1:{server.server_port}/js.html")
        headless._host_allowed.cache_clear()
        article = extract.extract_article(html, final)
        self.assertTrue(article.complete)
        self.assertIn("Paragraph 7", article.content_text)


if __name__ == "__main__":
    unittest.main()
