import csv
import html
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "update.py"


def updater():
    if not SCRIPT.exists():
        raise AssertionError("The catalog updater has not been implemented")
    spec = importlib.util.spec_from_file_location("album_updater", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(artist="Example Act", album="New Album", date="2026-10-16", **extra):
    row = {
        "artist": artist,
        "album": album,
        "year": 2026,
        "release_date": date,
        "date_status": "reported",
        "release_type": "album",
        "source_ids": [],
        "source_urls": ["https://example.bandcamp.com/album/new-album"],
        "notes": "",
    }
    row.update(extra)
    return row


def bandcamp_document(release_date):
    payload = {
        "artist": "Example Act", "album_release_date": release_date,
        "current": {"id": 123, "type": "album", "title": "New Album", "artist": None,
                    "release_date": release_date, "publish_date": "01 Jun 2026 00:00:00 GMT"},
    }
    return '<script data-tralbum="' + html.escape(json.dumps(payload), quote=True) + '"></script><a class="tag">post-rock</a>'


class CatalogTests(unittest.TestCase):
    def test_same_album_versions_merge_without_a_master(self):
        m = updater()
        rows = m.merge_catalog([
            observation("Shy, Low", "Purity Spiral", source_ids=["discogs-release:11"]),
            observation("SHY LOW", "Purity Spiral", source_ids=["discogs-release:12"]),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_ids"], ["discogs-release:11", "discogs-release:12"])

    def test_zero_master_id_does_not_merge_unrelated_albums(self):
        m = updater()
        rows = m.merge_catalog([
            observation("One", "First", source_ids=["discogs-master:0"]),
            observation("Two", "Second", source_ids=["discogs-master:0"]),
        ])
        self.assertEqual(len(rows), 2)

    def test_shared_native_id_merges_changed_artist_credit(self):
        m = updater()
        rows = m.merge_catalog([
            observation("Old Credit", source_ids=["musicbrainz:abc"]),
            observation("New Credit", source_ids=["musicbrainz:abc"]),
        ])
        self.assertEqual(len(rows), 1)

    def test_self_titled_alias_merges_with_named_album(self):
        m = updater()
        rows = m.merge_catalog([
            observation("Caspian", "S/T"), observation("Caspian", "Caspian")
        ])
        self.assertEqual(len(rows), 1)

    def test_explicit_alias_merges_translated_title(self):
        m = updater()
        rows = m.merge_catalog([
            observation("NokENok", "My Clothes Have No Smell"),
            observation("NokENok", "내 옷은 냄새가 없다."),
        ], corrections=[{
            "artist": "NokENok", "album": "내 옷은 냄새가 없다.",
            "canonical_artist": "NokENok", "canonical_album": "My Clothes Have No Smell",
        }])
        self.assertEqual(len(rows), 1)

    def test_unknown_date_components_are_not_invented(self):
        m = updater()
        self.assertEqual(m.normalize_date("2026-10-00"), "2026-10")
        self.assertEqual(m.normalize_date("2026-00-00"), "2026")
        self.assertEqual(m.normalize_date("2026"), "2026")
        with self.assertRaises(ValueError):
            m.normalize_date("2026-02-30")

    def test_compatible_dates_keep_known_precision(self):
        m = updater()
        rows = m.merge_catalog([
            observation(date="2026"),
            observation(date="2026-10"),
            observation(date="2026-10-16", date_status="primary"),
        ])
        self.assertEqual(rows[0]["release_date"], "2026-10-16")
        self.assertEqual(rows[0]["date_status"], "primary")

    def test_conflicting_dates_keep_only_agreed_month(self):
        m = updater()
        rows = m.merge_catalog([
            observation(date="2026-10-02", date_status="primary"),
            observation(date="2026-10-30", date_status="primary"),
        ])
        self.assertEqual(rows[0]["release_date"], "2026-10")
        self.assertEqual(rows[0]["date_status"], "conflict")
        self.assertEqual(rows[0]["date_candidates"], ["2026-10-02", "2026-10-30"])

    def test_later_primary_page_does_not_hide_earlier_catalog_year(self):
        m = updater()
        rows = m.merge_catalog([
            observation(date="2011-10-26"),
            observation(date="2026-10-01", date_status="primary"),
        ])
        self.assertEqual(rows[0]["release_date"], "")
        self.assertEqual(rows[0]["date_status"], "conflict")

    def test_sourced_correction_pins_original_across_refreshes(self):
        m = updater()
        correction = {
            "artist": "Example Act", "album": "New Album",
            "release_date": "2011-10-26",
            "source_url": "https://example.bandcamp.com/album/original",
            "note": "2026 is a repress, not the original release.",
        }
        old = m.merge_catalog([observation()], corrections=[correction])
        refreshed = m.merge_catalog([observation(date="2026-11-01")], old, [correction])
        self.assertEqual(refreshed[0]["release_date"], "2011-10-26")
        self.assertEqual(refreshed[0]["year"], 2011)
        self.assertEqual(refreshed[0]["date_status"], "corrected")

    def test_correction_can_exclude_an_unmarked_ep(self):
        m = updater()
        rows = m.merge_catalog([observation()], corrections=[{
            "artist": "Example Act", "album": "New Album", "release_type": "ep"
        }])
        self.assertEqual(rows, [])

    def test_missing_feed_does_not_delete_existing_album(self):
        m = updater()
        existing = m.merge_catalog([observation("Earlier", "Kept")])
        refreshed = m.merge_catalog([observation("Later", "Added")], existing)
        self.assertEqual({r["artist"] for r in refreshed}, {"Earlier", "Later"})

    def test_conflict_evidence_survives_a_partial_refresh(self):
        m = updater()
        existing = m.merge_catalog([
            observation(date="2026-10-02"), observation(date="2026-10-30")
        ])
        refreshed = m.merge_catalog([observation(date="2026-10-02")], existing)
        self.assertEqual(refreshed[0]["date_status"], "conflict")
        self.assertEqual(refreshed[0]["date_candidates"], ["2026-10-02", "2026-10-30"])

    def test_musicbrainz_uses_release_groups_and_excludes_live_albums(self):
        m = updater()
        payload = {"count": 2, "offset": 0, "release-groups": [
            {"id": "first", "title": "Studio", "primary-type": "Album",
             "first-release-date": "2026-10", "secondary-types": [],
             "artist-credit": [{"name": "A", "artist": {"id": "a", "name": "A"}}],
             "tags": [{"count": 1, "name": "post-rock"}],
             "releases": [{"id": "edition-one", "status": "Official"},
                          {"id": "edition-two", "status": "Official"}]},
            {"id": "second", "title": "Concert", "primary-type": "Album",
             "first-release-date": "2026-10-16", "secondary-types": ["Live"],
             "artist-credit": [{"name": "A", "artist": {"id": "a", "name": "A"}}],
             "tags": [{"count": 1, "name": "post-rock"}]},
        ]}
        rows = m.merge_catalog(m.parse_musicbrainz(payload))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_ids"], ["musicbrainz:first"])
        self.assertEqual(rows[0]["release_date"], "2026-10")

    def test_successful_musicbrainz_reclassification_removes_saved_album(self):
        m = updater()
        group = {"id": "reclassified", "title": "New Album", "primary-type": "Album",
                 "first-release-date": "2026-10-16", "secondary-types": [],
                 "artist-credit": [{"name": "Example Act", "artist": {"name": "Example Act"}}],
                 "tags": [{"count": 1, "name": "post-rock"}]}
        existing = m.merge_catalog(m.parse_musicbrainz({"release-groups": [group]}))
        for kind in ["Live", "Compilation"]:
            with self.subTest(kind=kind):
                changed = dict(group, **{"secondary-types": [kind]})
                fresh = m.parse_musicbrainz({"release-groups": [changed]})
                self.assertEqual(m.merge_catalog(fresh, existing), [])

    def test_csv_id_is_rejected_as_an_unstable_correction_match(self):
        m = updater()
        existing = m.merge_catalog([observation()])
        with self.assertRaisesRegex(ValueError, "source_id|artist"):
            m.merge_catalog([observation()], existing, [{"id": existing[0]["id"], "exclude": True}])

    def test_canonical_name_remains_authoritative_across_native_id_aliases(self):
        m = updater()
        existing = m.merge_catalog([
            observation(source_ids=["musicbrainz:abc"]),
            observation(source_ids=["bandcamp:123"]),
        ])
        correction = {"source_id": "musicbrainz:abc", "canonical_artist": "Z Canonical Artist",
                      "canonical_album": "Z Canonical Album"}
        rows = m.merge_catalog([observation(source_ids=["bandcamp:123"])], existing, [correction])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["artist"], rows[0]["album"]), ("Z Canonical Artist", "Z Canonical Album"))
        again = m.merge_catalog([observation("Another credit", source_ids=["bandcamp:123"])], rows, [correction])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["artist"], "Z Canonical Artist")

    def test_date_correction_preserves_the_replaced_source_claim(self):
        m = updater()
        rows = m.merge_catalog([observation()], corrections=[{
            "artist": "Example Act", "album": "New Album", "release_date": "2011-10-26",
            "source_url": "https://example.bandcamp.com/album/original",
        }])
        self.assertEqual(rows[0]["release_date"], "2011-10-26")
        self.assertEqual(rows[0]["date_candidates"], ["2011-10-26", "2026-10-16"])

    def test_unknown_release_type_fails_instead_of_becoming_an_album(self):
        m = updater()
        with self.assertRaises(ValueError):
            m.merge_catalog([observation(release_type="not-a-type")])

    def test_selected_bandcamp_original_year_is_not_discarded(self):
        m = updater()
        class FixtureHTTP:
            def get(self, url):
                return bandcamp_document("26 Oct 2011 00:00:00 GMT")
        rows, errors = m.collect_sources({"musicbrainz": False}, {2026}, FixtureHTTP(), [observation()])
        merged = m.merge_catalog(rows)
        self.assertEqual(errors, [])
        self.assertEqual(merged[0]["date_status"], "conflict")
        self.assertEqual(merged[0]["date_candidates"], ["2011-10-26", "2026-10-16"])

    def test_bandcamp_uses_album_release_not_upload_or_shipping_date(self):
        m = updater()
        data = {
            "id": 123, "artist": "Example Act", "item_type": "album",
            "url": "https://example.bandcamp.com/album/new-album",
            "album_release_date": "16 Oct 2026 00:00:00 GMT",
            "current": {"id": 123, "type": "album", "artist": None,
                        "title": "New Album", "release_date": "16 Oct 2026 00:00:00 GMT",
                        "publish_date": "01 Jun 2026 00:00:00 GMT", "about": "A new full-length album.",
                        "credits": "Recorded in 2025."},
            "packages": [{"release_date": "01 Dec 2026 00:00:00 GMT"}],
            "trackinfo": [{"track_num": 1, "title": "First", "duration": 600}],
        }
        document = '<script data-tralbum="' + html.escape(json.dumps(data), quote=True) + '"></script>'
        document += '<a class="tag" href="https://bandcamp.com/tag/post-rock">post-rock</a>'
        row = m.parse_bandcamp_album(document, data["url"])
        self.assertEqual(row["release_date"], "2026-10-16")
        self.assertEqual(row["date_status"], "primary")
        self.assertEqual(row["source_ids"], ["bandcamp:123"])
        self.assertIn("post-rock", row["tags"])

    def test_bandcamp_catalog_follows_label_artist_album_links_only(self):
        m = updater()
        items = [
            {"id": 1, "type": "album", "artist": "A", "title": "One",
             "page_url": "https://artist.bandcamp.com/album/one?label=123&tab=music"},
            {"id": 2, "type": "track", "artist": "A", "title": "Single",
             "page_url": "https://artist.bandcamp.com/track/single"},
        ]
        document = '<ol id="music-grid" data-client-items="' + html.escape(json.dumps(items), quote=True) + '">'
        document += '<a href="/album/second">Second</a><a href="https://example.com/album/foreign">No</a></ol>'
        urls = m.parse_bandcamp_catalog(document, "https://label.bandcamp.com/music")
        self.assertEqual(urls, ["https://artist.bandcamp.com/album/one", "https://label.bandcamp.com/album/second"])

    def test_bandcamp_without_release_date_does_not_invent_current_year(self):
        m = updater()
        data = {
            "artist": "Example", "album_release_date": None,
            "current": {"id": 124, "type": "album", "title": "Undated",
                        "release_date": None, "publish_date": "01 Jun 2026 00:00:00 GMT"},
        }
        document = '<script data-tralbum="' + html.escape(json.dumps(data), quote=True) + '"></script>'
        row = m.parse_bandcamp_album(document, "https://example.bandcamp.com/album/undated")
        self.assertIsNone(row["year"])
        self.assertEqual(row["release_date"], "")

    def test_single_album_catalog_uses_own_page_and_ignores_recommendations(self):
        m = updater()
        data = {"url": "https://example.bandcamp.com/album/only-album",
                "current": {"id": 123, "type": "album", "title": "Only album"}}
        document = '<script data-tralbum="' + html.escape(json.dumps(data), quote=True) + '"></script>'
        document += '<a href="https://someoneelse.bandcamp.com/album/recommended">Recommendation</a>'
        self.assertEqual(m.parse_bandcamp_catalog(document, "https://example.bandcamp.com/music"),
                         ["https://example.bandcamp.com/album/only-album"])

    def test_challenge_page_is_an_error_not_an_empty_catalog(self):
        m = updater()
        with self.assertRaises(ValueError):
            m.parse_bandcamp_catalog("<h1>Please enable JavaScript</h1>", "https://label.bandcamp.com/music")

    def test_markdown_escapes_names_and_preserves_links(self):
        m = updater()
        rows = m.merge_catalog([observation(
            "A | B", "<Unusual>", date="2026-10",
            source_urls=["https://example.bandcamp.com/album/a(b)"],
        )])
        md = m.render_markdown(2026, rows)
        self.assertIn("A \\| B", md)
        self.assertIn("&lt;Unusual&gt;", md)
        self.assertIn("2026-10", md)
        self.assertNotIn("2026-10-01", md)
        self.assertIn("https://example.bandcamp.com/album/a%28b%29", md)


class CommandTests(unittest.TestCase):
    def run_update(self, folder, source):
        if not SCRIPT.exists():
            raise AssertionError("The catalog updater has not been implemented")
        return subprocess.run([
            sys.executable, str(SCRIPT), "--year", "2026", "--input", str(source),
            "--output-dir", str(folder / "out"),
        ], text=True, capture_output=True)

    def test_update_round_trip_is_idempotent_and_preserves_previous_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "input.json"
            source.write_text(json.dumps([
                observation("A, B", "Тихий альбом"), observation("Second", "Other")
            ]), encoding="utf-8")
            first = self.run_update(folder, source)
            self.assertEqual(first.returncode, 0, first.stderr)
            csv_path, md_path = folder / "out/2026.csv", folder / "out/2026.md"
            saved = (csv_path.read_bytes(), md_path.read_bytes())
            again = self.run_update(folder, source)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(saved, (csv_path.read_bytes(), md_path.read_bytes()))
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["album"] for r in rows}, {"Тихий альбом", "Other"})
            source.write_text("[]", encoding="utf-8")
            partial = self.run_update(folder, source)
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertEqual(saved, (csv_path.read_bytes(), md_path.read_bytes()))

    def test_bad_input_does_not_overwrite_good_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "input.json"
            source.write_text(json.dumps([observation()]), encoding="utf-8")
            first = self.run_update(folder, source)
            self.assertEqual(first.returncode, 0, first.stderr)
            paths = (folder / "out/2026.csv", folder / "out/2026.md")
            saved = [p.read_bytes() for p in paths]
            source.write_text('{"unexpected": "schema"}', encoding="utf-8")
            bad = self.run_update(folder, source)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(saved, [p.read_bytes() for p in paths])

    def test_missing_corrections_file_aborts_before_overwriting_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "input.json"
            source.write_text(json.dumps([observation()]), encoding="utf-8")
            first = self.run_update(folder, source)
            self.assertEqual(first.returncode, 0, first.stderr)
            paths = (folder / "out/2026.csv", folder / "out/2026.md")
            saved = [p.read_bytes() for p in paths]
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--year", "2026", "--input", str(source),
                "--output-dir", str(folder / "out"), "--corrections", str(folder / "missing.json"),
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(saved, [p.read_bytes() for p in paths])

    def test_existing_album_refreshes_linked_primary_date_outside_requested_year(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "input.json"
            source.write_text(json.dumps([observation()]), encoding="utf-8")
            first = self.run_update(folder, source)
            self.assertEqual(first.returncode, 0, first.stderr)
            (folder / "seed.json").write_text("[]", encoding="utf-8")
            config = folder / "config.json"
            config.write_text(json.dumps({"musicbrainz": False, "seed_file": "seed.json"}), encoding="utf-8")
            cache = folder / "cache"
            cache.mkdir()
            url = "https://example.bandcamp.com/album/new-album"
            (cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")).write_text(json.dumps({
                "url": url, "body": bandcamp_document("26 Oct 2011 00:00:00 GMT")
            }), encoding="utf-8")
            refreshed = subprocess.run([
                sys.executable, str(SCRIPT), "--year", "2026", "--config", str(config),
                "--cache-dir", str(cache), "--offline", "--output-dir", str(folder / "out"),
            ], capture_output=True, text=True)
            self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
            with (folder / "out/2026.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["date_status"], "conflict")
            self.assertEqual(rows[0]["release_date"], "")


class FetchTests(unittest.TestCase):
    def test_bandcamp_public_transport_populates_reusable_cache(self):
        m = updater()
        body = '<html><script data-tralbum="{}"></script></html>'
        result = subprocess.CompletedProcess([], 0, stdout=body + "\n200", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            client = m.HTTPClient(Path(directory))
            with patch.object(shutil, "which", return_value="/usr/bin/curl"), \
                 patch.object(subprocess, "run", return_value=result), \
                 patch.object(m, "urlopen", side_effect=ValueError("Python transport returned a challenge")):
                try:
                    fetched = client.get("https://example.bandcamp.com/album/test")
                except ValueError:
                    fetched = None
            self.assertEqual(fetched, body)
            offline = m.HTTPClient(Path(directory), offline=True)
            self.assertEqual(offline.get("https://example.bandcamp.com/album/test"), body)

    def test_challenge_response_is_not_cached_as_source_data(self):
        m = updater()
        body = "<html><title>Client Challenge</title><p>Enable JavaScript</p></html>"
        result = subprocess.CompletedProcess([], 0, stdout=body + "\n200", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(shutil, "which", return_value="/usr/bin/curl"), \
                 patch.object(subprocess, "run", return_value=result), \
                 patch.object(m, "urlopen", return_value=io.BytesIO(body.encode())):
                with self.assertRaises(ValueError):
                    m.HTTPClient(Path(directory)).get("https://example.bandcamp.com/album/test")
            self.assertEqual(list(Path(directory).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
