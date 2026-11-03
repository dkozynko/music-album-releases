"""Last.fm artist tags from public pages rendered by headless Chrome."""

import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, quote_plus, unquote, unquote_plus, urljoin, urlsplit


BASE = 'https://www.last.fm'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
EMPTY = re.compile(r'(?:this artist has no tags(?: yet)?|no tags (?:have been added|yet)|be the first to tag this artist)', re.I)


class Unavailable(ValueError):
    """Service/network/challenge failure, rather than an unknown artist."""


def artist_key(artist):
    return ' '.join(unicodedata.normalize('NFKC', artist).split()).casefold()


def artist_url(artist):
    return BASE + '/music/' + quote_plus(artist.strip(), safe='') + '/+tags'


def tag_list(values):
    if isinstance(values, str):
        values = json.loads(values) if values else []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError('Expected a list of Last.fm tag strings')
    result = {}
    for value in sorted({' '.join(html.unescape(v).split()) for v in values}, key=lambda v: (v.casefold(), v)):
        if value:
            result.setdefault(value.casefold(), value)
    return list(result.values())


def checked_artist_url(value, artist, page=None, allow_root=False):
    parts = urlsplit(urljoin(artist_url(artist), value))
    path = parts.path.strip('/').split('/')
    if (parts.scheme != 'https' or parts.hostname not in {'www.last.fm', 'last.fm'} or
            parts.username or parts.password or parts.port not in {None, 443} or
            not (len(path) == 3 and unquote(path[2]) == '+tags' or allow_root and len(path) == 2) or
            path[0] != 'music' or
            artist_key(unquote_plus(path[1])) != artist_key(artist)):
        raise ValueError('Last.fm response is not the requested artist tags page')
    number = parse_qs(parts.query).get('page', ['1'])
    if len(number) != 1 or not number[0].isdigit() or int(number[0]) < 1:
        raise ValueError('Invalid Last.fm pagination')
    if page is not None and int(number[0]) != page:
        raise ValueError('Last.fm pagination did not advance')
    return artist_url(artist) + ('?page=' + number[0] if int(number[0]) > 1 else '')


class TagPage(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags, self.text, self.next = [], [], [], []
        self.canonical = ''
        self.anchor = None
        self.hidden = 0
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = set((attrs.get('class') or '').split())
        if tag not in VOID:
            self.stack.append((tag, classes))
        if tag in {'script', 'style'}:
            self.hidden += 1
        if self.hidden:
            return
        rel = (attrs.get('rel') or '').split()
        if tag == 'link' and 'canonical' in rel:
            self.canonical = attrs.get('href', '')
        if tag in {'a', 'link'} and ('next' in rel or any('pagination-next' in labels for _, labels in self.stack)):
            if attrs.get('href') and not any('disabled' in labels for _, labels in self.stack):
                self.next.append(attrs['href'])
        if tag == 'a' and any('tags-list--global' in labels for _, labels in self.stack):
            target = urlsplit(urljoin(BASE, attrs.get('href', '')))
            if target.hostname in {'www.last.fm', 'last.fm'} and re.fullmatch(r'/tag/[^/]+/?', target.path):
                self.anchor = []

    def handle_data(self, value):
        if not self.hidden and value.strip():
            self.text.append(value.strip())
            if self.anchor is not None:
                self.anchor.append(value)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag == 'a' and self.anchor is not None:
            self.tags.append(''.join(self.anchor))
            self.anchor = None
        positions = [i for i, (name, _) in enumerate(self.stack) if name == tag]
        if positions:
            self.stack = self.stack[:positions[-1]]


def parse_tags(document, artist, page_number):
    if re.search(r'<title[^>]*>[^<]*(?:Client Challenge|Just a moment|Temporarily Unavailable)', document, re.I):
        raise Unavailable('Last.fm exposed a challenge or service error')
    page = TagPage(document)
    if not page.canonical:
        raise ValueError('Last.fm artist-page identity is missing')
    checked_artist_url(page.canonical, artist, allow_root=True)
    tags = tag_list(page.tags)
    if not tags and not EMPTY.search(' '.join(page.text)):
        raise ValueError('Last.fm did not expose artist tags or an explicit empty result')
    following = {checked_artist_url(url, artist, page_number + 1) for url in page.next}
    if len(following) > 1:
        raise ValueError('Conflicting Last.fm next-page links')
    return tags, next(iter(following), None)


def read_artist(page, artist, timeout=20):
    from playwright.sync_api import Error
    url, number, tags, seen = artist_url(artist), 1, [], set()
    while url:
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
            if response and response.status >= 400:
                error = Unavailable if response.status >= 500 or response.status in {403, 429} else ValueError
                raise error(f'Last.fm HTTP {response.status}')
            page.wait_for_function("""() => document.querySelector('.tags-list--global .tag a') ||
                /this artist has no tags|no tags (?:have been added|yet)|be the first to tag this artist/i.test(document.body.innerText)""", timeout=timeout * 1000)
            checked_artist_url(page.url, artist, number)
            document = page.content()
        except Error as error:
            raise Unavailable('Last.fm browser navigation or tag rendering failed') from error
        if len(document.encode('utf-8')) > 10_000_000:
            raise ValueError('Last.fm artist page exceeds 10 MB')
        batch, following = parse_tags(document, artist, number)
        fingerprint = tuple(batch)
        if fingerprint in seen:
            raise ValueError('Last.fm pagination repeated a tag page')
        seen.add(fingerprint)
        tags.extend(batch)
        url, number = following, number + 1
        if url:
            time.sleep(1.1)
    return tag_list(tags)


def cache_path(cache_dir, artist):
    return Path(cache_dir) / 'lastfm-artists' / (hashlib.sha256(artist_key(artist).encode()).hexdigest() + '.json')


def save_cache(cache_dir, artist, tags):
    path = cache_path(cache_dir, artist)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'artist': artist, 'url': artist_url(artist), 'tags': tag_list(tags)}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def enrich(rows, years, cache_dir, offline=False, refresh=False):
    artists = {artist_key(row['artist']): row['artist'] for row in rows if row['year'] in years}
    results, pending, errors = {}, [], []
    for key, artist in sorted(artists.items()):
        path = cache_path(cache_dir, artist)
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if artist_key(snapshot['artist']) != key or snapshot['url'] != artist_url(snapshot['artist']):
                raise ValueError('Cached Last.fm artist identity differs')
            cached = tag_list(snapshot['tags'])
            if offline or not refresh and time.time() - path.stat().st_mtime < 86400:
                results[key] = cached
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if offline:
            errors.append(f'Last.fm artist {artist}: no valid cached artist tags; kept previous tags')
        else:
            pending.append((key, artist))
    if pending:
        from reddit_browser import RedditBrowser
        failures = 0
        try:
            with tempfile.TemporaryDirectory(prefix='music-album-releases-lastfm-') as profile:
                with RedditBrowser(profile, headless=True) as browser:
                    for index, (key, artist) in enumerate(pending):
                        try:
                            tags = read_artist(browser.page, artist)
                            save_cache(cache_dir, artist, tags)
                            results[key] = tags
                            failures = 0
                        except (ValueError, OSError) as error:
                            errors.append(f'Last.fm artist {artist}: {error}; kept previous tags')
                            if isinstance(error, Unavailable):
                                failures += 1
                        print(f'Last.fm: checked {index + 1}/{len(pending)} artists', file=sys.stderr, flush=True)
                        if failures >= 3:
                            errors.append(f'Last.fm: stopped after 3 service failures; kept previous tags for {len(pending) - index - 1} remaining artists')
                            break
                        time.sleep(1.1)
        except (ValueError, OSError) as error:
            errors.append(f'Last.fm browser: {error}; kept previous artist tags')
    for row in rows:
        key = artist_key(row['artist'])
        if key in results:
            row['lastfm_artist_tags'] = list(results[key])
    return errors
