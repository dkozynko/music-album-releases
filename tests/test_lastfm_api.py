import contextlib
import io
import json
import os
from http.client import BadStatusLine, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import lastfm


KEY = 'private-test-key-not-for-cache'


def payload(artist='Example Act', tags=('post-rock', 'ambient')):
    return {'toptags': {'tag': [{'name':name,'count':100,'url':'https://www.last.fm/tag/'+name} for name in tags],
                        '@attr': {'artist':artist}}}


class Transport:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return io.BytesIO(json.dumps(response).encode())


class LastfmAPITests(unittest.TestCase):
    def client(self, transport):
        import lastfm_api
        with patch('lastfm_api.build_opener', return_value=transport):
            return lastfm_api.ArtistTagsClient(KEY)

    def test_requests_artist_tags_with_exact_encoding_and_no_autocorrection(self):
        transport = Transport(payload('A/B & C+D'))
        client = self.client(transport)
        self.assertEqual(client.get_tags('A/B & C+D'), ['ambient','post-rock'])
        request = transport.requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(urlsplit(request.full_url).scheme, 'https')
        self.assertEqual(urlsplit(request.full_url).netloc, 'ws.audioscrobbler.com')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            'method':['artist.getTopTags'],'artist':['A/B & C+D'],'autocorrect':['0'],
            'format':['json'],'api_key':[KEY]})

    def test_explicit_empty_tag_list_is_successful_and_wrong_artist_is_rejected(self):
        self.assertEqual(self.client(Transport(payload(tags=()))).get_tags('Example Act'), [])
        with self.assertRaises(ValueError):
            self.client(Transport(payload('Another Act'))).get_tags('Example Act')

    def test_missing_tag_structure_is_not_a_successful_empty_result(self):
        documents = [{}, {'toptags':{}}, {'toptags':{'tag':[]}},
                     {'toptags':{'tag':'unexpected','@attr':{'artist':'Example Act'}}}]
        for document in documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.client(Transport(document)).get_tags('Example Act')

    def test_invalid_key_stops_immediately_and_redacts_reflected_secret(self):
        import lastfm_api
        transport = Transport({'error':10,'message':'Rejected key '+KEY})
        with self.assertRaises(lastfm_api.InvalidAPIKey) as caught:
            self.client(transport).get_tags('Example Act')
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn('10', str(caught.exception))

    def test_rate_limit_is_retried_then_returns_valid_tags(self):
        transport = Transport({'error':29,'message':'Rate limit exceeded'}, payload())
        with patch('lastfm_api.time.sleep'):
            tags = self.client(transport).get_tags('Example Act')
        self.assertEqual(tags, ['ambient','post-rock'])
        self.assertEqual(len(transport.requests), 2)

    def test_persistent_service_error_has_bounded_retries(self):
        transport = Transport(*[{'error':11,'message':'Service offline'}]*3)
        with patch('lastfm_api.time.sleep'), self.assertRaises(lastfm.Unavailable):
            self.client(transport).get_tags('Example Act')
        self.assertEqual(len(transport.requests), 3)

    def test_transport_errors_never_expose_a_request_url_with_the_key(self):
        transport = Transport(*[URLError('Could not fetch https://example/?api_key='+KEY)]*3)
        with patch('lastfm_api.time.sleep'), self.assertRaises(ValueError) as caught:
            self.client(transport).get_tags('Example Act')
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn('api_key=', str(caught.exception))

    def test_http_protocol_errors_are_sanitized_and_retry_boundedly(self):
        for error in (BadStatusLine('api_key='+KEY), IncompleteRead(KEY.encode(),100)):
            with self.subTest(error=type(error).__name__):
                transport = Transport(error,error,error)
                with patch('lastfm_api.time.sleep'), self.assertRaises(lastfm.Unavailable) as caught:
                    self.client(transport).get_tags('Example Act')
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(transport.requests), 3)

    def test_malformed_shared_schema_stops_after_three_and_keeps_saved_data(self):
        transport = Transport({}, {}, {}, {}, {})
        rows = [{'artist':f'Band {i}','year':2026,'lastfm_artist_tags':['saved']} for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LASTFM_API_KEY':KEY}), patch('lastfm_api.build_opener',return_value=transport), patch('lastfm_api.time.sleep'):
                errors = lastfm.enrich(rows, {2026}, Path(directory))
            self.assertEqual(len(transport.requests), 3)
            self.assertTrue(any('stopped after 3' in error for error in errors))
            self.assertTrue(all(row['lastfm_artist_tags'] == ['saved'] for row in rows))
            self.assertEqual(list(Path(directory).rglob('*.json')), [])

    def test_numeric_error_code_cannot_reflect_a_numeric_api_key(self):
        import lastfm_api
        numeric_key = '12345678901234567890123456789012'
        transport = Transport({'error':numeric_key})
        with patch('lastfm_api.build_opener', return_value=transport):
            client = lastfm_api.ArtistTagsClient(numeric_key)
        with self.assertRaises(lastfm.Unavailable) as caught:
            client.get_tags('Example Act')
        self.assertNotIn(numeric_key, str(caught.exception))

    def test_reflected_key_is_not_accepted_as_a_public_tag(self):
        with self.assertRaises(lastfm.Unavailable) as caught:
            self.client(Transport(payload(tags=('post-rock',KEY)))).get_tags('Example Act')
        self.assertNotIn(KEY, str(caught.exception))

    def test_redirects_are_rejected_before_a_secret_can_leave_the_api_host(self):
        import lastfm_api
        request = Request('https://ws.audioscrobbler.com/2.0/?api_key='+KEY)
        handler = lastfm_api.NoRedirect()
        self.assertIsNone(handler.redirect_request(request, None, 302, 'Found', {}, 'https://other.example/?api_key='+KEY))
        transport = Transport(HTTPError(request.full_url, 302, 'Found', {}, None))
        with self.assertRaises(ValueError) as caught:
            self.client(transport).get_tags('Example Act')
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_actual_http_redirect_is_not_followed_by_the_constructed_client(self):
        import lastfm_api
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                if self.path.startswith('/api?'):
                    self.send_response(302)
                    self.send_header('Location', f'http://localhost:{self.server.server_port}/leak?'+urlsplit(self.path).query)
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps(payload()).encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch('lastfm_api.API_ROOT', f'http://127.0.0.1:{server.server_port}/api'), self.assertRaises(lastfm.Unavailable) as caught:
                lastfm_api.ArtistTagsClient(KEY).get_tags('Example Act')
            self.assertEqual(len(paths), 1)
            self.assertNotIn(KEY, str(caught.exception))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_http_rate_limit_retries_without_exposing_the_key(self):
        error = HTTPError('https://ws.audioscrobbler.com/?api_key='+KEY,429,'Rate limit',{'Retry-After':'0'},None)
        transport = Transport(error, payload())
        with patch('lastfm_api.time.sleep'):
            self.assertEqual(self.client(transport).get_tags('Example Act'), ['ambient','post-rock'])
        self.assertEqual(len(transport.requests), 2)

    def test_api_enrichment_updates_all_albums_once_and_caches_no_key(self):
        transport = Transport(payload())
        rows = [{'artist':'Example Act','year':2026,'styles':['Drone'],'lastfm_artist_tags':[]},
                {'artist':'Example Act','year':2025,'styles':['Post Rock'],'lastfm_artist_tags':[]}]
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LASTFM_API_KEY':KEY}), patch('lastfm_api.build_opener',return_value=transport), \
                 patch('reddit_browser.RedditBrowser', side_effect=AssertionError('API must not launch Chrome')):
                errors = lastfm.enrich(rows, {2025,2026}, Path(directory))
            self.assertEqual(errors, [])
            self.assertEqual([r['lastfm_artist_tags'] for r in rows], [['ambient','post-rock']]*2)
            self.assertEqual([r['styles'] for r in rows], [['Drone'],['Post Rock']])
            self.assertEqual(len(transport.requests), 1)
            snapshot = next(Path(directory).rglob('*.json')).read_text()
            self.assertNotIn(KEY, snapshot)
            self.assertNotIn('api_key', snapshot)
            with patch.dict(os.environ, {'LASTFM_API_KEY':KEY}), patch('lastfm_api.build_opener', side_effect=AssertionError('Cache must avoid HTTP')):
                self.assertEqual(lastfm.enrich(rows, {2025,2026}, Path(directory)), [])

    def test_unknown_artist_does_not_stop_following_artist_and_preserves_tags(self):
        transport = Transport({'error':6,'message':'Artist not found'}, payload('Band B'))
        rows = [{'artist':'Band A','year':2026,'lastfm_artist_tags':['saved']},
                {'artist':'Band B','year':2026,'lastfm_artist_tags':[]}]
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LASTFM_API_KEY':KEY}), patch('lastfm_api.build_opener',return_value=transport), patch('lastfm_api.time.sleep'):
                errors = lastfm.enrich(rows, {2026}, Path(directory))
        self.assertEqual(len(errors), 1)
        self.assertEqual(rows[0]['lastfm_artist_tags'], ['saved'])
        self.assertEqual(rows[1]['lastfm_artist_tags'], ['ambient','post-rock'])

    def test_key_error_does_not_launch_browser_or_repeat_for_every_artist(self):
        transport = Transport({'error':10,'message':KEY})
        rows = [{'artist':f'Band {i}','year':2026,'lastfm_artist_tags':['saved']} for i in range(5)]
        log = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(log):
            with patch.dict(os.environ, {'LASTFM_API_KEY':KEY}), patch('lastfm_api.build_opener',return_value=transport), \
                 patch('reddit_browser.RedditBrowser',side_effect=AssertionError('No browser fallback for a rejected key')):
                errors = lastfm.enrich(rows, {2026}, Path(directory))
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(errors)
        self.assertTrue(all(r['lastfm_artist_tags'] == ['saved'] for r in rows))
        self.assertNotIn(KEY, log.getvalue()+' '.join(errors))


if __name__ == '__main__':
    unittest.main()
