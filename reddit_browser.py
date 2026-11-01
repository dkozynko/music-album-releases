"""Optional Reddit wiki reader using a separate persistent Chrome session."""

import os
from pathlib import Path
import time
from urllib.parse import urlsplit


RELEASE_TABLES = """tables => tables.filter(table => {
    const row = table.querySelector('tr');
    const cells = row ? [...row.cells].map(cell =>
        cell.textContent.toLowerCase().replace(/[^a-z0-9]/g, '')) : [];
    return cells[0] === 'releasedate' && ['band', 'artist'].includes(cells[1]) &&
        cells.length >= 3 && cells[2].startsWith('album');
}).map(table => table.outerHTML).join('\\n')"""


class RedditBrowser:
    def __init__(self, profile_dir, headless=True):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.context = self.driver = self.page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright, Error
        except ImportError as error:
            raise ValueError("Reddit browser mode needs Playwright: install requirements-browser.txt into .venv") from error
        self._browser_error = Error
        self.profile_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.profile_dir.chmod(0o700)
        try:
            self.driver = sync_playwright().start()
            channel = os.environ.get("REDDIT_BROWSER_CHANNEL", "chrome")
            options = {"headless": self.headless, "channel": channel, "locale": "en-US"}
            if self.headless:
                # Use this Chrome's real platform/version, without the headless-mode token.
                # Discover it on about:blank before creating the persistent context.
                probe = self.driver.chromium.launch(headless=True, channel=channel)
                try:
                    agent = probe.new_page().evaluate("navigator.userAgent")
                    options["user_agent"] = agent.replace("HeadlessChrome/", "Chrome/")
                finally:
                    probe.close()
            self.context = self.driver.chromium.launch_persistent_context(
                str(self.profile_dir.resolve()), accept_downloads=False, **options,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except Error as error:
            self.__exit__()
            raise ValueError("Cannot start Reddit browser: check Chrome installation and close other windows using its dedicated profile") from error
        return self

    def read_wiki(self, year, timeout=45):
        path = f"/r/postrock/wiki/albums/{year}"
        deadline = time.monotonic() + timeout
        try:
            self.page.goto("https://www.reddit.com" + path + "/", wait_until="domcontentloaded", timeout=timeout * 1000)
            while time.monotonic() < deadline:
                current = urlsplit(self.page.url)
                if current.hostname in {"www.reddit.com", "reddit.com", "old.reddit.com"} and current.path.rstrip("/") == path:
                    try:
                        document = self.page.locator("table").evaluate_all(RELEASE_TABLES)
                    except self._browser_error as error:
                        if "Execution context was destroyed" not in str(error):
                            raise
                        # Reddit can navigate again after the initial DOM is ready.
                        document = ""
                    if document:
                        if len(document.encode("utf-8")) > 10_000_000:
                            raise ValueError("Reddit wiki tables exceed 10 MB")
                        return document
                self.page.wait_for_timeout(500)
        except self._browser_error as error:
            raise ValueError("Reddit browser navigation failed or the window was closed; retry the update") from error
        raise ValueError("Reddit did not expose release tables (access challenge or changed markup); retry the update later")

    def __exit__(self, *args):
        try:
            if self.context:
                try:
                    self.context.close()
                except self._browser_error:
                    pass
        finally:
            if self.driver:
                try:
                    self.driver.stop()
                except self._browser_error:
                    pass
