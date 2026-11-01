"""Public Sputnikmusic post-rock listing and catalog metadata; standard library only."""

import calendar
from datetime import date
from html.parser import HTMLParser
import re
import sys
from urllib.parse import parse_qs, urljoin, urlsplit


BASE = "https://www.sputnikmusic.com"
LISTING_URL = BASE + "/newreleases.php?t=6"
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
SECTIONS = {"LPs": "album", "EPs": "ep", "Live Albums": "live", "Compilations": "compilation",
            "Singles": "single", "Demos": "demo", "Remixes": "remix"}


def native_path(href):
    parts = urlsplit(urljoin(BASE + "/", href))
    if parts.scheme not in {"http", "https"} or parts.hostname not in {"www.sputnikmusic.com", "sputnikmusic.com"}:
        return None
    return parts


def album_id(href):
    parts = native_path(href)
    if not parts:
        return None
    match = re.fullmatch(r"/album/([1-9]\d*)(?:/.*)?", parts.path)
    if match:
        return int(match[1])
    if parts.path == "/soundoff.php":
        values = parse_qs(parts.query).get("albumid", [])
        if len(values) == 1 and re.fullmatch(r"[1-9]\d*", values[0]):
            return int(values[0])
    return None


class Page(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.styles = [], [], []
        self.selected_genre = None
        self.month, self.kind = "", "album"
        self.hidden = self.tables = self.strong = self.lists = 0
        self.discography, self.genre_lists = [], []
        self.selects = []
        self.group, self.option, self.anchor = "", None, None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.tables += 1
            if "plaincontentbox" in (attrs.get("class") or "").split():
                self.discography.append(self.tables)
        if tag == "ul":
            self.lists += 1
            if "tags" in (attrs.get("class") or "").split():
                self.genre_lists.append(self.lists)
        if tag == "strong":
            self.strong += 1
        if tag == "select":
            self.selects.append(attrs)
        if tag == "optgroup":
            self.group = attrs.get("label", "").upper()
        if tag == "option":
            self.option = {"attrs": attrs, "group": self.group, "text": []}
        if tag == "a":
            self.anchor = {"href": attrs.get("href", ""), "text": [], "month": self.month,
                           "kind": self.kind, "discography": bool(self.discography),
                           "genre_tag": bool(self.genre_lists)}

    def handle_data(self, value):
        if self.hidden or not value.strip():
            return
        value = " ".join(value.split())
        if self.anchor is not None:
            self.anchor["text"].append(value)
        if self.option is not None:
            self.option["text"].append(value)
        if not self.selects:
            self.text.append(value)
            match = re.fullmatch(r"([A-Za-z]+) ([1-9]\d{3})", value)
            if match and match[1].lower() in MONTHS and self.anchor is None:
                self.month = f"{int(match[2]):04d}-{MONTHS[match[1].lower()]:02d}"
            if self.discography and self.strong and value in SECTIONS:
                self.kind = SECTIONS[value]

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if self.hidden:
            return
        if tag == "a" and self.anchor is not None:
            self.anchor["text"] = " ".join(self.anchor["text"])
            self.links.append(self.anchor)
            self.anchor = None
        if tag == "option" and self.option is not None:
            option = self.option
            label = " ".join(option["text"])
            if option["group"] == "EXISTING TAGS" and option["attrs"].get("value"):
                self.styles.append(re.sub(r"\s*\(\d+ votes?\)$", "", label).strip())
            if self.selects and self.selects[-1].get("id") == "genreselect" and "selected" in option["attrs"]:
                self.selected_genre = (parse_qs(urlsplit(option["attrs"].get("value", "")).query).get("t"), label)
            self.option = None
        if tag == "optgroup":
            self.group = ""
        if tag == "select" and self.selects:
            self.selects.pop()
        if tag == "strong":
            self.strong = max(0, self.strong - 1)
        if tag == "table":
            self.discography = [level for level in self.discography if level < self.tables]
            self.tables = max(0, self.tables - 1)
        if tag == "ul":
            self.genre_lists = [level for level in self.genre_lists if level < self.lists]
            self.lists = max(0, self.lists - 1)


def parse_listing(document):
    page = Page(document)
    if page.selected_genre != (["6"], "Post Rock"):
        raise ValueError("Sputnikmusic listing is not the selected Post Rock genre")
    found = {}
    for link in page.links:
        identifier = album_id(link["href"])
        if identifier and link["month"]:
            found.setdefault(identifier, set()).add(link["month"])
    if not found:
        raise ValueError("Sputnikmusic listing exposes no dated release links; markup may have changed")
    return found


def parse_album(document, identifier):
    page = Page(document)
    identity, band_url = None, None
    for link in page.links:
        parts = native_path(link["href"])
        if not parts:
            continue
        if re.fullmatch(r"/bands/[^/]+/[1-9]\d*/?", parts.path) and band_url is None:
            band_url = BASE + parts.path
        if parts.path == "/digging.php":
            query = parse_qs(parts.query)
            if query.get("a") == [str(identifier)] and query.get("band_name") and query.get("album_name"):
                identity = (query["band_name"][0], query["album_name"][0])
    match = re.search(r"\bRelease Date:\s*([1-9]\d{3})\b", " ".join(page.text))
    if not identity or not all(value.strip() for value in identity) or not band_url or not match:
        raise ValueError("Sputnikmusic album lacks matching identity, artist link or release year")
    year = int(match[1])
    date(year, 1, 1)
    return {"artist": identity[0], "album": identity[1], "year": year, "band_url": band_url}


def parse_band(document):
    page = Page(document)
    styles, types = list(page.styles), {}
    for link in page.links:
        parts = native_path(link["href"])
        if parts and link["genre_tag"] and re.fullmatch(r"/genre/[1-9]\d*/[^/]+/?", parts.path) and link["text"]:
            styles.append(link["text"])
        identifier = album_id(link["href"])
        if identifier and link["discography"] and link["text"]:
            types[identifier] = link["kind"]
    if not types:
        raise ValueError("Sputnikmusic artist page exposes no discography entries")
    return sorted(set(styles)), types


def collect_sputnik(client, years, existing=()):
    rows, errors, candidates, bands, saved_years = [], [], {}, {}, {}
    try:
        listed = parse_listing(client.get(LISTING_URL))
        candidates.update((identifier, months) for identifier, months in listed.items()
                          if any(int(month[:4]) in years for month in months))
    except (ValueError, KeyError, TypeError, OSError) as error:
        errors.append(f"Sputnikmusic listing: {error}")
    for row in existing:
        if int(row["year"]) in years:
            for source_id in row.get("source_ids", []):
                match = re.fullmatch(r"sputnikmusic:([1-9]\d*)", source_id)
                if match:
                    identifier = int(match[1])
                    candidates.setdefault(identifier, set())
                    saved_years[identifier] = int(row["year"])
    for identifier, months in sorted(candidates.items()):
        url = BASE + "/soundoff.php?albumid=" + str(identifier)
        try:
            record = parse_album(client.get(url), identifier)
            if record["year"] not in years and identifier not in saved_years:
                continue
            band_url = record.pop("band_url")
            if band_url not in bands:
                bands[band_url] = parse_band(client.get(band_url))
            styles, types = bands[band_url]
            if identifier not in types:
                raise ValueError("Album ID is absent from its artist's discography")
            value = str(record["year"])
            if len(months) == 1 and next(iter(months)).startswith(value + "-"):
                value = next(iter(months))
            kind = types[identifier]
            if kind == "album":
                for label, pattern in (("ep", r"\bEP\b|\bE\.P\."), ("single", r"\(single\)|\[single\]"),
                                       ("live", r"\(live\)|\blive (?:at|in|from)\b"),
                                       ("reissue", r"\b(?:reissue|repress|remastered)\b")):
                    if re.search(pattern, record["album"], re.I):
                        kind = label
                        break
            record.update(release_date=value, date_candidates=[str(record["year"]), value],
                          date_status="reported", release_type=kind,
                          styles=styles + ["Post Rock"], source_ids=[f"sputnikmusic:{identifier}"],
                          source_urls=[url, band_url, LISTING_URL], notes="Sputnikmusic genres are artist-level tags.")
            # Keep the discovery bucket when an upstream year changes, and let the
            # shared merger retain the contradictory original-date claim.
            record["year"] = saved_years.get(identifier, record["year"])
            rows.append(record)
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append(f"Sputnikmusic album {identifier}: {error}")
    print(f"Sputnikmusic: {len(rows)} release observations", file=sys.stderr, flush=True)
    return rows, errors
