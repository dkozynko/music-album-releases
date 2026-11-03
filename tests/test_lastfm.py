import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_update import observation, updater


def tags_page(artist='Example Act', tags=('post-rock', 'ambient'), next_page=None, empty=False):
    from urllib.parse import quote_plus
    base = 'https://www.last.fm/music/' + quote_plus(artist) + '/+tags'
    links = ''.join(f'<li class="tag"><a href="/tag/{tag}">{tag}</a></li>' for tag in tags)
    pagination = f'<a class="pagination-next" href="?page={next_page}">Next</a>' if next_page else ''
    return (f'<link rel="canonical" href="{base}"><h1>{artist}</h1>'
            '<a href="/tag/not-an-artist-tag">Navigation</a>'
            '<ul class="tags-list tags-list--global">' + links + '</ul>' +
            ('<p>This artist has no tags yet.</p>' if empty else '') + pagination)


class LastfmTests(unittest.TestCase):
    def test_column_isolated_from_styles_and_survives_partial_album_refresh(self):
        m = updater()
        rows = m.merge_catalog([observation(styles=['Post Rock'], lastfm_artist_tags=['ambient', 'post-rock'])])
        self.assertEqual(rows[0].get('lastfm_artist_tags'), ['ambient', 'post-rock'])
        self.assertEqual(rows[0]['styles'], ['Post Rock'])
        refreshed = m.merge_catalog([observation(styles=['Drone'])], rows)
        self.assertEqual(refreshed[0]['lastfm_artist_tags'], ['ambient', 'post-rock'])
        self.assertEqual(refreshed[0]['styles'], ['Drone', 'Post Rock'])

    def test_artist_change_does_not_keep_another_bands_tags(self):
        m = updater()
        old = m.merge_catalog([observation(lastfm_artist_tags=['synthpop'])])
        corrections = [{'artist': old[0]['artist'], 'album': old[0]['album'], 'canonical_artist': 'Different Band'}]
        rows = m.merge_catalog([], old, corrections)
        self.assertEqual(rows[0].get('lastfm_artist_tags'), [])

    def test_markdown_has_separate_escaped_lastfm_column(self):
        m = updater()
        rows = m.merge_catalog([observation(styles=['Post Rock'], lastfm_artist_tags=['ambient | drone'])])
        document = m.render_markdown(2026, rows)
        self.assertIn('| Styles | Last.fm artist tags |', document)
        self.assertIn('Post Rock | ambient \\| drone |', document)

    def test_csv_migrates_the_current_schema_without_losing_album_facts(self):
        m = updater()
        old_fields = ['id','year','artist','album','styles','release_date','date_status','release_type','date_candidates','source_ids','source_urls','notes']
        old = m.merge_catalog([observation(styles=['Post Rock'])])[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / '2026.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=old_fields)
                writer.writeheader()
                writer.writerow({key: json.dumps(old[key]) if isinstance(old[key], list) else old[key] for key in old_fields})
            rows = m.read_catalog(root)
            self.assertEqual(rows[0].get('lastfm_artist_tags'), [])
            with contextlib.redirect_stdout(io.StringIO()): m.write_catalog(root, rows, {2026})
            migrated = m.read_catalog(root)[0]
            self.assertEqual({k:v for k,v in migrated.items() if k != 'lastfm_artist_tags'}, {k:v for k,v in old.items() if k != 'lastfm_artist_tags'})
            self.assertIn('lastfm_artist_tags', (root / '2026.csv').read_text().splitlines()[0])

    def test_artist_url_encodes_punctuation_without_creating_an_album_path(self):
        import lastfm
        self.assertEqual(lastfm.artist_url('A/B & C+D'), 'https://www.last.fm/music/A%2FB+%26+C%2BD/+tags')
        self.assertNotEqual(lastfm.artist_key('A+B'), lastfm.artist_key('AB'))

    def test_parser_reads_only_global_artist_tags_and_follows_pagination(self):
        import lastfm
        tags, next_url = lastfm.parse_tags(tags_page(tags=('post-rock', 'ambient'), next_page=2), 'Example Act', 1)
        self.assertEqual(tags, ['ambient', 'post-rock'])
        self.assertEqual(next_url, 'https://www.last.fm/music/Example+Act/+tags?page=2')

    def test_artist_root_canonical_is_allowed_on_a_valid_artist_tags_document(self):
        import lastfm
        document = tags_page().replace('Example+Act/+tags', 'Example+Act')
        self.assertEqual(lastfm.parse_tags(document, 'Example Act', 1), (['ambient','post-rock'], None))

    def test_album_wrong_artist_and_challenge_pages_are_not_successful_tag_results(self):
        import lastfm
        documents = [tags_page('Other Act'), tags_page().replace('/+tags', '/An+Album/+tags'), '<title>Client Challenge</title>', '<h1>Temporarily unavailable</h1>']
        for document in documents:
            with self.subTest(document=document[:80]), self.assertRaises(ValueError):
                lastfm.parse_tags(document, 'Example Act', 1)

    def test_explicit_no_tags_is_empty_success_but_missing_markup_is_failure(self):
        import lastfm
        self.assertEqual(lastfm.parse_tags(tags_page(tags=(), empty=True), 'Example Act', 1), ([], None))
        with self.assertRaises(ValueError): lastfm.parse_tags(tags_page(tags=()), 'Example Act', 1)

    def test_repeated_or_foreign_pagination_is_rejected(self):
        import lastfm
        for target in ('?page=1', 'https://www.last.fm/music/Other/+tags?page=2'):
            document = tags_page(next_page=2).replace('?page=2', target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                lastfm.parse_tags(document, 'Example Act', 1)

    def test_cache_and_cli_enrich_all_same_artist_albums_without_a_browser(self):
        import lastfm
        m = updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root / 'cache'; output = root / 'output'
            lastfm.save_cache(cache, 'Example Act', ['ambient', 'post-rock'])
            saved = m.merge_catalog([observation(artist='Example Act'), observation(artist='Example Act', album='Second Album')])
            with contextlib.redirect_stdout(io.StringIO()): m.write_catalog(output, saved, {2026})
            args = ['--year','2026','--lastfm-only','--offline','--cache-dir',str(cache),'--output-dir',str(output)]
            with patch('reddit_browser.RedditBrowser', side_effect=AssertionError('Offline must not open Chrome')):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(m.main(args), 0)
                    first = {p.name:p.read_bytes() for p in output.iterdir()}
                    self.assertEqual(m.main(args), 0)
                self.assertEqual(first, {p.name:p.read_bytes() for p in output.iterdir()})
            rows = m.read_catalog(output)
            self.assertTrue(all(r['lastfm_artist_tags'] == ['ambient','post-rock'] for r in rows))
            self.assertEqual([{k:v for k,v in r.items() if k != 'lastfm_artist_tags'} for r in rows],
                             [{k:v for k,v in r.items() if k != 'lastfm_artist_tags'} for r in saved])

    def test_failed_refresh_keeps_saved_tags_and_does_not_mix_styles(self):
        import lastfm
        rows = [{'artist':'Example Act','year':2026,'styles':['Post Rock'],'lastfm_artist_tags':['ambient']}]
        with tempfile.TemporaryDirectory() as directory:
            errors = lastfm.enrich(rows, {2026}, Path(directory), offline=True)
        self.assertTrue(errors)
        self.assertEqual(rows[0]['lastfm_artist_tags'], ['ambient'])
        self.assertEqual(rows[0]['styles'], ['Post Rock'])

    def test_normal_update_uses_configured_lastfm_enrichment(self):
        import lastfm
        m = updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root/'cache'; output=root/'output'
            lastfm.save_cache(cache, 'Example Act', ['post-rock'])
            (root/'sources.json').write_text(json.dumps({'lastfm_artist_tags':True,'musicbrainz':False,'seed_file':'seeds.json'}))
            (root/'seeds.json').write_text(json.dumps([observation(artist='Example Act', source_urls=[])]))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = m.main(['--year','2026','--offline','--config',str(root/'sources.json'),'--cache-dir',str(cache),'--output-dir',str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(m.read_catalog(output)[0]['lastfm_artist_tags'], ['post-rock'])

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_real_headless_browser_reads_all_pages_and_caches_only_artist_tags(self):
        import lastfm
        from reddit_browser import RedditBrowser
        class RoutedBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                def handle(route):
                    document = tags_page(tags=('drone',)) if route.request.url.endswith('?page=2') else tags_page(next_page=2)
                    route.fulfill(content_type='text/html', body=document + '<script>window.privateSession="do-not-cache"</script>')
                self.context.route('https://www.last.fm/**', handle)
                return self
        rows = [{'artist':'Example Act','year':2026,'lastfm_artist_tags':[]},
                {'artist':'Example Act','year':2025,'lastfm_artist_tags':[]}]
        with tempfile.TemporaryDirectory() as directory:
            with patch('reddit_browser.RedditBrowser', RoutedBrowser):
                errors = lastfm.enrich(rows, {2025,2026}, Path(directory))
            self.assertEqual(errors, [])
            self.assertEqual([row['lastfm_artist_tags'] for row in rows], [['ambient','drone','post-rock']]*2)
            snapshots = list(Path(directory).rglob('*.json'))
            self.assertEqual(len(snapshots), 1)
            self.assertNotIn('do-not-cache', snapshots[0].read_text())
            self.assertNotIn('<html', snapshots[0].read_text())

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_partial_browser_page_failure_preserves_complete_cache_and_csv_tags(self):
        import lastfm
        from reddit_browser import RedditBrowser
        class PartialBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                def handle(route):
                    if route.request.url.endswith('?page=2'):
                        route.fulfill(status=503, body='<h1>Temporarily unavailable</h1>')
                    else:
                        route.fulfill(content_type='text/html', body=tags_page(tags=('new-partial-tag',),next_page=2))
                self.context.route('https://www.last.fm/**', handle)
                return self
        rows = [{'artist':'Example Act','year':2026,'lastfm_artist_tags':['ambient']}]
        with tempfile.TemporaryDirectory() as directory:
            lastfm.save_cache(Path(directory), 'Example Act', ['ambient'])
            path = next(Path(directory).rglob('*.json')); saved = path.read_bytes()
            with patch('reddit_browser.RedditBrowser', PartialBrowser):
                errors = lastfm.enrich(rows, {2026}, Path(directory), refresh=True)
            self.assertTrue(errors)
            self.assertEqual(rows[0]['lastfm_artist_tags'], ['ambient'])
            self.assertEqual(path.read_bytes(), saved)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_expired_cache_cannot_replace_newer_csv_tags_after_a_failed_refresh(self):
        import lastfm
        from reddit_browser import RedditBrowser
        class UnavailableBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                self.context.route('https://www.last.fm/**', lambda route: route.fulfill(status=503, body='<h1>Temporarily unavailable</h1>'))
                return self
        rows = [{'artist':'Example Act','year':2026,'lastfm_artist_tags':['newer-tag']}]
        with tempfile.TemporaryDirectory() as directory:
            lastfm.save_cache(Path(directory), 'Example Act', ['obsolete-tag'])
            with patch('reddit_browser.RedditBrowser', UnavailableBrowser):
                errors = lastfm.enrich(rows, {2026}, Path(directory), refresh=True)
            self.assertTrue(errors)
            self.assertEqual(rows[0]['lastfm_artist_tags'], ['newer-tag'])

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_access_denial_stops_after_three_artists_and_preserves_saved_data(self):
        import lastfm
        from reddit_browser import RedditBrowser
        requests = []
        class DeniedBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                def handle(route):
                    requests.append(route.request.url)
                    route.fulfill(status=403, body='<title>Client Challenge</title>')
                self.context.route('https://www.last.fm/**', handle)
                return self
        rows = [{'artist':f'Act {i}','year':2026,'lastfm_artist_tags':['saved-tag']} for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for row in rows: lastfm.save_cache(root, row['artist'], row['lastfm_artist_tags'])
            saved = {p.name:p.read_bytes() for p in root.rglob('*.json')}
            progress = io.StringIO()
            with patch('reddit_browser.RedditBrowser', DeniedBrowser), patch('lastfm.time.sleep'), contextlib.redirect_stderr(progress):
                errors = lastfm.enrich(rows, {2026}, root, refresh=True)
            self.assertEqual(len(requests), 3)
            self.assertTrue(any('stopped after 3' in error for error in errors))
            self.assertTrue(all(row['lastfm_artist_tags'] == ['saved-tag'] for row in rows))
            self.assertEqual({p.name:p.read_bytes() for p in root.rglob('*.json')}, saved)
            self.assertIn('HTTP 403', progress.getvalue())
            self.assertIn('Client Challenge', progress.getvalue())

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_http_200_service_error_reports_its_body_status(self):
        import lastfm
        from reddit_browser import RedditBrowser
        with tempfile.TemporaryDirectory() as directory, RedditBrowser(directory) as browser:
            browser.context.route('https://www.last.fm/**', lambda route: route.fulfill(content_type='text/html',
                body='<title>Last.fm - Temporarily Unavailable</title><h1>Temporarily unavailable</h1><div>Error 503</div>'))
            with self.assertRaisesRegex(lastfm.Unavailable, r'service error 503.*HTTP 200'):
                lastfm.read_artist(browser.page, 'Example Act', timeout=1)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_unavailable_page_without_error_number_does_not_invent_service_error_200(self):
        import lastfm
        from reddit_browser import RedditBrowser
        with tempfile.TemporaryDirectory() as directory, RedditBrowser(directory) as browser:
            browser.context.route('https://www.last.fm/**', lambda route: route.fulfill(content_type='text/html',
                body='<title>Last.fm - Temporarily Unavailable</title><h1>Temporarily unavailable</h1>'))
            with self.assertRaisesRegex(lastfm.Unavailable, r'service error unknown.*HTTP 200'):
                lastfm.read_artist(browser.page, 'Example Act', timeout=1)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_challenge_timeout_reports_http_status_and_page_title(self):
        import lastfm
        from reddit_browser import RedditBrowser
        with tempfile.TemporaryDirectory() as directory, RedditBrowser(directory) as browser:
            browser.context.route('https://www.last.fm/**', lambda route: route.fulfill(content_type='text/html',
                body='<title>Client Challenge</title><h1>JavaScript required</h1>'))
            with self.assertRaisesRegex(lastfm.Unavailable, r'access challenge.*HTTP 200.*Client Challenge'):
                lastfm.read_artist(browser.page, 'Example Act', timeout=1)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_missing_tag_markup_on_an_artist_page_is_not_a_global_service_failure(self):
        import lastfm
        from reddit_browser import RedditBrowser
        with tempfile.TemporaryDirectory() as directory, RedditBrowser(directory) as browser:
            browser.context.route('https://www.last.fm/**', lambda route: route.fulfill(content_type='text/html',
                body=tags_page(tags=())))
            with self.assertRaises(ValueError) as caught:
                lastfm.read_artist(browser.page, 'Example Act', timeout=1)
            self.assertNotIsInstance(caught.exception, lastfm.Unavailable)
            self.assertIn('HTTP 200', str(caught.exception))

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_three_artist_markup_failures_do_not_skip_a_following_healthy_artist(self):
        import lastfm
        from reddit_browser import RedditBrowser
        from urllib.parse import unquote_plus
        class ArtistPagesBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                def handle(route):
                    artist = unquote_plus(route.request.url.split('/music/')[1].split('/')[0])
                    tags = ('post-rock',) if artist == 'Act 3' else ()
                    route.fulfill(content_type='text/html', body=tags_page(artist, tags=tags))
                self.context.route('https://www.last.fm/**', handle)
                return self
        rows = [{'artist':f'Act {i}','year':2026,'lastfm_artist_tags':[]} for i in range(4)]
        read_artist = lastfm.read_artist
        with tempfile.TemporaryDirectory() as directory:
            with patch('reddit_browser.RedditBrowser', ArtistPagesBrowser), patch('lastfm.time.sleep'), \
                 patch('lastfm.read_artist', side_effect=lambda page, artist: read_artist(page, artist, timeout=1)):
                errors = lastfm.enrich(rows, {2026}, Path(directory))
            self.assertEqual(rows[3]['lastfm_artist_tags'], ['post-rock'])
            self.assertEqual(len(errors), 3)
            self.assertFalse(any('stopped after 3' in error for error in errors))

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Optional Playwright dependency not installed')
    def test_artist_specific_404_interrupts_the_consecutive_service_failure_count(self):
        import lastfm
        from reddit_browser import RedditBrowser
        from urllib.parse import unquote_plus
        class IntermittentBrowser(RedditBrowser):
            def __enter__(self):
                super().__enter__()
                def handle(route):
                    artist = unquote_plus(route.request.url.split('/music/')[1].split('/')[0])
                    index = int(artist.rsplit(' ', 1)[1])
                    status = 200 if index == 5 else 404 if index % 2 else 503
                    route.fulfill(status=status, content_type='text/html', body=tags_page(artist, ('post-rock',)))
                self.context.route('https://www.last.fm/**', handle)
                return self
        rows = [{'artist':f'Act {i}','year':2026,'lastfm_artist_tags':[]} for i in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            with patch('reddit_browser.RedditBrowser', IntermittentBrowser), patch('lastfm.time.sleep'):
                errors = lastfm.enrich(rows, {2026}, Path(directory))
            self.assertEqual(rows[5]['lastfm_artist_tags'], ['post-rock'])
            self.assertFalse(any('stopped after 3' in error for error in errors))


if __name__ == '__main__':
    unittest.main()
