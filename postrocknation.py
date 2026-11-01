"""Public Post-Rock Nation archive, release details and artist genre tags."""

from datetime import date
from html.parser import HTMLParser
import math
import re
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit


BASE = 'https://postrocknation.com'
ARCHIVE = BASE + '/page/release-archive'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
KINDS = {'album', 'single', 'ep', 'live', 'compilation', 'remix', 'demo', 'reissue'}


def native_url(value):
    parts = urlsplit(urljoin(BASE + '/', value))
    host = parts.hostname or ''
    if (parts.scheme not in {'http', 'https'} or parts.username or parts.password or
            parts.port not in {None, 80, 443} or not (host == 'postrocknation.com' or host.endswith('.postrocknation.com'))):
        raise ValueError('Expected a public Post-Rock Nation URL')
    return urlunsplit(('https', parts.netloc, parts.path.rstrip('/'), '', ''))


def album_id(value):
    try:
        match = re.fullmatch(r'/albums/([1-9]\d*)(?:/[^/]+)?', urlsplit(native_url(value)).path)
        return int(match[1]) if match else None
    except ValueError:
        return None


def artist_url(value):
    try:
        url = native_url(value)
        parts = urlsplit(url)
        if re.fullmatch(r'/bands/[1-9]\d*(?:/[^/]+)?', parts.path):
            return url
        if parts.hostname not in {'postrocknation.com', 'www.postrocknation.com', 'reverb.postrocknation.com'} and not parts.path:
            return url
    except ValueError:
        pass
    return None


def iso_date(value):
    if not re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?', value):
        raise ValueError('Invalid Post-Rock Nation release date')
    parts = value.split('-')
    if len(parts) > 1 and parts[1] == '00':
        if len(parts) > 2 and parts[2] != '00':
            raise ValueError('Day without month in Post-Rock Nation date')
        parts = parts[:1]
    if len(parts) > 2 and parts[2] == '00':
        parts = parts[:2]
    date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
    return '-'.join(parts)


def release_kind(kind, title):
    kind = kind.lower().strip()
    if kind not in KINDS:
        raise ValueError(f'Unknown Post-Rock Nation release type: {kind}')
    if kind in {'album', 'single'}:
        patterns = (('ep', r'(?:^|[\s([])e\.?p\.?(?:\d+|$|[\s)\]])'),
                    ('live', r'\(live\)|\blive (?:at|in|from|à)\b'),
                    ('single', r'\(single\)|\[single\]'),
                    ('reissue', r'\b(?:reissue|repress|remastered|remaster)\b'),
                    ('demo', r'\b(?:demo|demos)\b'), ('remix', r'\b(?:remix|remixes)\b'))
        for label, pattern in patterns:
            if re.search(pattern, title, re.I):
                return label
    return kind


class Page(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.text, self.headings, self.links, self.artists, self.styles = [], [], [], [], []
        self.canonical = ''
        self.headers, self.rows = [], []
        self.stack = []
        self.hidden = self.table_depth = 0
        self.archive_depth = self.tags_depth = self.artists_depth = None
        self.row = self.cell = self.anchor = None
        self.in_heading = self.found_tags = False
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or '' for key, value in attrs}
        if tag not in VOID:
            self.stack.append(tag)
        if tag in {'script', 'style'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == 'link' and 'canonical' in attrs.get('rel', '').split():
            self.canonical = attrs.get('href', '')
        if tag == 'h1':
            self.in_heading = True
            self.headings.append([])
        if tag == 'table':
            self.table_depth += 1
            if 'fi-ta-table' in attrs.get('class', '').split():
                self.archive_depth = self.table_depth
        if self.archive_depth == self.table_depth:
            if tag == 'tr':
                self.row = []
            if tag in {'th', 'td'} and self.row is not None:
                self.cell = {'text': [], 'links': [], 'header': tag == 'th'}
        if tag == 'a':
            self.anchor = {'url': attrs.get('href', ''), 'text': []}
            if self.cell is not None:
                self.cell['links'].append(attrs.get('href', ''))

    def handle_data(self, value):
        if self.hidden or not value.strip():
            return
        value = ' '.join(value.split())
        self.text.append(value)
        if self.cell is not None:
            self.cell['text'].append(value)
        if self.anchor is not None:
            self.anchor['text'].append(value)
        if self.in_heading:
            self.headings[-1].append(value)
        if value == 'TAGS:' and not self.found_tags:
            self.tags_depth = len(self.stack) - (self.stack[-1:] == ['span'])
            self.found_tags = True
        elif self.tags_depth is not None:
            self.styles.append(value)
        if value == 'Artists:':
            self.artists_depth = len(self.stack) - (self.stack[-1:] == ['span'])

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden:
            if tag == 'a' and self.anchor is not None:
                self.anchor['text'] = ' '.join(self.anchor['text'])
                self.links.append(self.anchor)
                if self.artists_depth is not None:
                    self.artists.append(self.anchor)
                self.anchor = None
            if tag == 'h1':
                self.in_heading = False
            if tag in {'th', 'td'} and self.cell is not None and self.archive_depth == self.table_depth:
                self.cell['text'] = ' '.join(self.cell['text'])
                self.row.append(self.cell)
                self.cell = None
            if tag == 'tr' and self.row is not None and self.archive_depth == self.table_depth:
                if self.row and all(cell['header'] for cell in self.row):
                    self.headers = [cell['text'] for cell in self.row]
                elif self.row:
                    self.rows.append(self.row)
                self.row = None
            if tag == 'table':
                if self.table_depth == self.archive_depth:
                    self.archive_depth = None
                self.table_depth = max(0, self.table_depth - 1)
        if tag in self.stack:
            position = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.stack = self.stack[:position]
        if self.tags_depth is not None and len(self.stack) < self.tags_depth:
            self.tags_depth = None
        if self.artists_depth is not None and len(self.stack) < self.artists_depth:
            self.artists_depth = None


def observation(artist, title, released, kind, url, profiles):
    identifier = album_id(url)
    if not identifier or not artist.strip() or not title.strip():
        raise ValueError('Post-Rock Nation release lacks an identity')
    released = iso_date(released)
    return {'artist': artist, 'album': title, 'year': int(released[:4]), 'release_date': released,
            'date_status': 'reported', 'release_type': release_kind(kind, title), 'styles': [],
            'source_ids': [f'postrocknation:{identifier}'],
            'source_urls': sorted(set([native_url(url), ARCHIVE] + profiles)),
            'notes': 'Post-Rock Nation dates are catalog claims; genres are artist-level tags.'}


def parse_archive(document):
    page = Page(document)
    headers = {''.join(c for c in value.lower() if c.isalnum()): i for i, value in enumerate(page.headers)}
    required = ('name', 'band', 'releasetype', 'releasedate')
    if not all(key in headers for key in required):
        raise ValueError('Post-Rock Nation archive exposes no release table')
    match = re.search(r'Showing ([\d,]+) to ([\d,]+) of ([\d,]+) results', ' '.join(page.text))
    if not match:
        raise ValueError('Post-Rock Nation archive exposes no pagination range')
    first, last, total = (int(value.replace(',', '')) for value in match.groups())
    rows = []
    for cells in page.rows:
        if len(cells) <= max(headers.values()):
            raise ValueError('Incomplete Post-Rock Nation release row')
        name, band, kind, released = (cells[headers[key]] for key in required)
        urls = [value for value in name['links'] if album_id(value)]
        profiles = [value for value in (artist_url(v) for v in band['links']) if value]
        if not urls:
            raise ValueError('Post-Rock Nation release row has no native album URL')
        rows.append(observation(band['text'], name['text'], released['text'], kind['text'], urls[0], profiles))
    if len(rows) != last - first + 1 and not (not rows and (first, last, total) == (0, 0, 0)):
        raise ValueError('Post-Rock Nation pagination range differs from its row count')
    return rows, first, last, total


def parse_profile(document):
    page = Page(document)
    headings = [' '.join(h).strip() for h in page.headings]
    if not artist_url(page.canonical) or not any(h and not re.fullmatch(r'log\s*in(?: to continue)?|sign in', h, re.I) for h in headings):
        raise ValueError('Post-Rock Nation response is not an artist profile')
    return sorted(set(page.styles))


def parse_detail(document, identifier):
    page = Page(document)
    if album_id(page.canonical) != identifier or not page.headings:
        raise ValueError('Post-Rock Nation detail has a different native album ID')
    match = re.search(r'\breleased (\d{4}(?:-\d{2})?(?:-\d{2})?) (\w+)\b', ' '.join(page.text))
    artists = [link for link in page.artists if artist_url(link['url']) and link['text']]
    if not match or not artists:
        raise ValueError('Post-Rock Nation detail lacks date, type or artist')
    return observation(' / '.join(link['text'] for link in artists), ' '.join(page.headings[0]),
                       match[1], match[2], page.canonical, [artist_url(link['url']) for link in artists])


def collect_postrocknation(client, years, existing=()):
    rows, errors, accepted, seen, saved, profiles = [], [], set(), set(), {}, {}
    for old in existing:
        if int(old['year']) in years:
            for source_id in old.get('source_ids', []):
                match = re.fullmatch(r'postrocknation:([1-9]\d*)', source_id)
                if match:
                    identifier = int(match[1])
                    urls = [url for url in old.get('source_urls', []) if album_id(url) == identifier]
                    if urls:
                        saved[identifier] = (int(old['year']), urls[0])

    def accept(row):
        identifier = int(row['source_ids'][0].split(':')[1])
        if row['year'] not in years and identifier not in saved:
            return
        if identifier in saved:
            row['year'] = saved[identifier][0]
        if row['release_type'] == 'album':
            styles = []
            for url in row['source_urls']:
                if not artist_url(url):
                    continue
                if url not in profiles:
                    try:
                        profiles[url] = parse_profile(client.get(url))
                    except (ValueError, KeyError, TypeError, OSError) as error:
                        errors.append(f'Post-Rock Nation artist {url}: {error}')
                        profiles[url] = []
                styles.extend(profiles[url])
            row['styles'] = sorted(set(styles))
        rows.append(row)
        accepted.add(identifier)

    page_number, page_size, expected_total, previous_year = 1, None, None, 10000
    while True:
        try:
            url = ARCHIVE if page_number == 1 else ARCHIVE + f'?page={page_number}'
            batch, first, last, total = parse_archive(client.get(url))
            if total == 0 and page_number == 1:
                break
            if page_size is None:
                page_size, expected_total = len(batch), total
            if not page_size or total != expected_total or first != (page_number - 1) * page_size + 1 or last > total:
                raise ValueError('Archive pagination changed or did not advance; retry with --refresh')
            if last != total and len(batch) != page_size:
                raise ValueError('Incomplete archive pagination page')
            identifiers = [int(r['source_ids'][0].split(':')[1]) for r in batch]
            if len(set(identifiers)) != len(batch) or seen.intersection(identifiers):
                raise ValueError('Archive pagination repeated native album IDs')
            batch_years = [int(row['release_date'][:4]) for row in batch]
            if batch_years != sorted(batch_years, reverse=True) or batch_years[0] > previous_year:
                raise ValueError('Archive is no longer sorted newest year first')
            seen.update(identifiers)
            previous_year = batch_years[-1]
            for row in batch:
                accept(row)
            print(f'Post-Rock Nation: page {page_number}/{math.ceil(total / page_size)}, {len(rows)} selected observations', file=sys.stderr, flush=True)
            if last == total or previous_year < min(years):
                break
            page_number += 1
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append(f'Post-Rock Nation archive page {page_number}: {error}')
            break
    for identifier, (year, url) in sorted(saved.items()):
        if identifier not in accepted:
            try:
                row = parse_detail(client.get(url), identifier)
                accept(row)
            except (ValueError, KeyError, TypeError, OSError) as error:
                errors.append(f'Post-Rock Nation album {identifier}: {error}')
    return rows, errors
