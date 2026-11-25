"""Offline tests for the web app. Run: python -m pytest -q  (or python -m unittest discover -s tests -v)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import portable as web  # noqa: E402
from archivist import netguard  # noqa: E402
from archivist import store as archive_store  # noqa: E402
from helpers import FULL_ARTICLE, PUBLIC_IP, TEASER, URL, FakeResponse, page, paragraphs  # noqa: E402


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALE": "en", "DATA_DIR": self.tmp.name})
        self.env.start()  # restores the whole environment on stop, popped keys included
        self.addCleanup(self.env.stop)
        for name in ("SOURCES", "HEADLESS", "ALLOWED_HOSTS", "ALLOW_PRIVATE_TARGETS", "MIN_WORDS", "STORE_RAW_HTML"):
            os.environ.pop(name, None)
        # No real network or DNS anywhere in the suite.
        self.network = patch.object(web.requests.sessions.Session, "request", side_effect=AssertionError("Unexpected network access"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.dns = patch.object(netguard, "resolve", return_value={PUBLIC_IP})
        self.dns.start()
        self.addCleanup(self.dns.stop)
        web.jobs.clear()
        web.load_strings_for_locale.cache_clear()
        self.addCleanup(web.jobs.clear)
        self.addCleanup(web.load_strings_for_locale.cache_clear)
        self.client = web.app.test_client()
        self.strings = web.load_strings()

    @property
    def store(self):
        return archive_store.get_store()

    def save_snapshot(self, **overrides):
        record = dict(
            url=URL, url_key=web.urltools.archive_key(URL), method="googlebot",
            title="Saved Story", site_name="Example News", content_html="<p>Saved body text.</p>",
            content_text="Saved body text.", word_count=3, complete=True, raw_html=FULL_ARTICLE,
        )
        record.update(overrides)
        return self.store.save(**record)

    def run_sources(self, responses):
        """Patch the source functions with canned (html, url) results or exceptions."""
        patches = []
        for name, result in responses.items():
            function_name = web.SOURCES[name][0]
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            p = patch.object(web, function_name, **kwargs)
            patches.append(p)
        mocks = {name: p.start() for name, p in zip(responses, patches)}
        for p in patches:
            self.addCleanup(p.stop)
        return mocks


class PageTests(AppTestCase):
    def test_home_renders_form(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter Website Link", response.data)
        self.assertIn(b'<form', response.data)
        self.assertIn(b'href="/library"', response.data)

    def test_portuguese_home(self):
        with patch.dict(os.environ, {"LOCALE": "pt"}):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'lang="pt"', response.data)

    def test_unknown_locale_falls_back_to_english(self):
        with patch.dict(os.environ, {"LOCALE": "nonexistent"}):
            self.assertEqual(web.load_strings()["lang"], "en")

    def test_locale_path_traversal_falls_back(self):
        with patch.dict(os.environ, {"LOCALE": "../../etc/passwd"}):
            self.assertEqual(web.get_locale(), "en")

    def test_home_with_url_autostarts(self):
        response = self.client.get("/", query_string={"url": URL, "refresh": "1"})
        self.assertIn(b'"refresh": true', response.data)
        self.assertIn(URL.encode(), response.data)

    def test_robots_txt(self):
        self.assertIn(b"Disallow: /", self.client.get("/robots.txt").data)

    def test_html_fixes_relative_links(self):
        soup = web.BeautifulSoup(web.process_html_document(FULL_ARTICLE, URL), "html.parser")
        self.assertEqual(soup.base["href"], "https://example.com/news/")
        self.assertIn("Paragraph 3", soup.get_text())

    def test_html_replaces_existing_base(self):
        source = '<html><head><base href="/"></head><body>Article</body></html>'
        soup = web.BeautifulSoup(web.process_html_document(source, URL), "html.parser")
        self.assertEqual(len(soup.find_all("base")), 1)
        self.assertEqual(soup.base["href"], "https://example.com/news/")


class PipelineTests(AppTestCase):
    def test_direct_fetch_adds_scheme_and_googlebot(self):
        with patch.object(web, "http_get", return_value=FakeResponse(FULL_ARTICLE)) as get:
            snapshot_id = web.fetch_article("example.com/news/article", self.strings)
        get.assert_called_once_with(URL, headers=web.googlebot_headers, timeout=30)
        snapshot = self.store.get(snapshot_id)
        self.assertEqual(snapshot["method"], "googlebot")
        self.assertTrue(snapshot["complete"])
        self.assertIn("Paragraph 9", snapshot["content_text"])

    def test_tracking_parameters_are_not_sent(self):
        link = URL + "?utm_content=img&sid=abc&ss=P&st_rid=null&utm_source=newsletter&utm_medium=email"
        with patch.object(web, "http_get", return_value=FakeResponse(FULL_ARTICLE)) as get:
            web.fetch_article(link, self.strings)
        self.assertEqual(get.call_args.args[0], URL + "?sid=abc&ss=P")

    def test_teaser_falls_through_to_next_source(self):
        mocks = self.run_sources({
            "googlebot": (TEASER, URL),
            "clean": (FULL_ARTICLE, URL),
            "wayback": AssertionError("should not be reached"),
        })
        snapshot = self.store.get(web.fetch_article(URL, self.strings))
        self.assertEqual(snapshot["method"], "clean")
        self.assertEqual([a["outcome"] for a in snapshot["attempts"]], ["partial", "complete"])
        mocks["wayback"].assert_not_called()

    def test_best_partial_copy_is_kept_with_warnings(self):
        longer_teaser = page(paragraphs(3), extra_body="<p>Subscribe now to continue reading.</p>")
        self.run_sources({
            "googlebot": (TEASER, URL),
            "clean": (longer_teaser, URL),
            "wayback": (None, None),
            "archive_today": (None, None),
        })
        snapshot = self.store.get(web.fetch_article(URL, self.strings))
        self.assertEqual(snapshot["method"], "clean")
        self.assertFalse(snapshot["complete"])
        self.assertIn("prompt", [w["code"] for w in snapshot["warnings"]])
        self.assertNotIn("Subscribe now", snapshot["content_text"])

    def test_blocked_response_uses_archive(self):
        archived = "https://web.archive.org/web/20260101000000id_/" + URL
        with patch.object(web, "http_get", return_value=FakeResponse("Blocked", status_code=403)), \
                patch.object(web, "fetch_via_archive_org", return_value=(FULL_ARTICLE, archived)) as archive, \
                patch.object(web, "fetch_via_archive_ph") as fallback:
            snapshot = self.store.get(web.fetch_article(URL, self.strings))
        archive.assert_called_once_with(URL, None)
        fallback.assert_not_called()
        self.assertEqual(snapshot["method"], "wayback")
        self.assertEqual(snapshot["source_url"], archived)
        self.assertEqual([a["detail"] for a in snapshot["attempts"][:2]], ["HTTP 403", "HTTP 403"])

    def test_second_archive_is_used_when_first_is_empty(self):
        with patch.object(web, "http_get", return_value=FakeResponse("Blocked", status_code=403)), \
                patch.object(web, "fetch_via_archive_org", return_value=(None, None)), \
                patch.object(web, "fetch_via_archive_ph", return_value=(FULL_ARTICLE, URL)) as fallback:
            snapshot = self.store.get(web.fetch_article(URL, self.strings))
        fallback.assert_called_once_with(URL, None)
        self.assertEqual(snapshot["method"], "archive_today")

    def test_challenge_pages_are_skipped(self):
        challenge = "<html><title>Just a moment...</title><body>Checking your browser</body></html>"
        self.run_sources({"googlebot": (challenge, URL), "clean": (FULL_ARTICLE, URL)})
        snapshot = self.store.get(web.fetch_article(URL, self.strings))
        self.assertEqual(snapshot["attempts"][0]["outcome"], "challenge")
        self.assertEqual(snapshot["method"], "clean")

    def test_request_errors_fall_through(self):
        self.run_sources({
            "googlebot": web.requests.exceptions.Timeout(),
            "clean": web.requests.exceptions.ConnectionError(),
            "wayback": (FULL_ARTICLE, URL),
        })
        snapshot = self.store.get(web.fetch_article(URL, self.strings))
        self.assertEqual([a["detail"] for a in snapshot["attempts"][:2]], ["timed out", "could not connect"])

    def test_all_sources_empty_raise_user_error(self):
        with patch.object(web, "http_get", return_value=FakeResponse("Blocked", status_code=403)), \
                patch.object(web, "fetch_via_archive_org", return_value=(None, None)), \
                patch.object(web, "fetch_via_archive_ph", return_value=(None, None)):
            with self.assertRaises(web.UserFacingError) as error:
                web.fetch_article(URL, self.strings)
        message = str(error.exception)
        self.assertTrue(message.startswith(self.strings["nothing_found_error"]))
        self.assertIn("Wayback Machine: nothing usable (no usable snapshot)", message)

    def test_private_targets_are_refused_before_any_request(self):
        for link in ("http://127.0.0.1:8080/admin", "http://192.168.1.1/", "http://localhost/", "file:///etc/passwd"):
            with self.subTest(link=link), patch.object(web, "http_get") as get:
                with self.assertRaises(web.UserFacingError) as error:
                    web.fetch_article(link, self.strings)
                get.assert_not_called()
                self.assertIn(str(error.exception), (self.strings["blocked_target_error"], self.strings["invalid_url"]))

    def test_hostname_resolving_to_private_address_is_refused(self):
        with patch.object(netguard, "resolve", return_value={"10.0.0.5"}), patch.object(web, "http_get") as get:
            with self.assertRaises(web.UserFacingError):
                web.fetch_article("https://intranet.example.com/", self.strings)
        get.assert_not_called()

    def test_archived_article_is_reused_unless_refresh(self):
        existing = self.save_snapshot()
        with patch.object(web, "http_get") as get:
            self.assertEqual(web.fetch_article(URL + "?utm_source=x", self.strings), existing)
        get.assert_not_called()
        with patch.object(web, "http_get", return_value=FakeResponse(FULL_ARTICLE)):
            refreshed = web.fetch_article(URL, self.strings, refresh=True)
        self.assertNotEqual(refreshed, existing)
        self.assertEqual(len(self.store.versions(web.urltools.archive_key(URL))), 2)

    def test_medium_uses_freedium_before_direct_fetch(self):
        with patch.object(web, "fetch_via_freedium", return_value=(FULL_ARTICLE, "https://freedium.cfd/x")), \
                patch.object(web, "http_get") as get:
            snapshot = self.store.get(web.fetch_article("https://medium.com/story", self.strings))
        get.assert_not_called()
        self.assertEqual(snapshot["method"], "freedium")

    def test_sources_env_controls_order_and_headless_needs_opt_in(self):
        with patch.dict(os.environ, {"SOURCES": "wayback, googlebot, nonsense, headless"}):
            self.assertEqual(web.configured_sources(URL), ["wayback", "googlebot"])
            with patch.object(web.headless, "enabled", return_value=True):
                self.assertEqual(web.configured_sources(URL), ["wayback", "googlebot", "headless"])

    def test_raw_html_storage_can_be_disabled(self):
        with patch.dict(os.environ, {"STORE_RAW_HTML": "0"}), \
                patch.object(web, "http_get", return_value=FakeResponse(FULL_ARTICLE)):
            snapshot_id = web.fetch_article(URL, self.strings)
        self.assertIsNone(self.store.raw_html(snapshot_id))


class RouteTests(AppTestCase):
    def sync_threads(self):
        def start(thread):
            thread._target(*thread._args, **thread._kwargs)
        p = patch.object(web.threading.Thread, "start", start)
        p.start()
        self.addCleanup(p.stop)

    def test_article_post_redirects_to_reader(self):
        with patch.object(web, "fetch_article", return_value=7) as fetch:
            response = self.client.post("/article", data={"link": URL})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/read/7")
        fetch.assert_called_once_with(URL, self.strings)

    def test_article_requires_link(self):
        self.assertEqual(self.client.post("/article", data={}).status_code, 400)

    def test_article_errors_are_user_facing(self):
        cases = [(web.UserFacingError("Unavailable"), "Unavailable", 400),
                 (RuntimeError("private details"), self.strings["unexpected_error"], 500)]
        for error, message, status in cases:
            with self.subTest(error=type(error).__name__), patch.object(web, "fetch_article", side_effect=error):
                response = self.client.post("/article", data={"link": URL})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_data(as_text=True), message)

    def test_status_requires_url(self):
        self.assertEqual(self.client.get("/status").status_code, 400)

    def test_status_stream_success_and_cleanup(self):
        self.sync_threads()
        with patch.object(web, "fetch_article", return_value=3) as fetch:
            response = self.client.get("/status", query_string={"url": URL, "refresh": "1"})
            body = response.get_data(as_text=True)
        self.assertEqual(response.mimetype, "text/event-stream")
        event = json.loads(body.split("data: ", 1)[1].strip())
        self.assertEqual(event, {"step": "done", "redirect": "/read/3"})
        self.assertTrue(fetch.call_args.kwargs["refresh"])
        self.assertEqual(web.jobs, {})

    def test_status_stream_failure_and_cleanup(self):
        self.sync_threads()
        with patch.object(web, "fetch_article", side_effect=web.UserFacingError("Nope")):
            body = self.client.get("/status", query_string={"url": URL}).get_data(as_text=True)
        self.assertIn("event: error_msg", body)
        self.assertIn("Nope", body)
        self.assertEqual(web.jobs, {})

    def test_status_stream_hides_unexpected_errors(self):
        self.sync_threads()
        with patch.object(web, "fetch_article", side_effect=RuntimeError("secret path /home/x")):
            body = self.client.get("/status", query_string={"url": URL}).get_data(as_text=True)
        self.assertIn(self.strings["unexpected_error"], body)
        self.assertNotIn("secret", body)

    def test_reader_page_is_script_free_and_escaped(self):
        snapshot_id = self.save_snapshot(title='<script>alert(1)</script>Evil', byline="A <b>Writer</b>",
                                         warnings=[{"code": "short", "message": "Only 3 words were found."}])
        response = self.client.get(f"/read/{snapshot_id}")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)  # no script-src, so no scripts at all
        self.assertNotIn("script-src", csp)
        self.assertNotIn("<script>alert", response.get_data(as_text=True))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;Evil", response.get_data(as_text=True))
        self.assertIn("This copy may be incomplete", response.get_data(as_text=True))
        self.assertIn("Saved body text.", response.get_data(as_text=True))

    def test_reader_page_404(self):
        self.assertEqual(self.client.get("/read/999").status_code, 404)

    def test_original_view_is_sandboxed(self):
        snapshot_id = self.save_snapshot()
        response = self.client.get(f"/read/{snapshot_id}/original")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["Content-Security-Policy"]
        self.assertTrue(csp.startswith("sandbox"))
        self.assertNotIn("allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn('<base href="https://example.com/news/"', response.get_data(as_text=True))
        csp_js = self.client.get(f"/read/{snapshot_id}/original?js=1").headers["Content-Security-Policy"]
        self.assertIn("allow-scripts", csp_js)
        self.assertNotIn("allow-same-origin", csp_js)

    def test_original_view_without_raw_html_is_404(self):
        snapshot_id = self.save_snapshot(raw_html=None)
        self.assertEqual(self.client.get(f"/read/{snapshot_id}/original").status_code, 404)

    def test_library_lists_and_searches(self):
        self.save_snapshot(title="Inverter efficiency", content_text="gallium nitride switching")
        self.save_snapshot(title="Rowing results", content_text="regatta on the river", url="https://example.org/r",
                           url_key="https://example.org/r")
        page_all = self.client.get("/library").get_data(as_text=True)
        self.assertIn("Inverter efficiency", page_all)
        self.assertIn("Rowing results", page_all)
        page_search = self.client.get("/library?q=gallium").get_data(as_text=True)
        self.assertIn("Inverter efficiency", page_search)
        self.assertNotIn("Rowing results", page_search)

    def test_delete_requires_same_site(self):
        snapshot_id = self.save_snapshot()
        refused = self.client.post(f"/library/{snapshot_id}/delete", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(refused.status_code, 403)
        self.assertIsNotNone(self.store.get(snapshot_id))
        refused_origin = self.client.post(f"/library/{snapshot_id}/delete", headers={"Origin": "https://evil.example"})
        self.assertEqual(refused_origin.status_code, 403)
        accepted = self.client.post(f"/library/{snapshot_id}/delete", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(accepted.status_code, 303)
        self.assertIsNone(self.store.get(snapshot_id))

    def test_host_header_check(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "127.0.0.1:5000"}).status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "[::1]:5000"}).status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "192.168.1.20:5000"}).status_code, 200)
        with patch.dict(os.environ, {"ALLOWED_HOSTS": "raspberrypi.local, reader.lan"}):
            self.assertEqual(self.client.get("/", headers={"Host": "raspberrypi.local:5000"}).status_code, 200)

    def test_path_style_link_opens_archived_copy(self):
        snapshot_id = self.save_snapshot()
        response = self.client.get("/" + URL + "?utm_source=newsletter")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/read/{snapshot_id}")

    def test_path_style_link_starts_fetch_for_new_article(self):
        response = self.client.get("/" + URL + "?id=5&utm_campaign=x")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('"url": "https://example.com/news/article?id=5"', body)

    def test_path_style_without_scheme(self):
        response = self.client.get("/example.com/news/article")
        self.assertIn('"url": "https://example.com/news/article"', response.get_data(as_text=True))

    def test_path_style_rejects_garbage(self):
        self.assertEqual(self.client.get("/not-a-url").status_code, 400)


if __name__ == "__main__":
    unittest.main()
