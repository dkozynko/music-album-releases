import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_discogs import SEARCH, page, release, summary
from test_update import SCRIPT, bandcamp_document, observation, updater


class StyleTests(unittest.TestCase):
    def test_existing_album_keeps_cached_styles_even_when_discovery_does_not_return_it(self):
        m = updater()
        rows = m.merge_catalog([observation(styles=["Ambient"], source_ids=["discogs-release:123"])])
        before = {k: v for k, v in rows[0].items() if k != "styles"}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            url = "https://api.discogs.com/releases/123"
            path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            payload = release(styles=["Post Rock", "Drone"], formats=[{"name": "Vinyl", "descriptions": ["LP"]}])
            path.write_text(json.dumps({"url": url, "body": json.dumps(payload)}))
            errors = m.enrich_cached_discogs_styles(rows, cache)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["styles"], ["Ambient", "Drone", "Post Rock"])
        self.assertEqual({k: v for k, v in rows[0].items() if k != "styles"}, before)

    def test_styles_merge_all_sources_and_survive_partial_refresh(self):
        m = updater()
        rows = m.merge_catalog([
            observation(styles='["Ambient","Post Rock"]'),
            observation(styles=["post-rock", "Drone", " ambient ", ""]),
        ])
        self.assertEqual(rows[0]["styles"], ["Ambient", "Drone", "Post Rock"])
        self.assertEqual(m.merge_catalog([], rows)[0]["styles"], ["Ambient", "Drone", "Post Rock"])

    def test_invalid_style_values_fail_instead_of_silently_losing_tags(self):
        m = updater()
        for value in ("Ambient", [1], {"name": "Ambient"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.merge_catalog([observation(styles=value)])

    def test_discogs_keeps_every_style_not_only_post_rock(self):
        m = updater()
        payload = release(styles=["Post Rock", "Ambient", "Experimental"])
        row = m.parse_discogs_release(payload, 2026)
        self.assertEqual(row["styles"], ["Ambient", "Experimental", "Post Rock"])
        master = {"id": 42, "year": 2026, "title": "The Record", "artists": [{"name": "Proof Band"}],
                  "styles": ["Post Rock", "Experimental"]}
        class FixtureHTTP:
            def get(self, url):
                if url == SEARCH.format(1):
                    return json.dumps(page([summary(123, 42, style=["Post Rock", "Ambient"])]))
                if url == "https://api.discogs.com/masters/42":
                    return json.dumps(master)
                raise AssertionError(url)
        rows, errors = m.collect_discogs(FixtureHTTP(), {2026})
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["styles"], ["Ambient", "Experimental", "Post Rock"])

    def test_musicbrainz_and_bandcamp_keep_all_their_tags(self):
        m = updater()
        payload = {"release-groups": [{
            "id": "example", "title": "New Album", "primary-type": "Album", "secondary-types": [],
            "first-release-date": "2026-10-16", "artist-credit": [{"name": "Example Act"}],
            "tags": [{"name": "post-rock", "count": 3}, {"name": "ambient", "count": 1}, {"name": "post-metal", "count": 2}],
        }]}
        self.assertEqual(m.parse_musicbrainz(payload)[0]["styles"], ["ambient", "post-metal", "post-rock"])
        document = bandcamp_document("16 Oct 2026 00:00:00 GMT")
        document += '<a class="tag">ambient</a><a class="tag">post-metal</a>'
        self.assertEqual(m.parse_bandcamp_album(document, "https://example.bandcamp.com/album/new-album")["styles"],
                         ["ambient", "post-metal", "post-rock"])

    def test_stale_master_association_keeps_styles_from_already_fetched_release(self):
        m = updater()
        master = {"id": 42, "year": 2026, "title": "The Record", "artists": [{"name": "Proof Band"}], "styles": ["Post Rock"]}
        class FixtureHTTP:
            def get(self, url):
                if url == SEARCH.format(1):
                    return json.dumps(page([summary(123)]))
                if url == "https://api.discogs.com/releases/123":
                    return json.dumps(release(master_id=42, styles=["Post Rock", "Drone"]))
                if url == "https://api.discogs.com/masters/42":
                    return json.dumps(master)
                raise AssertionError(url)
        rows, errors = m.collect_discogs(FixtureHTTP(), {2026})
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["styles"], ["Drone", "Post Rock"])

    def test_markdown_has_escaped_styles_column_and_unknown_placeholder(self):
        m = updater()
        rows = m.merge_catalog([observation(styles=["Ambient | Drone", "Post Rock", "<experimental>"])])
        markdown = m.render_markdown(2026, rows)
        self.assertIn("<th nowrap>Styles</th><th nowrap>Last.fm artist tags</th>", markdown)
        self.assertIn("&lt;experimental&gt;, Ambient | Drone, Post Rock", markdown)
        album_lines = [line for line in markdown.splitlines() if line.startswith("<tr>")]
        self.assertEqual(len(album_lines), 1)
        self.assertEqual(album_lines[0].count("<td nowrap>"), 8)
        unknown = m.render_markdown(2026, m.merge_catalog([observation()]))
        self.assertIn("<td nowrap>—</td>", unknown)

    def test_legacy_csv_is_read_and_migrated_without_losing_saved_album(self):
        m = updater()
        old_fields = ["id", "year", "artist", "album", "release_date", "date_status", "release_type",
                      "date_candidates", "source_ids", "source_urls", "notes"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "2026.csv"
            row = observation(id="saved-album-id", date_candidates=["2026-10-16"])
            with csv_path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=old_fields)
                writer.writeheader()
                writer.writerow({k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()})
            self.assertEqual(m.read_catalog(root)[0]["styles"], [])
            incoming = root / "incoming.json"
            incoming.write_text("[]")
            result = subprocess.run([sys.executable, str(SCRIPT), "--year", "2026", "--input", str(incoming),
                                     "--output-dir", str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with csv_path.open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertIn("styles", reader.fieldnames)
                saved = list(reader)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["id"], "saved-album-id")
            self.assertEqual(json.loads(saved[0]["styles"]), [])
            self.assertEqual(saved[0]["release_date"], "2026-10-16")


if __name__ == "__main__":
    unittest.main()
