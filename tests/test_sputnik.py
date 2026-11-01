import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from test_update import updater
import sputnik


BASE = "https://www.sputnikmusic.com"
LISTING_URL = BASE + "/newreleases.php?t=6"
BAND_URL = BASE + "/bands/Example-Act/70/"


def listing(items=((101, "October 2026"),)):
    return '<title>New Releases - Post Rock | Sputnikmusic</title><select id="genreselect"><option value="newreleases.php?t=6" selected>Post Rock</option></select>' + ''.join(
        f'<b>{month}</b><table><tr><td><a href=/album/{identifier}//><b>Example Act</b> A Long Re...</a></td></tr></table>'
        for identifier, month in items)


def album(identifier=101, title="A Long Record & Other Stories", year=2026):
    query = urlencode({"Submit": "Submit", "a": identifier, "band_name": "Example Act", "album_name": title})
    return f'<title>Example Act - {title} User Opinions - sputnikmusic</title><a href="bands/Example-Act/70/">Example Act</a><a href="digging.php?{query}">dig it</a><td>Release Date: <b>{year}</b></td><p>Ratings | November 1st 26</p>'


def band(sections=(("LPs", (101, 102)), ("EPs", (103,)), ("Live Albums", (104,)), ("Compilations", (105,)))):
    return '<ul class="tags"><li><a href="/genre/6/post-rock/">Post Rock</a></li><li><a href="/genre/33/ambient/">Ambient</a></li></ul><select name="genre[]"><optgroup label="EXISTING TAGS"><option value="6">Post Rock (3 votes)</option><option value="93">Math Rock (1 votes)</option></optgroup><optgroup label="ALL GENRES"><option value="4">Hip-Hop</option><option value="1">Metal</option></optgroup></select><table class=plaincontentbox>' + ''.join(
        (f'<tr><td><strong>{section}</strong></td></tr>' if section else '') + ''.join(
            f'<tr><td><a href="/album/{identifier}/Example-Act-Record/"><img src="cover.jpg"></a></td><td><b><a href="/album/{identifier}/Example-Act-Record/">Record {identifier}</a></b><font>2026</font></td></tr>'
            for identifier in identifiers)
        for section, identifiers in sections) + '</table><aside><a href="/album/999/Other-Band-Record/">Related</a></aside>'


class FixtureClient:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


def pages():
    return {LISTING_URL: listing(), BASE + '/soundoff.php?albumid=101': album(), BAND_URL: band()}


class SputnikTests(unittest.TestCase):
    def test_listing_uses_month_headers_and_deduplicates_native_ids(self):
        rows = sputnik.parse_listing(listing(((101, 'October 2026'), (101, 'October 2026'), (102, 'September 2026'))))
        self.assertEqual(rows, {101: {'2026-10'}, 102: {'2026-09'}})
        for body in ('<title>Access denied</title>', listing().replace('t=6', 't=33')):
            with self.subTest(body=body), self.assertRaises(ValueError):
                sputnik.parse_listing(body)

    def test_album_uses_full_identity_release_year_and_root_relative_band_link(self):
        row = sputnik.parse_album(album(), 101)
        self.assertEqual((row['artist'], row['album'], row['year'], row['band_url']),
                         ('Example Act', 'A Long Record & Other Stories', 2026, BAND_URL))
        with self.assertRaises(ValueError):
            sputnik.parse_album(album(identifier=999), 101)

    def test_album_named_after_a_month_does_not_change_the_next_release_month(self):
        document = listing().replace('A Long Re...', 'September 2025')
        document += '<table><tr><td><a href="/album/102//"><b>Example Act</b> Other Record</a></td></tr></table>'
        self.assertEqual(sputnik.parse_listing(document), {101: {'2026-10'}, 102: {'2026-10'}})

    def test_band_collects_all_assigned_tags_and_explicit_release_types(self):
        styles, types = sputnik.parse_band(band())
        self.assertEqual(set(styles), {'Post Rock', 'Ambient', 'Math Rock'})
        self.assertEqual(types, {101: 'album', 102: 'album', 103: 'ep', 104: 'live', 105: 'compilation'})
        self.assertEqual(sputnik.parse_band(band(((None, (101,)),)))[1], {101: 'album'})

    def test_navigation_genre_links_do_not_become_artist_tags(self):
        document = '<nav><a href="/genre/4/hip-hop/">Hip-Hop</a></nav>' + band()
        styles, _ = sputnik.parse_band(document)
        self.assertNotIn('Hip-Hop', styles)

    def test_changed_year_of_a_saved_album_reaches_date_conflict_resolution(self):
        data = pages()
        data[BASE + '/soundoff.php?albumid=101'] = album(year=2020)
        existing = [{'artist': 'Example Act', 'album': 'A Long Record & Other Stories', 'year': 2026,
                     'release_date': '2026-10', 'source_ids': ['sputnikmusic:101']}]
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026}, existing)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]['release_date'], '2020')
        merged = updater().merge_catalog(rows, existing)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['date_status'], 'conflict')
        self.assertEqual(merged[0]['date_candidates'], ['2020', '2026-10'])

    def test_collect_preserves_month_precision_and_distinct_works_for_one_artist(self):
        data = pages()
        data[LISTING_URL] = listing(((101, 'October 2026'), (102, 'October 2026'), (103, 'October 2026')))
        data[BASE + '/soundoff.php?albumid=102'] = album(102, 'Another Record')
        data[BASE + '/soundoff.php?albumid=103'] = album(103, 'Short Record')
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026})
        self.assertEqual(errors, [])
        self.assertEqual([r['release_date'] for r in rows], ['2026-10'] * 3)
        self.assertEqual([r['release_type'] for r in rows], ['album', 'album', 'ep'])
        self.assertEqual(rows[0]['source_ids'], ['sputnikmusic:101'])
        merged = updater().merge_catalog(rows)
        self.assertEqual({r['album'] for r in merged}, {'A Long Record & Other Stories', 'Another Record'})
        self.assertEqual(set(merged[0]['styles']), {'Post Rock', 'Ambient', 'Math Rock'})

    def test_metadata_year_never_becomes_the_new_listing_year(self):
        data = pages()
        data[BASE + '/soundoff.php?albumid=101'] = album(year=2020)
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026})
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2020, 2026})
        self.assertEqual(rows[0]['release_date'], '2020')
        self.assertEqual(rows[0]['year'], 2020)

    def test_saved_ids_refresh_after_rolling_off_the_listing_and_failures_are_isolated(self):
        data = pages()
        data[LISTING_URL] = listing(((102, 'October 2026'),))
        data[BASE + '/soundoff.php?albumid=102'] = ValueError('HTTP 404')
        existing = [{'year': 2026, 'source_ids': ['sputnikmusic:101']}]
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026}, existing)
        self.assertEqual([(r['album'], r['release_date']) for r in rows], [('A Long Record & Other Stories', '2026')])
        self.assertEqual(len(errors), 1)
        self.assertIn('102', errors[0])
        data[LISTING_URL] = '<h1>Access denied</h1>'
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026}, existing)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)

    def test_cached_cli_imports_styles_and_preserves_catalog_on_failed_refresh(self):
        m = updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, output = root / 'cache', root / 'output'
            cache.mkdir()
            (root / 'sources.json').write_text(json.dumps({'sputnikmusic': True, 'reddit': False, 'musicbrainz': False}))
            (root / 'seeds.json').write_text('[]')
            (root / 'corrections.json').write_text('[]')
            def save(url, body):
                path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
                path.write_text(json.dumps({'url': url, 'body': body}))
            for url, body in pages().items():
                save(url, body)
            args = ['--year', '2026', '--offline', '--config', str(root / 'sources.json'), '--corrections', str(root / 'corrections.json'), '--cache-dir', str(cache), '--output-dir', str(output)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(args), 0)
                before = {p.name: p.read_bytes() for p in output.iterdir()}
                self.assertEqual(m.main(args), 0)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)
            with (output / '2026.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['release_date'], '2026-10')
            self.assertEqual(set(json.loads(rows[0]['styles'])), {'Post Rock', 'Ambient', 'Math Rock'})
            self.assertIn('Sputnikmusic', (output / '2026.md').read_text())
            save(LISTING_URL, '<h1>Access denied</h1>')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(args), 1)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
