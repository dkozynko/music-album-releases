"""Read public artist tags through Last.fm's official API."""

import json
from http.client import HTTPException
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lastfm import InvalidAPIKey, Unavailable, artist_key, tag_list


API_ROOT = 'https://ws.audioscrobbler.com/2.0/'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The API key belongs only to the fixed HTTPS API endpoint.
        return None


class ArtistTagsClient:
    def __init__(self, api_key):
        self.api_key = api_key.strip()
        if not self.api_key:
            raise InvalidAPIKey('Last.fm API needs LASTFM_API_KEY')
        self.opener = build_opener(NoRedirect())
        self.last_request = None

    def get_tags(self, artist):
        url = API_ROOT + '?' + urlencode({'method':'artist.getTopTags', 'artist':artist,
            'autocorrect':0, 'format':'json', 'api_key':self.api_key})
        failure = 'Last.fm API request failed'
        for attempt in range(3):
            if self.last_request is not None:
                time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            wait = 2 ** (attempt + 1)
            try:
                request = Request(url, headers={'User-Agent':'music-album-releases/0.1 (https://github.com/dkozynko/music-album-releases)',
                                                'Accept':'application/json'})
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise Unavailable('Last.fm API response exceeds 1 MB')
                try:
                    payload = json.loads(raw.decode('utf-8'))
                except (ValueError, UnicodeError):
                    raise Unavailable('Last.fm API returned invalid JSON') from None
            except HTTPError as error:
                status = error.code
                retry_after = (error.headers or {}).get('Retry-After', '')
                error.close()
                if status == 401:
                    raise InvalidAPIKey('Last.fm API key rejected (HTTP 401)') from None
                failure = f'Last.fm API HTTP {status}'
                if status not in {429,500,502,503,504}:
                    raise Unavailable(failure) from None
                if retry_after.isdigit() and len(retry_after) <= 4:
                    wait = min(30, int(retry_after))
            except (URLError, TimeoutError, OSError, HTTPException):
                # Exceptions can contain the full request URL; never expose it.
                failure = 'Last.fm API network request failed'
            else:
                if not isinstance(payload, dict):
                    raise Unavailable('Last.fm API response is not an object')
                code = payload.get('error')
                if code is None:
                    tags = self.parse_tags(payload, artist)
                    if any(self.api_key in tag for tag in tags):
                        raise Unavailable('Last.fm API returned invalid public tag data')
                    return tags
                if not isinstance(code, (int, str)) or len(str(code)) > 3 or not str(code).isdigit():
                    raise Unavailable('Last.fm API returned an invalid error code')
                code = int(code)
                if code in {4,10,26}:
                    raise InvalidAPIKey(f'Last.fm API key rejected (error {code})')
                if code in {6,7}:
                    raise ValueError(f'Last.fm API artist lookup failed (error {code})')
                failure = f'Last.fm API error {code}'
                if code not in {8,11,16,29}:
                    raise Unavailable(failure)
            if attempt < 2:
                time.sleep(wait)
        raise Unavailable(failure) from None

    @staticmethod
    def parse_tags(payload, artist):
        top = payload.get('toptags')
        if not isinstance(top, dict) or not isinstance(top.get('@attr'), dict):
            raise Unavailable('Last.fm API artist-tag structure is missing')
        name = top['@attr'].get('artist')
        if not isinstance(name, str):
            raise Unavailable('Last.fm API artist identity is missing')
        if artist_key(name) != artist_key(artist):
            raise ValueError('Last.fm API returned a different artist')
        tags = top.get('tag')
        if isinstance(tags, dict) and 'name' in tags:
            tags = [tags]
        if not isinstance(tags, list) or any(not isinstance(tag, dict) or
                not isinstance(tag.get('name'), str) or not tag['name'].strip() for tag in tags):
            raise Unavailable('Last.fm API tag list is missing or invalid')
        return tag_list([tag['name'] for tag in tags])
