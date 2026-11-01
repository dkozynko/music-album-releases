import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import HTTPRedirectHandler

from test_update import SCRIPT, observation, updater


SEARCH = "https://api.discogs.com/database/search?type=release&style=Post+Rock&format=Album&year=2026&per_page=100&page={}"


def release(identifier=123, **changes):
    value = {"id": identifier, "title": "The Record", "year": 2026, "released": "2026-10-02",
             "artists": [{"name": "Proof Band (12)", "anv": "", "join": ""}],
             "styles": ["Post Rock"], "formats": [{"name": "CD", "qty": "1", "descriptions": ["Album"]}],
             "master_id": 0, "date_added": "2026-10-20T10:00:00", "date_changed": "2026-10-30T10:00:00"}
    value.update(changes)
    return value


def summary(identifier, master_id=0, **changes):
    value = {"id": identifier, "master_id": master_id, "type": "release", "year": "2026",
             "style": ["Post Rock"], "format": ["CD", "Album"], "title": "Proof Band (12) - The Record"}
    value.update(changes)
    return value


def page(results, current=1, pages=1, items=None):
    total = (100 * (pages - 1) + len(results) if pages > 1 else len(results)) if items is None else items
    return {"pagination": {"page": current, "pages": pages, "per_page": 100, "items": total}, "results": results}


class DiscogsTests(unittest.TestCase):
    def test_master_year_does_not_erase_agreed_month_between_conflicting_days(self):
        m = updater()
        existing = m.merge_catalog([
            observation("Proof Band", "The Record", date="2026-10-02", source_ids=["discogs-master:42"]),
            observation("Proof Band", "The Record", date="2026-10-16"),
        ])
        master = {"id": 42, "year": 2026, "title": "The Record", "artists": [{"name": "Proof Band"}]}
        rows = m.merge_catalog([m.parse_discogs_master(master, 2026)], existing)
        self.assertEqual(rows[0]["release_date"], "2026-10")
        self.assertEqual(rows[0]["date_status"], "conflict")
        self.assertEqual(rows[0]["date_candidates"], ["2026", "2026-10-02", "2026-10-16"])

    def test_malformed_token_fails_without_echoing_secret_in_errors(self):
        m = updater()
        for secret in ("sensitive-test-value\nsecond-line", "sensitive-test-value\t", "sensitive-test-valueЖ"):
            with self.subTest(secret_kind=type(secret).__name__), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"DISCOGS_TOKEN": secret}):
                # Trailing whitespace is harmless after trimming; internal/control or non-ASCII input is invalid.
                if secret.endswith("\t"):
                    client = m.HTTPClient(Path(directory))
                    self.assertEqual(client.discogs_token, "sensitive-test-value")
                else:
                    with self.assertRaises(ValueError) as error:
                        m.HTTPClient(Path(directory))
                    self.assertIn("DISCOGS_TOKEN", str(error.exception))
                    self.assertNotIn("sensitive-test-value", str(error.exception))

    def test_every_requested_year_is_discovered_without_a_hardcoded_current_year(self):
        m = updater()
        responses = {
            SEARCH.format(1).replace("year=2026", "year=2025"): page([summary(21, 41, year="2025")]),
            SEARCH.format(1): page([summary(22, 42)]),
            "https://api.discogs.com/masters/41": {"id": 41, "year": 2025, "title": "Earlier Work", "artists": [{"name": "Proof Band"}]},
            "https://api.discogs.com/masters/42": {"id": 42, "year": 2026, "title": "The Record", "artists": [{"name": "Proof Band"}]},
        }
        class FixtureHTTP:
            def get(self, url):
                return json.dumps(responses[url])
        rows, errors = m.collect_discogs(FixtureHTTP(), {2026, 2025})
        self.assertEqual(errors, [])
        self.assertEqual([(r["year"], r["album"]) for r in rows], [(2025, "Earlier Work"), (2026, "The Record")])

    def test_unauthenticated_requests_use_the_lower_discogs_rate_limit(self):
        m = updater()
        for token, expected_delay in (("", 1.5), ("test-token", 0.1)):
            with self.subTest(authenticated=bool(token)), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"DISCOGS_TOKEN": token}), \
                 patch.object(m, "urlopen", side_effect=lambda *a, **k: io.BytesIO(b'{}')), \
                 patch.object(m.time, "monotonic", side_effect=[100.0, 100.0, 101.0, 101.0]), \
                 patch.object(m.time, "sleep") as sleep:
                client = m.HTTPClient(Path(directory))
                client.get("https://api.discogs.com/releases/1")
                client.get("https://api.discogs.com/releases/2")
                self.assertAlmostEqual(sleep.call_args_list[-1].args[0], expected_delay)

    def test_bad_master_id_does_not_discard_good_releases_or_abort_the_source(self):
        m = updater()
        for invalid in ("not-an-id", -1, {"unexpected": "object"}):
            class FixtureHTTP:
                def get(self, url):
                    if url == SEARCH.format(1):
                        return json.dumps(page([summary(1, invalid), summary(123)]))
                    if url == "https://api.discogs.com/releases/123":
                        return json.dumps(release())
                    raise AssertionError(url)
            with self.subTest(invalid=invalid):
                rows, errors = m.collect_discogs(FixtureHTTP(), {2026})
                self.assertEqual(len(rows), 1)
                self.assertTrue(any("Discogs 2026" in error and "1" in error for error in errors))

    def test_inconsistent_pagination_keeps_usable_rows_and_reports_incomplete_source(self):
        m = updater()
        for bad in (page([summary(123)], pages=0), page([summary(123)], pages=1, items=200)):
            class FixtureHTTP:
                def get(self, url):
                    if url == SEARCH.format(1):
                        return json.dumps(bad)
                    if url == "https://api.discogs.com/releases/123":
                        return json.dumps(release())
                    raise AssertionError(url)
            with self.subTest(pagination=bad["pagination"]):
                rows, errors = m.collect_discogs(FixtureHTTP(), {2026})
                self.assertEqual(len(rows), 1)
                self.assertTrue(any("Discogs 2026" in error and "pagination" in error.lower() for error in errors))

    def test_master_uses_original_year_without_promoting_edition_dates(self):
        m = updater()
        master = {"id": 42, "year": 2026, "title": "The Record", "main_release": 123,
                  "artists": [{"name": "Proof Band (12)", "join": ""}], "styles": ["Post Rock"]}
        row = m.parse_discogs_master(master, 2026, [1, 2])
        self.assertEqual((row["artist"], row["release_date"]), ("Proof Band", "2026"))
        self.assertEqual(row["source_ids"], ["discogs-master:42", "discogs-release:1", "discogs-release:2"])
        self.assertEqual(row["source_urls"], ["https://www.discogs.com/master/42"])
        self.assertIsNone(m.parse_discogs_master(dict(master, year=2011), 2026, [1, 2]))
        with self.assertRaises(ValueError):
            m.parse_discogs_master(dict(master, year=0), 2026, [1, 2])

    def test_masterless_release_reads_stated_partial_date_and_artist_credit(self):
        m = updater()
        data = release(released="2026-10-00", artists=[
            {"name": "Proof Band (12)", "anv": "PROOF BAND", "join": "&"},
            {"name": "Guest (2)", "anv": "", "join": ""},
        ])
        row = m.parse_discogs_release(data, 2026)
        self.assertEqual((row["artist"], row["release_date"]), ("Proof Band & Guest", "2026-10"))
        self.assertEqual(row["source_ids"], ["discogs-release:123"])
        self.assertEqual(row["date_status"], "reported")
        self.assertIsNone(m.parse_discogs_release(release(released="2011-10-02", year=2011), 2026))

    def test_masterless_reissue_repress_and_nonalbums_are_not_new_albums(self):
        m = updater()
        for kind in ("Reissue", "Repress", "Remastered", "EP", "Single", "Compilation", "Unofficial Release", "Demo", "Remix", "Live"):
            with self.subTest(kind=kind):
                self.assertIsNone(m.parse_discogs_release(release(formats=[{"name": "Vinyl", "descriptions": ["Album", kind]}]), 2026))
        self.assertIsNone(m.parse_discogs_release(release(title="Live at the Hall"), 2026))
        self.assertIsNone(m.parse_discogs_release(release(styles=["Post-Punk"]), 2026))

    def test_various_artist_compilation_is_excluded_even_without_format_annotation(self):
        m = updater()
        data = release(artists=[{"name": "Various", "join": ""}])
        self.assertIsNone(m.parse_discogs_release(data, 2026))
        self.assertIsNone(m.parse_discogs_master(dict(data, id=42), 2026, [123]))

    def test_release_search_paginates_keeps_masterless_and_resolves_stale_master_ids(self):
        m = updater()
        master = {"id": 42, "year": 2026, "title": "The Record", "artists": [{"name": "Proof Band (12)"}]}
        responses = {
            SEARCH.format(1): page([summary(1, 42), summary(2, 42, format=["LP", "Album", "Reissue"]),
                                   summary(3), summary(4, style=["Post-Punk"]), summary(5, format=["12\"", "Single"])], pages=2),
            SEARCH.format(2): page([summary(1, 42), summary(6, 99), summary(7)], current=2, pages=2),
            "https://api.discogs.com/releases/3": release(3, title="Standalone Work"),
            "https://api.discogs.com/releases/7": release(7, master_id=99),
            "https://api.discogs.com/masters/42": master,
            "https://api.discogs.com/masters/99": dict(master, id=99, year=2011),
        }
        class FixtureHTTP:
            def get(self, url):
                if url not in responses:
                    raise AssertionError(f"Unexpected URL: {url}")
                return json.dumps(responses[url])
        rows, errors = m.collect_discogs(FixtureHTTP(), {2026})
        self.assertEqual(errors, [])
        merged = m.merge_catalog(rows)
        self.assertEqual([(r["album"], r["release_date"]) for r in merged], [("The Record", "2026"), ("Standalone Work", "2026-10-02")])
        master_row = next(r for r in merged if r["album"] == "The Record")
        self.assertIn("discogs-release:2", master_row["source_ids"])
        self.assertFalse(any("discogs-master:0" in r["source_ids"] for r in merged))

    def test_failed_or_repeated_page_reports_error_but_keeps_collected_albums(self):
        m = updater()
        for failure in (ValueError("HTTP 503"), json.dumps(page([summary(3)], current=1, pages=2))):
            class PartialHTTP:
                def get(self, url):
                    if url == SEARCH.format(1):
                        return json.dumps(page([summary(3)], pages=2))
                    if url == SEARCH.format(2):
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    if url == "https://api.discogs.com/releases/3":
                        return json.dumps(release(3))
                    raise AssertionError(url)
            with self.subTest(failure=failure):
                rows, errors = m.collect_discogs(PartialHTTP(), {2026})
                self.assertEqual(len(rows), 1)
                self.assertTrue(any("Discogs 2026" in error for error in errors))

    def test_failed_discogs_source_preserves_existing_catalog(self):
        m = updater()
        class BlockedHTTP:
            def get(self, url):
                raise ValueError("HTTP 401")
        existing = m.merge_catalog([observation(source_urls=["https://www.discogs.com/release/123"])])
        rows, errors = m.collect_sources({"discogs": True, "musicbrainz": False}, {2026}, BlockedHTTP(), [], existing)
        self.assertTrue(any("Discogs 2026" in error and "401" in error for error in errors))
        self.assertEqual(m.merge_catalog(rows, existing), existing)

    def test_token_is_host_restricted_not_cached_and_not_forwarded(self):
        m = updater()
        requests = []
        def response(request, timeout):
            requests.append(request)
            return io.BytesIO(b'{}')
        secret = "discogs-test-token-never-save"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DISCOGS_TOKEN": secret}), \
             patch.object(m, "urlopen", side_effect=response), patch.object(m.time, "sleep"):
            client = m.HTTPClient(Path(directory))
            client.get("https://api.discogs.com/releases/123")
            client.get("https://musicbrainz.org/ws/2/release-group/example")
            self.assertEqual(requests[0].get_header("Authorization"), "Discogs token=" + secret)
            self.assertIsNone(requests[1].get_header("Authorization"))
            target = HTTPRedirectHandler().redirect_request(requests[0], None, 302, "Found", {}, "https://example.com/")
            self.assertIsNone(target.get_header("Authorization"))
            self.assertTrue(all(secret not in p.read_text() for p in Path(directory).glob("*.json")))

    def test_cli_updates_from_discogs_cache_and_preserves_rows_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "seeds.json").write_text("[]")
            config = root / "sources.json"
            config.write_text(json.dumps({"discogs": True, "musicbrainz": False}))
            cache = root / "cache"
            cache.mkdir()
            def save(url, payload):
                path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
                path.write_text(json.dumps({"url": url, "body": json.dumps(payload)}))
                return path
            search_file = save(SEARCH.format(1), page([summary(123)]))
            save("https://api.discogs.com/releases/123", release())
            command = [sys.executable, str(SCRIPT), "--year", "2026", "--config", str(config),
                       "--cache-dir", str(cache), "--offline", "--output-dir", str(root / "out")]
            initial = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            path = root / "out/2026.csv"
            with path.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([(r["artist"], r["album"]) for r in rows], [("Proof Band", "The Record")])
            snapshot = {p.name: p.read_bytes() for p in (root / "out").iterdir()}
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual({p.name: p.read_bytes() for p in (root / "out").iterdir()}, snapshot)
            search_file.unlink()
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("Discogs 2026", failed.stderr)
            self.assertEqual({p.name: p.read_bytes() for p in (root / "out").iterdir()}, snapshot)


if __name__ == "__main__":
    unittest.main()
