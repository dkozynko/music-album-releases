import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_update import updater, wiki_payload


TABLE = wiki_payload('<tr><td>Oct 2</td><td>Browser Act</td><td><a href="https://browseract.bandcamp.com/album/record">Record</a></td><td>Kyiv</td></tr>')["data"]["content_html"]


class FixtureBrowser:
    def __init__(self, profile_dir, headless=True):
        self.profile_dir, self.headless = profile_dir, headless

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read_wiki(self, year, timeout=45):
        return '<script>window.secret="private-session-token"</script><table><tr><th>Account</th></tr><tr><td>private-email@example.com</td></tr></table>' + TABLE


def browser_module(browser=FixtureBrowser):
    module = types.ModuleType("reddit_browser")
    module.RedditBrowser = browser
    return module


class RedditBrowserTests(unittest.TestCase):
    def test_browser_fetch_caches_only_public_observations_and_offline_needs_no_browser(self):
        m = updater()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "http"
            with patch.dict(sys.modules, {"reddit_browser": browser_module()}):
                rows, errors = m.collect_reddit_browser({2026}, cache)
            self.assertEqual(errors, [])
            self.assertEqual([(r["artist"], r["album"], r["release_date"]) for r in rows],
                             [("Browser Act", "Record", "2026-10-02")])
            files = list(cache.rglob("*.json"))
            self.assertEqual(len(files), 1)
            contents = files[0].read_text()
            self.assertNotIn("private-session-token", contents)
            self.assertNotIn("private-email", contents)
            self.assertNotIn("<script", contents)
            with patch.dict(sys.modules, {"reddit_browser": None}):
                saved, offline_errors = m.collect_reddit_browser({2026}, cache, offline=True)
            self.assertEqual(offline_errors, [])
            self.assertEqual(m.merge_catalog(saved), m.merge_catalog(rows))

    def test_challenge_does_not_replace_good_cache_and_reports_stale_use(self):
        m = updater()
        class ChallengeBrowser(FixtureBrowser):
            def read_wiki(self, year, timeout=45):
                return '<html><title>Prove your humanity</title><h1>Log in</h1></html>'
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "http"
            with patch.dict(sys.modules, {"reddit_browser": browser_module()}):
                initial, errors = m.collect_reddit_browser({2026}, cache)
            self.assertEqual(errors, [])
            saved = {p: p.read_bytes() for p in cache.rglob("*.json")}
            with patch.dict(sys.modules, {"reddit_browser": browser_module(ChallengeBrowser)}):
                rows, errors = m.collect_reddit_browser({2026}, cache, refresh=True)
            self.assertEqual(m.merge_catalog(rows), m.merge_catalog(initial))
            self.assertTrue(any("2026" in error and "stale" in error.lower() for error in errors))
            self.assertEqual({p: p.read_bytes() for p in cache.rglob("*.json")}, saved)

    def test_missing_offline_cache_never_launches_browser(self):
        m = updater()
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"reddit_browser": None}):
            rows, errors = m.collect_reddit_browser({2026}, Path(directory), offline=True)
        self.assertEqual(rows, [])
        self.assertTrue(any("2026" in error and "cache" in error.lower() for error in errors))

    def test_browser_startup_failure_is_a_source_warning(self):
        m = updater()
        class BrokenBrowser(FixtureBrowser):
            def __enter__(self):
                raise ValueError("Chrome profile is already in use")
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"reddit_browser": browser_module(BrokenBrowser)}):
            rows, errors = m.collect_reddit_browser({2025, 2026}, Path(directory))
        self.assertEqual(rows, [])
        self.assertTrue(any("2025" in error for error in errors))
        self.assertTrue(any("2026" in error for error in errors))

    def test_playwright_driver_start_failure_preserves_stale_observations(self):
        m = updater()
        class DriverError(Exception):
            pass
        api = types.ModuleType("playwright.sync_api")
        api.Error = DriverError
        api.sync_playwright = Mock(return_value=Mock(start=Mock(side_effect=DriverError("Driver unavailable"))))
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "http"
            with patch.dict(sys.modules, {"reddit_browser": browser_module()}):
                m.collect_reddit_browser({2026}, cache)
            with patch.dict(sys.modules, {"playwright.sync_api": api}):
                try:
                    rows, errors = m.collect_reddit_browser({2026}, cache, refresh=True)
                except DriverError:
                    self.fail("Driver startup must be a source warning, not abort the update")
            self.assertEqual([(r["artist"], r["album"]) for r in rows], [("Browser Act", "Record")])
            self.assertTrue(any("stale" in error for error in errors))

    def test_playwright_cleanup_failure_keeps_successful_import(self):
        from reddit_browser import RedditBrowser
        m = updater()
        class DriverError(Exception):
            pass
        context = Mock(pages=[Mock()], close=Mock(side_effect=DriverError("Browser already closed")))
        driver = Mock(stop=Mock(side_effect=DriverError("Driver already closed")))
        driver.chromium.launch_persistent_context.return_value = context
        api = types.ModuleType("playwright.sync_api")
        api.Error = DriverError
        api.sync_playwright = Mock(return_value=Mock(start=Mock(return_value=driver)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"playwright.sync_api": api}), patch.object(RedditBrowser, "read_wiki", return_value=TABLE):
            try:
                rows, errors = m.collect_reddit_browser({2026}, Path(directory) / "http")
            except DriverError:
                self.fail("Driver cleanup must not discard a successful import")
        self.assertEqual([(r["artist"], r["album"]) for r in rows], [("Browser Act", "Record")])
        self.assertEqual(errors, [])

    def test_browser_source_reuses_existing_parser_and_merge_pipeline(self):
        m = updater()
        class FixtureHTTP:
            offline, refresh = False, False
            def get(self, url):
                if url == "https://browseract.bandcamp.com/album/record":
                    raise ValueError("Uncached linked primary page")
                raise AssertionError(f"Browser mode called HTTP API: {url}")
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"reddit_browser": browser_module()}):
            client = FixtureHTTP()
            client.cache_dir = Path(directory)
            rows, errors = m.collect_sources({"reddit": True, "reddit_mode": "browser", "musicbrainz": False}, {2026}, client, [])
        self.assertEqual(len(m.merge_catalog(rows)), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Bandcamp", errors[0])

    def test_wiki_client_navigation_during_table_extraction_is_retried(self):
        from reddit_browser import RedditBrowser
        class NavigationError(Exception):
            pass
        browser = RedditBrowser(Path("unused-profile"))
        browser._browser_error = NavigationError
        browser.page = Mock(url="https://www.reddit.com/r/postrock/wiki/albums/2026/")
        browser.page.locator.return_value.evaluate_all.side_effect = [
            NavigationError("Execution context was destroyed, most likely because of a navigation"), TABLE,
        ]
        self.assertIn("Browser Act", browser.read_wiki(2026, timeout=5))

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Optional Playwright dependency not installed")
    def test_real_headless_browser_reads_shadow_dom_and_reuses_private_profile(self):
        from reddit_browser import RedditBrowser
        table_only = TABLE
        document = '<script>window.secret="private-session-token"</script><div id="wiki"></div><script>document.querySelector("#wiki").attachShadow({mode:"open"}).innerHTML=' + json.dumps(table_only) + ';</script>'
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            with RedditBrowser(profile, headless=True) as browser:
                requested_agents = []
                def serve_wiki(route):
                    agent = route.request.all_headers()["user-agent"]
                    requested_agents.append(agent)
                    body = '<h1>Prove your humanity</h1>' if "HeadlessChrome/" in agent else document
                    route.fulfill(content_type="text/html", body=body)
                browser.context.route("https://www.reddit.com/r/postrock/wiki/albums/2026/", serve_wiki)
                browser.context.add_cookies([{"name": "session-example", "value": "saved-once", "domain": "www.reddit.com", "path": "/", "expires": 2000000000}])
                html = browser.read_wiki(2026, timeout=5)
                self.assertIn("Browser Act", html)
                self.assertNotIn("private-session-token", html)
                agent = browser.page.evaluate("navigator.userAgent")
                self.assertEqual(requested_agents, [agent])
                self.assertIn("Chrome/" + browser.context.browser.version.split(".")[0] + ".", agent)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            with RedditBrowser(profile, headless=True) as browser:
                cookies = browser.context.cookies("https://www.reddit.com/")
                self.assertTrue(any(c["name"] == "session-example" and c["value"] == "saved-once" for c in cookies))


if __name__ == "__main__":
    unittest.main()
