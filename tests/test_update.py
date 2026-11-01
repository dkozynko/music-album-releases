import csv
import html
import hashlib
import importlib.util
import io
import json
import os
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
    def test_same_bandcamp_album_link_merges_wiki_title_alias_without_merging_wiki_page(self):
        m = updater()
        url = "https://label.bandcamp.com/album/the-record"
        wiki = "https://www.reddit.com/r/postrock/wiki/albums/2026/"
        source = wiki_payload('<tr><td>Oct 16</td><td>Example Act</td><td><a href="' + url + '">Translated Title</a></td></tr>'
                              '<tr><td>Oct 16</td><td>Other Act</td><td>Different Work</td></tr>')
        rows = m.merge_catalog(m.parse_reddit(source, 2026) + [
            m.parse_bandcamp_album(bandcamp_document("16 Oct 2026 00:00:00 GMT"), url + "?from=wiki"),
        ])
        self.assertEqual(len(rows), 2)
        merged = next(r for r in rows if r["artist"] == "Example Act")
        self.assertIn("bandcamp:123", merged["source_ids"])
        self.assertIn(wiki, merged["source_urls"])

    def test_shared_bandcamp_reference_does_not_merge_two_distinct_works(self):
        m = updater()
        url = "https://example.bandcamp.com/album/two-companion-albums"
        rows = m.merge_catalog([
            observation("Act", "Work One", source_urls=[url]),
            observation("Act", "Work Two", source_urls=[url]),
        ])
        self.assertEqual(len(rows), 2)

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
        self.assertIn("bandcamp:123", row["source_ids"])
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


def wiki_payload(table_rows, year=2026):
    return {"kind": "wikipage", "data": {
        "content_md": f"# {year} Album Releases",
        "content_html": f'<div class="md"><h1>{year} Album Releases</h1><table><thead><tr>'
                        '<th>Release date</th><th>Band</th><th>Album (Bandcamp)</th><th>Hails from</th>'
                        '</tr></thead><tbody>' + table_rows + '</tbody></table></div>',
        "revision_id": "example-revision", "revision_date": 1793440000,
    }}


class RedditTests(unittest.TestCase):
    def test_app_credentials_obtain_fresh_token_without_storing_it(self):
        m = updater()
        requests = []
        def token_response(request, timeout):
            requests.append(request)
            return io.BytesIO(b'{"access_token":"fresh-test-token","token_type":"bearer","expires_in":3600,"scope":"wikiread"}')
        with patch.dict(os.environ, {"REDDIT_ACCESS_TOKEN": "", "REDDIT_CLIENT_ID": "test-client", "REDDIT_CLIENT_SECRET": "test-secret"}), \
             patch.object(m, "urlopen", side_effect=token_response):
            self.assertEqual(m.reddit_access_token(), "fresh-test-token")
        self.assertEqual(requests[0].full_url, "https://www.reddit.com/api/v1/access_token")
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(requests[0].data, b"grant_type=client_credentials&scope=wikiread")
        self.assertEqual(requests[0].get_header("Authorization"), "Basic dGVzdC1jbGllbnQ6dGVzdC1zZWNyZXQ=")
        self.assertNotIn("Authorization", requests[0].headers)

    def test_offline_with_app_credentials_does_not_fetch_token(self):
        m = updater()
        with patch.dict(os.environ, {"REDDIT_ACCESS_TOKEN": "", "REDDIT_CLIENT_ID": "test-client", "REDDIT_CLIENT_SECRET": "test-secret"}), \
             patch.object(m, "urlopen", side_effect=AssertionError("Offline run made a request")):
            self.assertEqual(m.reddit_access_token(offline=True), "")

    def test_full_wiki_table_reads_links_nested_text_and_partial_dates(self):
        m = updater()
        rows = m.parse_reddit(wiki_payload('''
          <tr><td>Jan 7</td><td><strong>A &amp; B</strong></td><td><a href="https://ab.bandcamp.com/album/one"><em>One | Two</em></a></td><td>Kyiv</td></tr>
          <tr><td>Late October</td><td>Second</td><td>Unlinked Album</td><td></td></tr>
          <tr><td>Fall 2026</td><td>Third</td><td>Planned Album</td><td></td></tr>
          <tr><td>2026-12-04</td><td>Fourth</td><td><a href="https://fourth.bandcamp.com/album/four">Four</a></td><td></td></tr>
        '''), 2026)
        self.assertEqual([(r["artist"], r["album"], r["release_date"]) for r in rows], [
            ("A & B", "One | Two", "2026-01-07"), ("Second", "Unlinked Album", "2026-10"),
            ("Third", "Planned Album", "2026"), ("Fourth", "Four", "2026-12-04"),
        ])
        self.assertEqual(rows[0]["date_status"], "reported")
        self.assertIn("https://ab.bandcamp.com/album/one", rows[0]["source_urls"])
        self.assertIn("https://www.reddit.com/r/postrock/wiki/albums/2026/", rows[0]["source_urls"])
        self.assertIn("Fall 2026", rows[2]["notes"])

    def test_wiki_keeps_nonalbum_evidence_until_merge(self):
        m = updater()
        cells = [
            ('<a href="https://one.bandcamp.com/album/a">Studio</a>', "album"),
            ('<a href="https://two.bandcamp.com/track/b">Song</a>', "single"),
            ('<a href="https://three.bandcamp.com/album/c">Short</a> (EP)', "ep"),
            ('<a href="https://four.bandcamp.com/album/d">EP1</a>', "ep"),
            ('Live From Electrical Audio', "live"), ('demos', "demo"),
            ('Revisited (2026 reissue)', "reissue"), ('Anthology (compilation)', "compilation"),
            ('Record (Live)', "live"), ('Record (Remix)', "remix"), ('Record (Remixes)', "remix"),
            ('<a href="https://example.bandcamp.com/album/record">Record</a> (Live)', "live"),
            ('<a href="https://example.bandcamp.com/album/remixed">Record (Remixes)</a>', "remix"),
        ]
        table = ''.join(f'<tr><td>Jan 1</td><td>Act {i}</td><td>{cell}</td><td></td></tr>'
                        for i, (cell, _) in enumerate(cells))
        rows = m.parse_reddit(wiki_payload(table), 2026)
        self.assertEqual([r["release_type"] for r in rows], [kind for _, kind in cells])
        merged = m.merge_catalog(rows)
        self.assertEqual([(r["artist"], r["album"]) for r in merged], [("Act 0", "Studio")])

    def test_unlinked_type_annotation_reclassifies_the_saved_album(self):
        m = updater()
        original = wiki_payload('<tr><td>Oct 2</td><td>Act</td><td>Short</td></tr>')
        changed = wiki_payload('<tr><td>Oct 2</td><td>Act</td><td>Short (EP)</td></tr>')
        existing = m.merge_catalog(m.parse_reddit(original, 2026))
        self.assertEqual(m.merge_catalog(m.parse_reddit(changed, 2026), existing), [])

    def test_encoded_wiki_html_and_duplicate_rows_do_not_duplicate_albums(self):
        m = updater()
        row = '<tr><td>Oct 2</td><td>Act</td><td>A &amp; B</td><td></td></tr>'
        payload = wiki_payload(row + row)
        payload["data"]["content_html"] = html.escape(payload["data"]["content_html"])
        rows = m.merge_catalog(m.parse_reddit(payload, 2026))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album"], "A & B")

    def test_bad_wiki_shapes_and_invalid_dates_fail_instead_of_empty_success(self):
        m = updater()
        bad = [
            {"kind": "wikipage", "data": {"content_html": "<h1>Log in</h1>"}},
            {"error": 403, "message": "Forbidden"},
            wiki_payload('<tr><td>Feb 31</td><td>A</td><td>B</td><td></td></tr>'),
            wiki_payload('<tr><td>2025-10-02</td><td>A</td><td>B</td><td></td></tr>'),
            wiki_payload('<tr><td>Oct 2</td><td>A</td></tr>'),
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                m.parse_reddit(payload, 2026)

    def test_collector_reads_every_requested_wiki_and_refreshes_its_album_links(self):
        m = updater()
        class WikiHTTP:
            def get(self, url):
                if url == "https://www.reddit.com/r/postrock/wiki/albums/2025.json?raw_json=1":
                    return json.dumps(wiki_payload('<tr><td>May 9</td><td>Earlier Act</td><td>Earlier Work</td><td></td></tr>', 2025))
                if url == "https://www.reddit.com/r/postrock/wiki/albums/2026.json?raw_json=1":
                    return json.dumps(wiki_payload('<tr><td>Oct 16</td><td>Example Act</td><td><a href="https://example.bandcamp.com/album/new-album">Wiki Alias</a></td><td></td></tr>'))
                if url == "https://example.bandcamp.com/album/new-album":
                    return bandcamp_document("16 Oct 2026 00:00:00 GMT")
                raise AssertionError(f"Unexpected URL {url}")
        with patch.dict(os.environ, {"REDDIT_ACCESS_TOKEN": ""}):
            rows, errors = m.collect_sources({"reddit": True, "musicbrainz": False}, {2025, 2026}, WikiHTTP(), [])
        self.assertEqual(errors, [])
        merged = m.merge_catalog(rows)
        self.assertEqual(len(merged), 2)
        current = next(r for r in merged if r["year"] == 2026)
        self.assertEqual(current["date_status"], "primary")
        self.assertEqual(current["album"], "New Album")
        self.assertIn("bandcamp:123", current["source_ids"])

    def test_failed_reddit_refresh_reports_warning_and_preserves_saved_rows(self):
        m = updater()
        class BlockedHTTP:
            def get(self, url):
                raise ValueError("HTTP 403")
        existing = m.merge_catalog([observation(source_urls=["https://www.reddit.com/r/postrock/wiki/albums/2026/"])])
        rows, errors = m.collect_sources({"reddit": True, "musicbrainz": False}, {2026}, BlockedHTTP(), [], existing)
        self.assertTrue(any("Reddit 2026" in error and "403" in error for error in errors))
        self.assertEqual(m.merge_catalog(rows, existing), existing)


class CommandTests(unittest.TestCase):
    def test_successful_exclusion_survives_failed_reddit_refresh_and_seed_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            seed = observation("Act", "Short", source_urls=[])
            (folder / "seeds.json").write_text(json.dumps([seed]))
            config = folder / "sources.json"
            config.write_text(json.dumps({"reddit": True, "musicbrainz": False}))
            cache = folder / "cache"
            cache.mkdir()
            url = "https://www.reddit.com/r/postrock/wiki/albums/2026.json?raw_json=1"
            cached = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            payload = wiki_payload('<tr><td>Oct 16</td><td>Act</td><td>Short (EP)</td></tr>')
            cached.write_text(json.dumps({"url": url, "body": json.dumps(payload)}))
            command = [sys.executable, str(SCRIPT), "--year", "2026", "--config", str(config),
                       "--cache-dir", str(cache), "--offline", "--output-dir", str(folder / "out")]
            environment = dict(os.environ, REDDIT_ACCESS_TOKEN="", REDDIT_CLIENT_ID="", REDDIT_CLIENT_SECRET="")
            first = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(first.returncode, 0, first.stderr)
            csv_path = folder / "out/2026.csv"
            with csv_path.open(newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])
            cached.unlink()
            failed_refresh = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(failed_refresh.returncode, 1)
            self.assertIn("Reddit 2026", failed_refresh.stderr)
            with csv_path.open(newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

    def test_script_discovers_new_wiki_rows_and_excludes_reclassified_ep_on_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "seeds.json").write_text("[]")
            config = folder / "sources.json"
            config.write_text(json.dumps({"reddit": True, "musicbrainz": False}))
            cache = folder / "cache"
            cache.mkdir()
            url = "https://www.reddit.com/r/postrock/wiki/albums/2026.json?raw_json=1"
            cached = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            first = '<tr><td>Oct 2</td><td>A, B</td><td>One | Two</td><td></td></tr>'
            second = '<tr><td>Oct 3</td><td>Другий</td><td><a href="https://open.spotify.com/album/two">Тихий альбом</a>{}</td><td></td></tr>'
            cached.write_text(json.dumps({"url": url, "body": json.dumps(wiki_payload(first + second.format("")))}))
            command = [sys.executable, str(SCRIPT), "--year", "2026", "--config", str(config),
                       "--cache-dir", str(cache), "--offline", "--output-dir", str(folder / "out")]
            environment = dict(os.environ, REDDIT_ACCESS_TOKEN="")
            initial = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            csv_path, md_path = folder / "out/2026.csv", folder / "out/2026.md"
            with csv_path.open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)
            third = '<tr><td>Oct 4</td><td>New Act</td><td>Newly Discovered</td><td></td></tr>'
            cached.write_text(json.dumps({"url": url, "body": json.dumps(wiki_payload(first + second.format(" (EP)") + third))}))
            refreshed = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
            with csv_path.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([(r["artist"], r["album"]) for r in rows], [("A, B", "One | Two"), ("New Act", "Newly Discovered")])
            snapshot = (csv_path.read_bytes(), md_path.read_bytes())
            repeated = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual((csv_path.read_bytes(), md_path.read_bytes()), snapshot)
            cached.write_text(json.dumps({"url": url, "body": '<form><input name="js_challenge"></form>'}))
            blocked = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("Reddit 2026", blocked.stderr)
            self.assertEqual((csv_path.read_bytes(), md_path.read_bytes()), snapshot)

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
    def test_reddit_token_only_reaches_oauth_host_and_never_cache_or_redirect(self):
        m = updater()
        from urllib.request import HTTPRedirectHandler
        requests = []
        def public_response(request, timeout):
            requests.append(request)
            return io.BytesIO(b'{"kind":"wikipage","data":{}}')
        secret = "example-test-token-do-not-save"
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"REDDIT_ACCESS_TOKEN": secret}), \
             patch.object(m, "urlopen", side_effect=public_response), patch.object(m.time, "sleep"):
            client = m.HTTPClient(Path(directory))
            client.get("https://oauth.reddit.com/r/postrock/wiki/albums/2026?raw_json=1")
            client.get("https://musicbrainz.org/ws/2/release-group/example")
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer " + secret)
            self.assertIsNone(requests[1].get_header("Authorization"))
            redirected = HTTPRedirectHandler().redirect_request(requests[0], None, 302, "Found", {}, "https://example.com/")
            self.assertIsNone(redirected.get_header("Authorization"))
            self.assertTrue(all(secret not in path.read_text() for path in Path(directory).glob("*.json")))

    def test_reddit_challenges_with_http_200_are_rejected_without_caching(self):
        m = updater()
        bodies = [
            '<html><title>Reddit</title><form><input name="js_challenge" value="1"></form></html>',
            '<html><title>Reddit - Prove your humanity</title></html>',
            "<html><p>You've been blocked by network security.</p></html>",
        ]
        for body in bodies:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with patch.object(m, "urlopen", return_value=io.BytesIO(body.encode())):
                    with self.assertRaises(ValueError):
                        m.HTTPClient(Path(directory)).get("https://www.reddit.com/r/postrock/wiki/albums/2026.json")
                self.assertEqual(list(Path(directory).glob("*.json")), [])

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
