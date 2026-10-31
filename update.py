#!/usr/bin/env python3
"""Update a personal post-rock catalog. Python standard library only."""

import argparse
import calendar
import csv
from datetime import date
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
USER_AGENT = "music-album-releases/0.1 (https://github.com/dkozynko/music-album-releases)"
FIELDS = ["id", "year", "artist", "album", "release_date", "date_status",
          "release_type", "date_candidates", "source_ids", "source_urls", "notes"]
LIST_FIELDS = {"date_candidates", "source_ids", "source_urls"}
STATUSES = {"unknown", "reported", "primary", "corrected", "conflict"}
NON_ALBUMS = {"ep", "single", "live", "compilation", "remix", "demo", "reissue", "excluded"}


def normalize_date(value):
    """ISO dates with unknown components remain year/month precision."""
    value = str(value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", value):
        raise ValueError(f"Invalid ISO release date: {value!r}")
    parts = value.split("-")
    year = int(parts[0])
    date(year, 1, 1)
    if len(parts) == 1 or parts[1] == "00":
        if len(parts) == 3 and parts[2] != "00":
            raise ValueError(f"Day without a month: {value!r}")
        return parts[0]
    month = int(parts[1])
    date(year, month, 1)
    if len(parts) == 2 or parts[2] == "00":
        return "-".join(parts[:2])
    date(year, month, int(parts[2]))
    return value


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def album_key(artist, album):
    artist_key, title_key = normalized(artist), normalized(album)
    if title_key in {"st", "selftitled"}:
        title_key = artist_key
    return artist_key + ":" + title_key


def string_list(value):
    if isinstance(value, str):
        value = json.loads(value) if value else []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError("Expected a list of strings")
    return value


def checked_url(value):
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"Invalid public source URL: {value!r}")
    return value


def bandcamp_url(value):
    parts = urlsplit(checked_url(value))
    if not parts.hostname.endswith(".bandcamp.com") or parts.port not in {None, 443}:
        raise ValueError(f"Expected a Bandcamp artist/label URL: {value!r}")
    return urlunsplit(("https", parts.hostname, parts.path.rstrip("/"), "", ""))


def prepare(record):
    if not isinstance(record, dict):
        raise ValueError("Each album observation must be an object")
    row = dict(record)
    for field in ("artist", "album"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"An observation needs a nonempty {field}")
        row[field] = html.unescape(row[field]).strip()
    row["release_date"] = normalize_date(row.get("release_date", ""))
    row["year"] = int(row.get("year") or row["release_date"][:4])
    date(row["year"], 1, 1)
    row["date_status"] = row.get("date_status", "reported")
    if row["date_status"] not in STATUSES:
        raise ValueError(f"Unknown date status: {row['date_status']}")
    row["release_type"] = str(row.get("release_type", "album")).strip().lower()
    if row["release_type"] not in NON_ALBUMS | {"album"}:
        raise ValueError(f"Unknown release type: {row['release_type']}")
    row["source_ids"] = sorted({v for v in string_list(row.get("source_ids", []))
                                if v.partition(":")[2] and v.partition(":")[2] not in {"0", "null", "None"}})
    row["source_urls"] = sorted({checked_url(v) for v in string_list(row.get("source_urls", []))})
    row["date_candidates"] = sorted({normalize_date(v) for v in string_list(row.get("date_candidates", [])) if v})
    if row["release_date"] and (row["date_status"] != "conflict" or not row["date_candidates"]):
        row["date_candidates"] = sorted(set(row["date_candidates"] + [row["release_date"]]))
    row["notes"] = str(row.get("notes", ""))
    return row


def validate_corrections(corrections):
    for correction in corrections:
        if "id" in correction or not (correction.get("source_id") or
                                      correction.get("artist") and correction.get("album")):
            raise ValueError("Match corrections with source_id or artist + album; CSV id is read-only")
        for field in ("source_id", "artist", "album", "canonical_artist", "canonical_album"):
            if field in correction and (not isinstance(correction[field], str) or not correction[field].strip()):
                raise ValueError(f"A correction needs a nonempty {field}")
        if "release_type" in correction and str(correction["release_type"]).strip().lower() not in NON_ALBUMS | {"album"}:
            raise ValueError(f"Unknown correction release type: {correction['release_type']}")
        if "exclude" in correction and not isinstance(correction["exclude"], bool):
            raise ValueError("Correction exclude must be true or false")
        if "release_date" in correction:
            if not normalize_date(correction["release_date"]):
                raise ValueError("A date correction must provide an original release date")
            checked_url(correction["source_url"])


def matches(row, correction):
    if correction.get("source_id"):
        return correction["source_id"] in row["source_ids"]
    key = album_key(row["artist"], row["album"])
    return key in {
        album_key(correction["artist"], correction["album"]),
        album_key(correction.get("canonical_artist", correction["artist"]),
                  correction.get("canonical_album", correction["album"])),
    }


def apply_corrections(row, corrections):
    for correction in corrections:
        if not matches(row, correction):
            continue
        row["artist"] = correction.get("canonical_artist", row["artist"])
        row["album"] = correction.get("canonical_album", row["album"])
        if "canonical_artist" in correction or "canonical_album" in correction:
            row["_canonical_corrected"] = True
        if "release_date" in correction:
            value = normalize_date(correction["release_date"])
            source = checked_url(correction["source_url"])
            if not value:
                raise ValueError("A date correction must provide an original release date")
            row.update(release_date=value, year=int(value[:4]), date_status="corrected",
                       date_candidates=sorted(set(row["date_candidates"] + [value])))
            row["source_urls"] = sorted(set(row["source_urls"] + [source]))
        if "release_type" in correction:
            row["release_type"] = correction["release_type"].strip().lower()
            row["_type_corrected"] = True
        if correction.get("exclude"):
            row["_excluded"] = True
        if correction.get("note"):
            row["notes"] += "\n" + correction["note"]
    return row


def resolve_dates(rows):
    candidates = sorted({v for r in rows for v in r["date_candidates"] if v})
    pinned = sorted({r["release_date"] for r in rows if r["date_status"] == "corrected" and r["release_date"]})
    active = pinned or candidates
    if not active:
        return "", "unknown", candidates
    longest = max(active, key=len)
    if all(longest.startswith(v) for v in active):
        status = "corrected" if pinned else "primary" if any(r["date_status"] == "primary" for r in rows) else "reported"
        return longest, status, candidates
    shared = []
    for pieces in zip(*(v.split("-") for v in active)):
        if len(set(pieces)) != 1:
            break
        shared.append(pieces[0])
    return "-".join(shared), "conflict", candidates


def merge_catalog(observations, existing=(), corrections=()):
    validate_corrections(corrections)
    rows = [apply_corrections(prepare(r), corrections) for r in list(existing) + list(observations)]
    parents = list(range(len(rows)))
    seen = {}

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(rows):
        identities = ["name:" + album_key(row["artist"], row["album"])] + row["source_ids"]
        if row.get("id"):
            identities.append("saved:" + row["id"])
        for identity in identities:
            if identity in seen:
                parents[find(i)] = find(seen[identity])
            else:
                seen[identity] = i
    grouped = {}
    for i, row in enumerate(rows):
        grouped.setdefault(find(i), []).append(row)
    output = []
    ranks = {"corrected": 4, "primary": 3, "conflict": 2, "reported": 1, "unknown": 0}
    for group in grouped.values():
        corrected_types = {r["release_type"] for r in group if r.get("_type_corrected")}
        types = corrected_types or {r["release_type"] for r in group}
        if any(r.get("_excluded") for r in group) or types.intersection(NON_ALBUMS):
            continue
        preferred = min(group, key=lambda r: (not r.get("_canonical_corrected", False), -ranks[r["date_status"]],
                        normalized(r["album"]) in {"st", "selftitled"},
                        r["artist"].isupper(), r["artist"].casefold(), r["artist"], r["album"].casefold(), r["album"]))
        value, status, candidates = resolve_dates(group)
        notes = {line.strip() for r in group for line in r["notes"].splitlines()
                 if line.strip() and not line.startswith(("Date conflict: ", "Other dates: "))}
        if status == "conflict":
            notes.add("Date conflict: " + " / ".join(candidates))
        elif status == "corrected" and any(not (v.startswith(value) or value.startswith(v)) for v in candidates):
            notes.add("Other dates: " + " / ".join(candidates))
        saved_ids = sorted({r["id"] for r in group if r.get("id")})
        identifier = saved_ids[0] if saved_ids else "pr_" + hashlib.sha256(album_key(preferred["artist"], preferred["album"]).encode()).hexdigest()[:16]
        output.append({
            "id": identifier, "year": int(value[:4]) if value else preferred["year"],
            "artist": preferred["artist"], "album": preferred["album"],
            "release_date": value, "date_status": status, "release_type": "album",
            "date_candidates": candidates,
            "source_ids": sorted({v for r in group for v in r["source_ids"]}),
            "source_urls": sorted({v for r in group for v in r["source_urls"]}),
            "notes": "\n".join(sorted(notes)),
        })
    return sorted(output, key=lambda r: (r["year"], r["release_date"] or "9999", r["artist"].casefold(), r["album"].casefold(), r["id"]))


class BandcampPage(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.data = None
        self.client_items = []
        self.links = set()
        self.tags = []
        self.is_catalog = False
        self._tag = False
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-tralbum" in attrs:
            self.data = json.loads(attrs["data-tralbum"])
        if "data-client-items" in attrs:
            items = json.loads(attrs["data-client-items"])
            if not isinstance(items, list):
                raise ValueError("Unexpected Bandcamp catalog structure")
            self.client_items.extend(items)
            self.is_catalog = True
        if attrs.get("id") == "music-grid":
            self.is_catalog = True
        if tag == "a":
            self._tag = "tag" in attrs.get("class", "").split()
            if "/album/" in attrs.get("href", ""):
                self.links.add(attrs["href"])

    def handle_data(self, data):
        if self._tag and data.strip():
            self.tags.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a":
            self._tag = False


def parse_bandcamp_catalog(document, url):
    page = BandcampPage(document)
    if not page.is_catalog:
        # Bandcamp redirects a one-album artist's /music page straight to that album.
        data = page.data or {}
        if data.get("current", {}).get("type") == "album" and data.get("url"):
            candidate = bandcamp_url(data["url"])
            if urlsplit(candidate).hostname == urlsplit(url).hostname and urlsplit(candidate).path.startswith("/album/"):
                return [candidate]
        raise ValueError("Bandcamp catalog markup missing (possibly a challenge/error page)")
    links = page.links | {item["page_url"] for item in page.client_items
                          if item.get("type") == "album" and item.get("page_url")}
    output = set()
    for link in links:
        try:
            candidate = bandcamp_url(urljoin(url, link))
        except ValueError:
            continue
        if urlsplit(candidate).path.startswith("/album/"):
            output.add(candidate)
    return sorted(output)


def parse_bandcamp_album(document, url):
    page = BandcampPage(document)
    data = page.data or {}
    current = data.get("current", {})
    if not current or current.get("type") != "album":
        raise ValueError("Bandcamp album data missing (not an album or an access challenge)")
    raw_date = current.get("release_date") or data.get("album_release_date")
    value = parsedate_to_datetime(raw_date).date().isoformat() if raw_date else ""
    title = current["title"]
    kind = "album"
    tags = {normalized(t) for t in page.tags}
    if re.search(r"(?:^|\s|[([])e\.?p\.?(?:$|\s|[)\]])", title, re.I) or "ep" in tags:
        kind = "ep"
    elif re.search(r"\blive\s*(?:@|at\b|in\b|from\b)", title, re.I) or "livealbum" in tags:
        kind = "live"
    elif "compilation" in tags or normalized(current.get("artist") or data.get("artist", "")) in {"various", "variousartists"}:
        kind = "compilation"
    return {
        "artist": current.get("artist") or data.get("artist"), "album": title,
        "year": int(value[:4]) if value else None,
        "release_date": value, "date_status": "primary" if value else "unknown",
        "release_type": kind, "source_ids": [f"bandcamp:{current['id']}"],
        "source_urls": [bandcamp_url(url)], "notes": "", "tags": page.tags,
    }


def parse_musicbrainz(payload, year=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("release-groups"), list):
        raise ValueError("MusicBrainz release-group response has an unexpected structure")
    output = []
    for group in payload["release-groups"]:
        if not any(normalized(t["name"]) == "postrock" for t in group.get("tags", [])):
            continue
        primary = str(group.get("primary-type", "")).lower()
        secondary = {str(v).lower() for v in group.get("secondary-types", [])} - {"soundtrack"}
        # Keep explicit exclusions until the merge so an existing album can be reclassified.
        kind = "album" if primary == "album" else primary if primary in NON_ALBUMS else "excluded"
        if secondary:
            kind = next(iter(sorted(secondary & NON_ALBUMS)), "excluded")
        value = normalize_date(group.get("first-release-date", ""))
        artist = "".join(c.get("name", c.get("artist", {}).get("name", "")) + c.get("joinphrase", "")
                         for c in group.get("artist-credit", [])).strip()
        output.append({
            "artist": artist, "album": group["title"], "year": int(value[:4]) if value else year,
            "release_date": value, "date_status": "reported" if value else "unknown", "release_type": kind,
            "source_ids": ["musicbrainz:" + group["id"]],
            "source_urls": ["https://musicbrainz.org/release-group/" + group["id"]], "notes": "",
        })
    return output


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class HTTPClient:
    def __init__(self, cache_dir, offline=False, refresh=False):
        self.cache_dir = cache_dir
        self.offline, self.refresh = offline, refresh
        self.last_request = 0.0
        self.errors = []

    def get(self, url):
        checked_url(url)
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if cached and self.is_challenge(cached["body"]):
            cached = None
        if cached and (self.offline or not self.refresh and time.time() - path.stat().st_mtime < 86400):
            return cached["body"]
        if self.offline:
            raise ValueError(f"No cached response for {url}")
        failure = None
        for attempt in range(3):
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                if urlsplit(url).hostname.endswith(".bandcamp.com") and shutil.which("curl"):
                    result = subprocess.run([
                        "curl", "--silent", "--show-error", "--location", "--max-redirs", "5", "--max-time", "30",
                        "--max-filesize", "10000000", "--user-agent", USER_AGENT,
                        "--header", "Accept: application/json,text/html;q=0.9",
                        "--write-out", "\n%{http_code}", "--url", url,
                    ], capture_output=True, text=True, encoding="utf-8")
                    if result.returncode:
                        raise URLError(result.stderr.strip() or "curl failed")
                    body, separator, status = result.stdout.rpartition("\n")
                    if not separator or not status.isdigit():
                        raise ValueError("curl returned no HTTP status")
                    if int(status) != 200:
                        raise HTTPError(url, int(status), "HTTP source error", {}, None)
                else:
                    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9"})
                    with urlopen(request, timeout=30) as response:
                        raw = response.read(10_000_001)
                        if len(raw) > 10_000_000:
                            raise ValueError("Source response exceeds 10 MB")
                        body = raw.decode("utf-8")
                if self.is_challenge(body):
                    raise ValueError(f"Access challenge at {url}; response was not cached")
                atomic_write(path, json.dumps({"url": url, "body": body}, ensure_ascii=False))
                return body
            except HTTPError as error:
                failure = error
                if error.code not in {429, 500, 502, 503, 504}:
                    break
                wait = error.headers.get("Retry-After", "")
                time.sleep(min(30, int(wait)) if wait.isdigit() else 2 ** (attempt + 1))
            except (URLError, TimeoutError, OSError) as error:
                failure = error
                time.sleep(2 ** attempt)
        if cached:
            self.errors.append(f"Using stale cache for {url}: {failure}")
            return cached["body"]
        raise ValueError(f"Could not fetch {url}: {failure}")

    @staticmethod
    def is_challenge(body):
        return bool(re.search(r"<title[^>]*>\s*(?:Client Challenge|Just a moment)", body, re.I))


def collect_sources(config, years, client, seeds, existing=()):
    rows, errors = list(seeds), []
    if config.get("musicbrainz", True):
        for year in sorted(years):
            offset, count = 0, 1
            ids = set()
            try:
                while offset < count:
                    query = f'tag:"post-rock" AND primarytype:album AND firstreleasedate:[{year:04d}-01-01 TO {year:04d}-12-31]'
                    url = "https://musicbrainz.org/ws/2/release-group/?" + urlencode({"query": query, "fmt": "json", "limit": 100, "offset": offset})
                    payload = json.loads(client.get(url))
                    batch = parse_musicbrainz(payload, year)
                    page_ids = {g["id"] for g in payload["release-groups"]}
                    count = int(payload["count"])
                    if offset < count and not page_ids.difference(ids):
                        raise ValueError("MusicBrainz pagination did not advance")
                    ids.update(page_ids)
                    rows.extend(batch)
                    offset += 100
            except (ValueError, KeyError, TypeError) as error:
                errors.append(f"MusicBrainz {year}: {error}")
    explicit = {bandcamp_url(v) for v in config.get("bandcamp_albums", [])}
    known_years = {}
    selected = list(seeds) + [r for r in existing if int(r["year"]) in years]
    for row in selected:
        for url in row.get("source_urls", []):
            try:
                url = bandcamp_url(url)
            except ValueError:
                continue
            if urlsplit(url).path.startswith("/album/"):
                explicit.add(url)
                known_years[url] = int(row.get("year") or row["release_date"][:4])
    albums = set(explicit)
    for url in config.get("bandcamp_catalogs", []):
        try:
            url = bandcamp_url(url)
            albums.update(parse_bandcamp_catalog(client.get(url), url))
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"Bandcamp catalog {url}: {error}")
    for index, url in enumerate(sorted(albums), 1):
        try:
            row = parse_bandcamp_album(client.get(url), url)
            if url in known_years:
                # A contradictory original year must reach the merge, not vanish at this filter.
                row["year"] = known_years[url]
                rows.append(row)
            elif row["year"] in years and (url in explicit or any(normalized(t) == "postrock" for t in row["tags"])):
                rows.append(row)
            elif url in explicit and row["year"] is None:
                raise ValueError("Selected album has no release date; import an observation with its discovery year")
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"Bandcamp album {url}: {error}")
        if index % 25 == 0:
            print(f"Bandcamp: checked {index}/{len(albums)} album pages", file=sys.stderr, flush=True)
    return rows, errors


def read_catalog(output_dir):
    rows = []
    for path in sorted(output_dir.glob("[0-9][0-9][0-9][0-9].csv")):
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != FIELDS:
                raise ValueError(f"Unexpected CSV columns in {path}; use --input to import other formats")
            rows.extend(prepare(r) for r in reader)
    return rows


def md_text(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def source_link(url):
    checked_url(url)
    host = urlsplit(url).hostname
    name = "Bandcamp" if host.endswith(".bandcamp.com") else "MusicBrainz" if host == "musicbrainz.org" else "Discogs" if host.endswith("discogs.com") else "AOTY" if host.endswith("albumoftheyear.org") else "Reddit" if host.endswith("reddit.com") else host
    return f"[{md_text(name)}]({quote(url, safe=':/?=&%#@+~.-_')})"


def render_markdown(year, rows):
    rows = [r for r in rows if int(r["year"]) == year]
    lines = [f"# Post-rock albums — {year}", "", f"{len(rows)} albums. [Download CSV]({year}.csv).", "",
             "Generated from the CSV. An evolving catalog, not an exhaustive census.", "",
             "Dates stay partial when the day/month is unknown. Future dates are announced releases and may change.", "",
             "Date status: **primary** = artist/label page; **reported** = catalog; **corrected** = sourced manual correction; **conflict** = sources disagree.", ""]
    buckets = {}
    for row in rows:
        value = row["release_date"]
        month = int(value[5:7]) if len(value) >= 7 else 0
        buckets.setdefault(month, []).append(row)
    for month in sorted(buckets, key=lambda m: m or 13):
        lines += ["## " + (calendar.month_name[month] if month else "Month unknown / needs review"), "",
                  "| Date | Artist | Album | Date status | Sources | Notes |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for row in sorted(buckets[month], key=lambda r: (r["release_date"], r["artist"].casefold(), r["album"].casefold())):
            cells = [row["release_date"] or "Unresolved", row["artist"], row["album"], row["date_status"]]
            links = " ".join(source_link(u) for u in string_list(row["source_urls"]))
            lines.append("| " + " | ".join([*(md_text(v) for v in cells), links, md_text(row["notes"])]) + " |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_catalog(output_dir, rows, years):
    years = set(years) | {r["year"] for r in rows} | {int(p.stem) for p in output_dir.glob("[0-9][0-9][0-9][0-9].csv")}
    for year in sorted(years):
        selected = [r for r in rows if r["year"] == year]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in selected:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False, separators=(",", ":")) if key in LIST_FIELDS else row[key] for key in FIELDS})
        csv_text = stream.getvalue()
        # Render the serialized CSV, so its contents are the single output truth.
        round_tripped = [prepare(r) for r in csv.DictReader(io.StringIO(csv_text))]
        markdown = render_markdown(year, round_tripped)
        atomic_write(output_dir / f"{year:04d}.csv", csv_text)
        atomic_write(output_dir / f"{year:04d}.md", markdown)
        conflicts = sum(r["date_status"] == "conflict" for r in selected)
        print(f"{year}: {len(selected)} albums, {conflicts} date conflicts → {output_dir / f'{year:04d}.csv'}")


def load_list(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", nargs="+", type=int, default=[date.today().year])
    parser.add_argument("--input", type=Path, help="Import observations from JSON instead of fetching configured sources")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "releases/post-rock")
    parser.add_argument("--config", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--corrections", type=Path, default=ROOT / "corrections.json")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache/http")
    parser.add_argument("--offline", action="store_true", help="Use cached HTTP responses only")
    parser.add_argument("--refresh", action="store_true", help="Ignore the 24-hour HTTP cache lifetime")
    parser.add_argument("--render-only", action="store_true", help="Regenerate Markdown from existing CSV without changing CSV")
    args = parser.parse_args(argv)
    try:
        for year in args.year:
            date(year, 1, 1)
        existing = read_catalog(args.output_dir)
        if args.render_only:
            for year in args.year:
                if not (args.output_dir / f"{year:04d}.csv").exists():
                    raise ValueError(f"No CSV for {year}")
                atomic_write(args.output_dir / f"{year:04d}.md", render_markdown(year, existing))
            return 0
        corrections = load_list(args.corrections)
        validate_corrections(corrections)
        errors = []
        if args.input:
            observations = load_list(args.input)
        else:
            config = json.loads(args.config.read_text(encoding="utf-8"))
            seeds = load_list(args.config.parent / config.get("seed_file", "seeds.json"))
            seeds = [r for r in seeds if int(r.get("year") or r["release_date"][:4]) in args.year]
            client = HTTPClient(args.cache_dir, args.offline, args.refresh)
            observations, errors = collect_sources(config, set(args.year), client, seeds, existing)
            errors += client.errors
        rows = merge_catalog(observations, existing, corrections)
        if not rows and errors:
            raise ValueError("All sources failed and there is no existing catalog to preserve")
        write_catalog(args.output_dir, rows, args.year)
        for error in errors:
            print("Warning: " + error, file=sys.stderr)
        return 1 if errors else 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
