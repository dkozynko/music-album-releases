#!/usr/bin/env python3
"""Update a personal post-rock catalog; optional Playwright for Reddit HTML."""

import argparse
import base64
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

from lastfm import artist_key as lastfm_artist_key, tag_list as lastfm_tag_list


ROOT = Path(__file__).resolve().parent
USER_AGENT = "music-album-releases/0.1 (https://github.com/dkozynko/music-album-releases)"
FIELDS = ["id", "year", "artist", "album", "styles", "lastfm_artist_tags", "release_date", "date_status",
          "release_type", "date_candidates", "source_ids", "source_urls", "notes"]
PREVIOUS_FIELDS = [field for field in FIELDS if field != "lastfm_artist_tags"]
LEGACY_FIELDS = [field for field in PREVIOUS_FIELDS if field != "styles"]
LIST_FIELDS = {"styles", "lastfm_artist_tags", "date_candidates", "source_ids", "source_urls"}
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


def style_list(value):
    labels = {" ".join(html.unescape(v).split()) for v in string_list(value)}
    unique = {}
    for label in sorted(labels, key=lambda v: (v.casefold(), v)):
        if label:
            unique.setdefault(normalized(label) or label.casefold(), label)
    return list(unique.values())


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
    row["styles"] = style_list(string_list(row.get("styles", [])) + string_list(row.get("tags", [])))
    row["lastfm_artist_tags"] = lastfm_tag_list(row.get("lastfm_artist_tags", []))
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
        artist = correction.get("canonical_artist", row["artist"])
        if lastfm_artist_key(artist) != lastfm_artist_key(row["artist"]):
            row["lastfm_artist_tags"] = []
        row["artist"] = artist
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
    # A compatible year/month-only claim adds no disagreement to more precise claims.
    active = [v for v in active if not any(other != v and other.startswith(v) for other in active)]
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

    prn_singles = {i for i, row in enumerate(rows) if row["release_type"] == "single" and
                   any(value.startswith("postrocknation:") for value in row["source_ids"])}

    def link_rows(separate_formats=(), isolate_singles=False):
        for i, row in enumerate(rows):
            private_single = isolate_singles and i in prn_singles and i not in separate_formats
            suffix = (":album" if row["release_type"] == "album" else ":nonalbum") if i in separate_formats else ":prn-single" if private_single else ""
            name = "native-single:" + "|".join(v for v in row["source_ids"] if v.startswith("postrocknation:")) if private_single else "name:" + album_key(row["artist"], row["album"]) + suffix
            identities = [name]
            identities += [value + suffix if value.startswith(("reddit-wiki:", "bandcamp-url:")) else value
                           for value in row["source_ids"]]
            if row.get("id"):
                identities.append("saved:" + row["id"])
            for identity in identities:
                if identity in seen:
                    parents[find(i)] = find(seen[identity])
                else:
                    seen[identity] = i

    link_rows()
    tentative = {}
    for i in range(len(rows)):
        tentative.setdefault(find(i), []).append(i)
    separate = set()
    for indices in tentative.values():
        formats = {}
        for i in indices:
            for value in rows[i]["source_ids"]:
                provider = value.partition(":")[0]
                if provider in {"postrocknation", "sputnikmusic", "bandcamp", "musicbrainz"}:
                    bins = formats.setdefault(provider, [set(), set()])
                    bins[rows[i]["release_type"] != "album"].add(value)
        # Distinct native release IDs can prove that an album and its title-track
        # single are separate releases. Synthetic wiki/URL keys must respect that.
        if any(albums and other and albums.isdisjoint(other) for albums, other in formats.values()):
            separate.update(indices)
    if separate or prn_singles:
        parents[:] = range(len(rows))
        seen.clear()
        # PRN's single object can be an album's title track. It can reclassify a
        # saved native PRN ID, but its name alone must not erase another album.
        link_rows(separate, isolate_singles=True)
    grouped = {}
    for i, row in enumerate(rows):
        grouped.setdefault(find(i), []).append(row)
    output = []
    reserved_ids = {row["id"] for row in rows if row.get("id")}
    assigned_ids = set()
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
        suffix = 0
        while not saved_ids and identifier in reserved_ids | assigned_ids:
            suffix += 1
            key = album_key(preferred["artist"], preferred["album"]) + "\0" + str(suffix)
            identifier = "pr_" + hashlib.sha256(key.encode()).hexdigest()[:16]
        assigned_ids.add(identifier)
        output.append({
            "id": identifier, "year": int(value[:4]) if value else preferred["year"],
            "artist": preferred["artist"], "album": preferred["album"],
            "styles": style_list([v for r in group for v in r["styles"]]),
            "lastfm_artist_tags": lastfm_tag_list([v for r in group
                if lastfm_artist_key(r["artist"]) == lastfm_artist_key(preferred["artist"])
                for v in r["lastfm_artist_tags"]]),
            "release_date": value, "date_status": status, "release_type": "album",
            "date_candidates": candidates,
            "source_ids": sorted({v for r in group for v in r["source_ids"]}),
            "source_urls": sorted({v for r in group for v in r["source_urls"]}),
            "notes": "\n".join(sorted(notes)),
        })
    return sorted(output, key=lambda r: (r["year"], r["release_date"] or "9999", r["artist"].casefold(), r["album"].casefold(), r["id"]))


def reconcile_bandcamp_references(observations, existing, corrections=()):
    """Reject cross-artist community links; split only fully refreshed false aliases."""
    fresh = [prepare(row) for row in observations]
    views = [apply_corrections(prepare(row), corrections) for row in fresh]
    by_id, bandcamp = {}, {}
    for raw, row in zip(fresh, views):
        for source_id in row["source_ids"]:
            by_id.setdefault(source_id, []).append(row)
        if any(re.fullmatch(r"bandcamp:[1-9]\d*", value) for value in row["source_ids"]):
            for value in row["source_ids"]:
                if value.startswith("bandcamp-url:"):
                    bandcamp[value] = (raw, row)
    mismatches = []
    for raw, row in zip(fresh, views):
        artist_alias = any("canonical_artist" in correction and matches(row, correction) for correction in corrections)
        if artist_alias or any(re.fullmatch(r"bandcamp:[1-9]\d*", v) for v in row["source_ids"]):
            continue
        for source_id in list(row["source_ids"]):
            pair = bandcamp.get(source_id)
            if pair and normalized(row["artist"]) != normalized(pair[1]["artist"]):
                primary_raw, primary = pair
                other_ids = set(row["source_ids"]) - {source_id}
                mismatches.append((source_id, other_ids, primary_raw, primary))
                url = source_id.removeprefix("bandcamp-url:")
                for target in (raw, row):
                    target["source_ids"].remove(source_id)
                    target["source_urls"] = [value for value in target["source_urls"] if value != url]
                    target["notes"] += "\nIgnored a Bandcamp link to a different artist: " + url
    retained = []
    for original in existing:
        old = apply_corrections(prepare(original), corrections)
        old_ids = set(old["source_ids"])
        for source_id, other_ids, primary_raw, primary in mismatches:
            if (source_id not in old_ids or not old_ids.intersection(other_ids) or
                    normalized(old["artist"]) != normalized(primary["artist"]) or
                    not all(value in by_id for value in old_ids)):
                continue
            claims = [row for value in old_ids for row in by_id[value]]
            dates = {value for row in claims for value in row["date_candidates"]}
            styles = {normalized(value) for row in claims for value in row["styles"]}
            urls = {value for row in claims for value in row["source_urls"]}
            if (not set(old["date_candidates"]).issubset(dates) or
                    not {normalized(v) for v in old["styles"]}.issubset(styles) or
                    not set(old["source_urls"]).issubset(urls)):
                continue
            replacement = dict(primary_raw, id=old["id"])
            notes = [line for line in old["notes"].splitlines() if not line.startswith(("Date conflict: ", "Other dates: "))]
            replacement["notes"] = "\n".join(notes + [primary_raw["notes"], "Previously combined with a different artist through a community Bandcamp link."])
            retained.append(replacement)
            break
        else:
            retained.append(original)
    return fresh, retained


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
        "release_type": kind, "source_ids": [f"bandcamp:{current['id']}", "bandcamp-url:" + bandcamp_url(url)],
        "source_urls": [bandcamp_url(url)], "notes": "", "tags": page.tags, "styles": style_list(page.tags),
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
            "styles": style_list([tag["name"] for tag in group.get("tags", [])]),
            "source_ids": ["musicbrainz:" + group["id"]],
            "source_urls": ["https://musicbrainz.org/release-group/" + group["id"]], "notes": "",
        })
    return output


def discogs_formats(payload):
    values = list(payload.get("format", []))
    for item in payload.get("formats", []):
        values += [item["name"]] + item.get("descriptions", [])
    return {normalized(value) for value in values}


def discogs_non_album(payload):
    kinds = discogs_formats(payload)
    title = payload.get("title", "")
    return bool(kinds & {"ep", "single", "compilation", "mixed", "unofficialrelease", "demo", "remix", "live"} or
                any(normalized(v.get("name", "")) in {"various", "variousartists"} for v in payload.get("artists", [])) or
                re.search(r"\blive\s*(?:$|@|at\b|in\b|from\b|session\b)|\(live\)|"
                          r"(?:^|[\s([])e\.?p\.?(?:\d+|$|[\s)\]])|\b(?:demos|remixes)\b", title, re.I))


def discogs_observation(payload, value, identifiers, url):
    credits = []
    for item in payload["artists"]:
        name = re.sub(r"\s+\(\d+\)$", "", item["name"]).strip()
        join = item.get("join", "").strip()
        credits.append(name + (" " + join + " " if join else " "))
    return prepare({"artist": "".join(credits).strip(), "album": payload["title"],
                    "release_date": value, "year": int(value[:4]), "date_status": "reported",
                    "release_type": "album", "styles": payload.get("styles") or [],
                    "source_ids": identifiers, "source_urls": [url], "notes": ""})


def parse_discogs_master(payload, year, release_ids=()):
    original_year = int(payload["year"])
    if not 1 <= original_year <= 9999 or int(payload["id"]) <= 0:
        raise ValueError("Discogs master has no valid original year or identifier")
    if original_year != year or discogs_non_album(payload):
        return None
    identifier = int(payload["id"])
    ids = [f"discogs-master:{identifier}"] + [f"discogs-release:{v}" for v in release_ids if int(v) > 0]
    # A main release is a representative edition, not evidence of the first day.
    return discogs_observation(payload, str(original_year), ids, f"https://www.discogs.com/master/{identifier}")


def parse_discogs_release(payload, year):
    kinds = discogs_formats(payload)
    if "album" not in kinds or kinds & {"reissue", "repress", "remastered"} or discogs_non_album(payload):
        return None
    if not any(normalized(v) == "postrock" for v in payload.get("styles", [])):
        return None
    value = normalize_date(payload.get("released") or payload.get("year"))
    if not value or int(value[:4]) != year:
        return None
    identifier = int(payload["id"])
    if identifier <= 0:
        raise ValueError("Discogs release has no valid identifier")
    return discogs_observation(payload, value, [f"discogs-release:{identifier}"],
                               f"https://www.discogs.com/release/{identifier}")


def collect_discogs(client, years):
    rows, errors = [], []
    for year in sorted(years):
        releases, seen, page, pages = {}, set(), 1, 1
        while page <= pages:
            try:
                query = urlencode({"type": "release", "style": "Post Rock", "format": "Album",
                                   "year": year, "per_page": 100, "page": page})
                payload = json.loads(client.get("https://api.discogs.com/database/search?" + query))
                results, pagination = payload["results"], payload["pagination"]
                if not isinstance(results, list) or int(pagination["page"]) != page:
                    raise ValueError("Unexpected Discogs search page structure")
                pages = int(pagination["pages"])
                ids = {int(item["id"]) for item in results}
                if ids and min(ids) <= 0 or page > 1 and not ids.difference(seen) or not results and int(pagination["items"]):
                    raise ValueError("Discogs pagination did not advance")
                seen.update(ids)
                for item in results:
                    if item.get("type") != "release" or str(item.get("year")) != str(year):
                        continue
                    if not any(normalized(v) == "postrock" for v in item.get("style", [])):
                        continue
                    if "album" in discogs_formats(item) and not discogs_non_album(item):
                        releases[int(item["id"])] = item
                items, per_page = int(pagination["items"]), int(pagination["per_page"])
                if items < 0 or per_page <= 0 or pages < 0:
                    raise ValueError("Invalid Discogs pagination metadata")
                expected_pages = (items + per_page - 1) // per_page
                if items and (pages != expected_pages or page > pages) or not items and (results or pages not in {0, 1}):
                    raise ValueError("Inconsistent Discogs pagination; source may be incomplete")
                if page == 1 or page % 5 == 0 or page == pages:
                    print(f"Discogs {year}: search page {page}/{pages}, {len(releases)} candidate editions", file=sys.stderr, flush=True)
                page += 1
            except (ValueError, KeyError, TypeError) as error:
                errors.append(f"Discogs {year} search page {page}: {error}. Set DISCOGS_TOKEN if API authentication is required.")
                break
        masters, standalone = {}, []
        for identifier, item in releases.items():
            try:
                master_id = int(item.get("master_id") or 0)
                if master_id < 0:
                    raise ValueError("Negative master identifier")
                if master_id > 0:
                    masters.setdefault(master_id, set()).add(identifier)
                else:
                    standalone.append(identifier)
            except (ValueError, TypeError) as error:
                errors.append(f"Discogs {year} release {identifier} master association: {error}")
        for index, identifier in enumerate(sorted(standalone), 1):
            try:
                payload = json.loads(client.get(f"https://api.discogs.com/releases/{identifier}"))
                if int(payload["id"]) != identifier:
                    raise ValueError("Discogs returned a different release")
                master_id = int(payload.get("master_id") or 0)
                if master_id < 0:
                    raise ValueError("Negative master identifier")
                if master_id > 0:
                    releases[identifier]["style"] = style_list(releases[identifier].get("style", []) + (payload.get("styles") or []))
                    masters.setdefault(master_id, set()).add(identifier)
                else:
                    row = parse_discogs_release(payload, year)
                    if row:
                        row["styles"] = style_list(row["styles"] + releases[identifier].get("style", []))
                        rows.append(row)
            except (ValueError, KeyError, TypeError) as error:
                errors.append(f"Discogs {year} release {identifier}: {error}")
            if index % 25 == 0:
                print(f"Discogs {year}: checked {index}/{len(standalone)} masterless releases", file=sys.stderr, flush=True)
        for index, (identifier, editions) in enumerate(sorted(masters.items()), 1):
            try:
                payload = json.loads(client.get(f"https://api.discogs.com/masters/{identifier}"))
                if int(payload["id"]) != identifier:
                    raise ValueError("Discogs returned a different master")
                row = parse_discogs_master(payload, year, sorted(editions))
                if row:
                    row["styles"] = style_list(row["styles"] + [v for rid in editions for v in releases[rid].get("style", [])])
                    rows.append(row)
            except (ValueError, KeyError, TypeError) as error:
                errors.append(f"Discogs {year} master {identifier}: {error}")
            if index % 25 == 0:
                print(f"Discogs {year}: checked {index}/{len(masters)} masters", file=sys.stderr, flush=True)
    return rows, errors


class WikiTables(HTMLParser):
    """Read actual cells and anchors, including nested formatting and literal pipes."""
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], [], [], None
        self.depth = 0
        self.anchor = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        if self.depth != 1:
            return
        if tag == "tr":
            self.row = []
        elif tag in {"th", "td"}:
            self.cell = {"text": [], "links": []}
        elif tag == "a" and self.cell is not None:
            self.anchor = (attrs.get("href", ""), len(self.cell["text"]))
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_data(self, value):
        if self.depth == 1 and self.cell is not None:
            self.cell["text"].append(value)

    def handle_endtag(self, tag):
        if self.depth == 1:
            if tag == "a" and self.anchor is not None and self.cell is not None:
                url, start = self.anchor
                self.cell["links"].append((url, "".join(self.cell["text"][start:]).strip()))
                self.anchor = None
            elif tag in {"th", "td"} and self.cell is not None:
                self.cell["text"] = " ".join("".join(self.cell["text"]).split())
                self.row.append(self.cell)
                self.cell = None
            elif tag == "tr" and self.row:
                self.table.append(self.row)
            elif tag == "table":
                self.tables.append(self.table)
        if tag == "table":
            self.depth = max(0, self.depth - 1)


def reddit_date(raw, year):
    raw = " ".join(raw.split())
    if re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", raw):
        value = normalize_date(raw)
    else:
        months = {name.casefold(): i for i in range(1, 13)
                  for name in (calendar.month_name[i], calendar.month_abbr[i])}
        exact = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", raw, re.I)
        if exact and exact[1].casefold() in months:
            value = normalize_date(f"{int(exact[3] or year):04d}-{months[exact[1].casefold()]:02d}-{int(exact[2]):02d}")
        else:
            known = {months[word.casefold()] for word in re.findall(r"[A-Za-z]+", raw) if word.casefold() in months}
            stated_years = {int(v) for v in re.findall(r"\b\d{4}\b", raw)}
            if stated_years and stated_years != {year}:
                raise ValueError(f"Reddit date outside requested year {year}: {raw!r}")
            value = f"{year:04d}-{next(iter(known)):02d}" if len(known) == 1 else str(year)
    if int(value[:4]) != year:
        raise ValueError(f"Reddit date outside requested year {year}: {raw!r}")
    return value


def parse_reddit(payload, year):
    if not isinstance(payload, dict) or payload.get("kind") != "wikipage" or not isinstance(payload.get("data"), dict):
        raise ValueError("Reddit returned no wiki page (authentication or API access may be required)")
    document = payload["data"].get("content_html")
    if not isinstance(document, str) or not document.strip():
        raise ValueError("Reddit wiki content_html is missing")
    if not re.search(r"<table\b", document, re.I):
        document = html.unescape(document)
    wiki_url = f"https://www.reddit.com/r/postrock/wiki/albums/{year}/"
    rows, found_table = [], False
    for table in WikiTables(document).tables:
        if not table or len(table[0]) < 3:
            continue
        header = [normalized(c["text"]) for c in table[0][:3]]
        if header[0] != "releasedate" or header[1] not in {"band", "artist"} or not header[2].startswith("album"):
            continue
        found_table = True
        for cells in table[1:]:
            if not any(c["text"] for c in cells):
                continue
            if len(cells) < 3 or not cells[1]["text"] or not cells[2]["text"]:
                raise ValueError("Malformed Reddit album table row")
            raw_date, artist, album_cell = cells[0]["text"], cells[1]["text"], cells[2]
            urls = [checked_url(urljoin(wiki_url, url)) for url, _ in album_cell["links"] if url]
            album = next((label for _, label in album_cell["links"] if label), album_cell["text"])
            kind, markers = "album", album_cell["text"]
            annotation_pattern = r"\((?:\d{4}\s+)?(single|ep|live|compilation|demos?|remix(?:es)?|reissue|re-release|remastered)\)"
            annotation = re.search(annotation_pattern, markers, re.I)
            if annotation:
                label = annotation[1].lower()
                kind = {"demos": "demo", "remixes": "remix", "re-release": "reissue", "remastered": "reissue"}.get(label, label)
            elif urls and all("/track/" in u for u in urls):
                kind = "single"
            elif re.search(r"(?:^|[\s([])e\.?p\.?(?:\d+|$|[\s)\]])", markers, re.I):
                kind = "ep"
            elif re.search(r"\blive\s*(?:$|@|at\b|in\b|from\b|session\b|ritual\b)", markers, re.I):
                kind = "live"
            elif re.search(r"\breissues?\b|\bre-?releases?\b|\bremaster(?:ed)?\b", markers, re.I):
                kind = "reissue"
            elif re.search(r"\bcompilation\b", markers, re.I) or normalized(artist) in {"various", "variousartists"}:
                kind = "compilation"
            elif re.fullmatch(r"demos?", markers, re.I) or re.search(r"\(demos?\)", markers, re.I):
                kind = "demo"
            album = re.sub(r"\s*" + annotation_pattern + r"\s*$", "", album, flags=re.I).strip()
            if not album:
                raise ValueError("Reddit row has a type annotation but no album title")
            value = reddit_date(raw_date, year)
            identifiers = [f"reddit-wiki:{year}:" + hashlib.sha256(album_key(artist, album).encode()).hexdigest()[:16]]
            # Only the wiki's album cell identifies an album page. Generic source links
            # may describe several works and must never become identity keys.
            for url in urls:
                try:
                    primary_url = bandcamp_url(url)
                except ValueError:
                    continue
                if urlsplit(primary_url).path.startswith("/album/"):
                    identifiers.append("bandcamp-url:" + primary_url)
            rows.append({
                "artist": artist, "album": album, "year": year, "release_date": value,
                "date_status": "reported", "release_type": kind,
                "source_ids": identifiers,
                "source_urls": sorted(set(urls + [wiki_url])),
                "notes": "Reddit date: " + raw_date if len(value) < 10 else "",
            })
    if not found_table:
        raise ValueError("Reddit album table missing (possibly a login/challenge page or changed markup)")
    return rows


def reddit_access_token(offline=False):
    token = os.environ.get("REDDIT_ACCESS_TOKEN", "").strip()
    if token:
        return token
    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id and not secret:
        return ""
    if not client_id or not secret:
        raise ValueError("Set both REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET")
    if offline:
        return ""
    request = Request("https://www.reddit.com/api/v1/access_token",
                      data=urlencode({"grant_type": "client_credentials", "scope": "wikiread"}).encode(),
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                               "Content-Type": "application/x-www-form-urlencoded"})
    credentials = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    request.add_unredirected_header("Authorization", "Basic " + credentials)
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read(100_000))
    token = payload.get("access_token")
    if not isinstance(token, str) or not token or payload.get("token_type", "").lower() != "bearer":
        raise ValueError("Reddit OAuth returned no bearer token; verify app credentials and Data API access")
    return token


def collect_reddit_browser(years, cache_dir, offline=False, refresh=False, headless=True, timeout=45):
    rows, errors, pending = [], [], []
    cache_dir = Path(cache_dir)

    def failed(year, cached, error):
        suffix = "; using stale wiki cache" if cached is not None else ""
        errors.append(f"Reddit browser {year}: {error}{suffix}")
        if cached is not None:
            rows.extend(cached)

    for year in sorted(years):
        path = cache_dir / "reddit-wiki" / f"{year}.json"
        url = f"https://www.reddit.com/r/postrock/wiki/albums/{year}/"
        cached = None
        try:
            if path.exists():
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                if snapshot["url"] != url or not isinstance(snapshot["observations"], list):
                    raise ValueError("Invalid browser wiki cache")
                cached = [prepare(r) for r in snapshot["observations"]]
                if any(r["year"] != year for r in cached):
                    raise ValueError("Browser wiki cache contains another year")
        except (ValueError, KeyError, TypeError, OSError):
            cached = None
        if cached is not None and (offline or not refresh and time.time() - path.stat().st_mtime < 86400):
            rows.extend(cached)
        elif offline:
            failed(year, None, "No valid cached browser wiki; run --reddit-login online first")
        else:
            pending.append((year, path, url, cached))
    if not pending:
        return rows, errors
    try:
        from reddit_browser import RedditBrowser
        with RedditBrowser(cache_dir.parent / "reddit-browser", headless=headless) as browser:
            for year, path, url, cached in pending:
                try:
                    print(f"Reddit browser {year}: opening wiki ({'headless' if headless else 'visible session setup'})", file=sys.stderr, flush=True)
                    document = browser.read_wiki(year, timeout=timeout)
                    batch = parse_reddit({"kind": "wikipage", "data": {"content_html": document}}, year)
                    # Persist parsed public release facts only, never full-page HTML or session state.
                    atomic_write(path, json.dumps({"url": url, "observations": batch}, ensure_ascii=False))
                    rows.extend(batch)
                    print(f"Reddit browser {year}: {len(batch)} release observations", file=sys.stderr, flush=True)
                except (ValueError, KeyError, TypeError, OSError) as error:
                    failed(year, cached, error)
    except (ImportError, ValueError, OSError) as error:
        for year, _, _, cached in pending:
            failed(year, cached, error)
    return rows, errors


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
        self.reddit_token = os.environ.get("REDDIT_ACCESS_TOKEN", "").strip()
        self.discogs_token = os.environ.get("DISCOGS_TOKEN", "").strip()
        if any(not 33 <= ord(c) <= 126 for c in self.discogs_token):
            raise ValueError("DISCOGS_TOKEN contains invalid characters")

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
            interval = 2.5 if urlsplit(url).hostname == "api.discogs.com" and not self.discogs_token else 1.1
            time.sleep(max(0, interval - (time.monotonic() - self.last_request)))
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
                    if urlsplit(url).hostname == "oauth.reddit.com" and self.reddit_token:
                        # Never carry the token to a redirect target or persist it in cache metadata.
                        request.add_unredirected_header("Authorization", "Bearer " + self.reddit_token)
                    if urlsplit(url).hostname == "api.discogs.com" and self.discogs_token:
                        request.add_unredirected_header("Authorization", "Discogs token=" + self.discogs_token)
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
        return bool(re.search(r"<title[^>]*>[^<]*(?:Client Challenge|Just a moment|Prove your humanity)|"
                              r"name=[\"']js_challenge[\"']|you've been blocked by network security", body, re.I))


def collect_sources(config, years, client, seeds, existing=()):
    rows, errors = list(seeds), []
    if config.get("postrocknation", False):
        from postrocknation import collect_postrocknation
        batch, warnings = collect_postrocknation(client, years, existing)
        rows.extend(batch)
        errors.extend(warnings)
    if config.get("sputnikmusic", False):
        from sputnik import collect_sputnik
        batch, warnings = collect_sputnik(client, years, existing)
        rows.extend(batch)
        errors.extend(warnings)
    if config.get("discogs", False):
        batch, warnings = collect_discogs(client, years)
        rows.extend(batch)
        errors.extend(warnings)
    reddit_mode = config.get("reddit_mode", "api")
    if reddit_mode == "auto":
        reddit_mode = "api" if os.environ.get("REDDIT_ACCESS_TOKEN") or os.environ.get("REDDIT_CLIENT_ID") else "browser"
    if config.get("reddit", False) and reddit_mode == "browser":
        batch, warnings = collect_reddit_browser(years, client.cache_dir,
                                                 offline=getattr(client, "offline", False),
                                                 refresh=getattr(client, "refresh", False),
                                                 timeout=config.get("reddit_timeout", 45))
        rows.extend(batch)
        errors.extend(warnings)
    elif config.get("reddit", False) and reddit_mode == "api":
        try:
            client.reddit_token = reddit_access_token(offline=getattr(client, "offline", False))
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append(f"Reddit authentication: {error}")
            reddit_years = []
        else:
            reddit_years = sorted(years)
        for year in reddit_years:
            try:
                host = "oauth.reddit.com" if client.reddit_token or os.environ.get("REDDIT_CLIENT_ID") else "www.reddit.com"
                suffix = "" if host == "oauth.reddit.com" else ".json"
                url = f"https://{host}/r/postrock/wiki/albums/{year}{suffix}?raw_json=1"
                batch = parse_reddit(json.loads(client.get(url)), year)
                rows.extend(batch)
                print(f"Reddit {year}: {len(batch)} release observations", file=sys.stderr, flush=True)
            except (ValueError, KeyError, TypeError) as error:
                errors.append(f"Reddit {year}: {error}. Configure REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET or REDDIT_ACCESS_TOKEN with wikiread access if anonymous reads are blocked.")
    elif config.get("reddit", False):
        errors.append("Reddit: reddit_mode must be auto, browser or api")
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
    selected = list(rows) + [r for r in existing if int(r["year"]) in years]
    for row in selected:
        if row.get("release_type", "album") in NON_ALBUMS:
            continue
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
            if reader.fieldnames not in (FIELDS, PREVIOUS_FIELDS, LEGACY_FIELDS):
                raise ValueError(f"Unexpected CSV columns in {path}; use --input to import other formats")
            rows.extend(prepare(r) for r in reader)
    return rows


def enrich_cached_discogs_styles(rows, cache_dir):
    """Reuse styles of known records even when discovery filters no longer match."""
    errors = []
    for row in rows:
        labels = list(row["styles"])
        for source_id in row["source_ids"]:
            match = re.fullmatch(r"discogs-(master|release):([1-9][0-9]*)", source_id)
            if not match:
                continue
            kind, identifier = match.groups()
            url = f"https://api.discogs.com/{'masters' if kind == 'master' else 'releases'}/{identifier}"
            path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            if not path.exists():
                continue
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                payload = json.loads(cached["body"])
                if cached["url"] != url or int(payload["id"]) != int(identifier):
                    raise ValueError("Cached Discogs source identity mismatch")
                labels += string_list(payload.get("styles") or [])
            except (ValueError, KeyError, TypeError, OSError) as error:
                errors.append(f"Discogs styles {source_id}: {error}")
        row["styles"] = style_list(labels)
    return errors


def retained_exclusions(existing, observations):
    """Keep source-reported non-albums from returning after a partial refresh."""
    records = {}
    for record in list(existing) + list(observations):
        row = prepare(record)
        if row["release_type"] not in NON_ALBUMS:
            continue
        key = (album_key(row["artist"], row["album"]), row["release_type"], tuple(row["source_ids"]))
        records[key] = {field: row[field] for field in FIELDS if field not in {"id", "lastfm_artist_tags"}}
    return [records[key] for key in sorted(records)]


def md_text(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def source_link(url):
    checked_url(url)
    host = urlsplit(url).hostname
    name = "Bandcamp" if host.endswith(".bandcamp.com") else "MusicBrainz" if host == "musicbrainz.org" else "Discogs" if host.endswith("discogs.com") else "AOTY" if host.endswith("albumoftheyear.org") else "Reddit" if host.endswith("reddit.com") else "Sputnikmusic" if host == "www.sputnikmusic.com" else "Post-Rock Nation" if host == "postrocknation.com" or host.endswith(".postrocknation.com") else host
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
                  "| Date | Artist | Album | Styles | Last.fm artist tags | Date status | Sources | Notes |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in sorted(buckets[month], key=lambda r: (r["release_date"], r["artist"].casefold(), r["album"].casefold())):
            cells = [row["release_date"] or "Unresolved", row["artist"], row["album"],
                     ", ".join(style_list(row.get("styles", []))) or "—",
                     ", ".join(lastfm_tag_list(row.get("lastfm_artist_tags", []))) or "—", row["date_status"]]
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
    parser.add_argument("--offline", action="store_true", help="Use cached source data only; no browser or network")
    parser.add_argument("--refresh", action="store_true", help="Ignore the 24-hour source cache lifetime")
    parser.add_argument("--render-only", action="store_true", help="Regenerate Markdown from existing CSV without changing CSV")
    parser.add_argument("--lastfm-only", action="store_true", help="Refresh Last.fm artist tags on saved albums without fetching release sources")
    parser.add_argument("--reddit-browser", action="store_true", help="Read Reddit wiki with headless Chrome instead of the API")
    parser.add_argument("--reddit-login", action="store_true", help="Open dedicated visible Chrome for sign-in and cache the wiki; do not edit catalogs")
    parser.add_argument("--reddit-timeout", type=int, help="Seconds per browser wiki (default 45; session setup 300)")
    args = parser.parse_args(argv)
    try:
        for year in args.year:
            date(year, 1, 1)
        if args.lastfm_only and (args.input or args.render_only or args.reddit_login):
            raise ValueError("--lastfm-only cannot be combined with --input, --render-only or --reddit-login")
        if args.reddit_timeout is not None and not 1 <= args.reddit_timeout <= 600:
            raise ValueError("--reddit-timeout must be between 1 and 600 seconds")
        if args.reddit_login:
            if args.offline or args.input or args.render_only:
                raise ValueError("--reddit-login cannot be combined with --offline, --input or --render-only")
            print("A separate Chrome profile will open. Sign in or complete any access check yourself, then return to the wiki tab.", flush=True)
            _, errors = collect_reddit_browser(set(args.year), args.cache_dir, refresh=True,
                                               headless=False, timeout=args.reddit_timeout or 300)
            for error in errors:
                print("Warning: " + error, file=sys.stderr)
            if not errors:
                print("Reddit browser session and public wiki cache are ready; run --reddit-browser to update catalogs.")
            return 1 if errors else 0
        existing = read_catalog(args.output_dir)
        if args.lastfm_only:
            from lastfm import enrich
            errors = enrich(existing, set(args.year), args.cache_dir, args.offline, args.refresh)
            write_catalog(args.output_dir, existing, args.year)
            for error in errors:
                print("Warning: " + error, file=sys.stderr)
            return 1 if errors else 0
        if args.render_only:
            for year in args.year:
                if not (args.output_dir / f"{year:04d}.csv").exists():
                    raise ValueError(f"No CSV for {year}")
                atomic_write(args.output_dir / f"{year:04d}.md", render_markdown(year, existing))
            return 0
        corrections = load_list(args.corrections)
        validate_corrections(corrections)
        exclusions_path = args.output_dir / "excluded.json"
        excluded = load_list(exclusions_path) if exclusions_path.exists() else []
        errors = []
        if args.input:
            observations = load_list(args.input)
        else:
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if args.reddit_browser:
                config.update(reddit=True, reddit_mode="browser")
            if args.reddit_timeout is not None:
                config["reddit_timeout"] = args.reddit_timeout
            seeds = load_list(args.config.parent / config.get("seed_file", "seeds.json"))
            seeds = [r for r in seeds if int(r.get("year") or r["release_date"][:4]) in args.year]
            client = HTTPClient(args.cache_dir, args.offline, args.refresh)
            observations, errors = collect_sources(config, set(args.year), client, seeds, existing)
            errors += client.errors
        observations, existing = reconcile_bandcamp_references(observations, existing, corrections)
        rows = merge_catalog(list(observations) + excluded, existing, corrections)
        errors += enrich_cached_discogs_styles(rows, args.cache_dir)
        if not args.input and config.get("lastfm_artist_tags", False):
            from lastfm import enrich
            errors += enrich(rows, set(args.year), args.cache_dir, args.offline, args.refresh)
        if not rows and errors and not existing and not observations:
            raise ValueError("All sources failed and there is no existing catalog to preserve")
        excluded = retained_exclusions(excluded, observations)
        write_catalog(args.output_dir, rows, args.year)
        atomic_write(exclusions_path, json.dumps(excluded, ensure_ascii=False, indent=2) + "\n")
        for error in errors:
            print("Warning: " + error, file=sys.stderr)
        return 1 if errors else 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
