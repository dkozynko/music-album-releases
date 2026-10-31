# Music album releases

A personal post-rock album calendar: **one album per row**, a script-owned CSV for each year, and Markdown rendered from that CSV.

- [2026 calendar](releases/post-rock/2026.md) · [CSV](releases/post-rock/2026.csv)
- [2025 calendar](releases/post-rock/2025.md) · [CSV](releases/post-rock/2025.csv)

## Run

Requires **Python 3.10+**. Bandcamp uses the system **curl** when available; otherwise Python's HTTP client is used and may receive access challenges. No Python packages, database, API keys, or server to install.

```sh
python3 update.py --year 2026
```

Several years in one run:

```sh
python3 update.py --year 2025 2026
```

Without `--year`, the updater uses the current year. Run it again to discover releases and refresh metadata. It writes `releases/post-rock/YYYY.csv` and `YYYY.md` and retains previous albums when a source disappears.

```sh
python3 update.py --year 2026 --offline       # Cached source responses only
python3 update.py --year 2026 --refresh       # Refresh HTTP cache now
python3 update.py --year 2026 --render-only   # Markdown from CSV; no network or CSV edits
python3 -m unittest discover -s tests -v
```

The first online run takes longer because it reads individual Bandcamp album pages. Public HTTP responses are cached in ignored `.cache/http/` for 24 hours. Requests are sequential, at least 1.1 seconds apart; temporary rate limits are retried. A 403 is reported, never bypassed.

If a source fails, existing albums remain, successful sources can still update the files, and the command exits **1** with warnings. A clean update exits **0**. Malformed local input or a missing corrections file aborts before outputs are written. Individual output files are replaced atomically. Run one updater at a time.

## Sources and coverage

1. **MusicBrainz:** release groups with the `post-rock` tag, primary type Album, and first-release dates in the requested years. Editions are grouped at the source. Live albums, compilations, remixes and other non-album secondary types are excluded; soundtrack albums are allowed.
2. **Bandcamp:** public album pages exposed by the artist/label catalogs in [`sources.json`](sources.json). Catalog discoveries need a post-rock tag. Explicit album URLs and seed-linked albums are curated post-rock selections and can have different tags on Bandcamp. Saved albums' linked Bandcamp pages are refreshed too, including dates that contradict the selected year.
3. **Starting observations:** [`seeds.json`](seeds.json) contains bibliographic facts and source links researched on 2026-10-31. It supplements ongoing discovery with October releases found through community/catalog sources. Reddit, AOTY and Discogs seed links are **not automatically crawled**. Reddit's public wiki API returned 403 during development.

This is a growing personal selection, **not a complete genre census**. MusicBrainz tagging is incomplete; Bandcamp coverage depends on the configured catalogs and the albums exposed by their public pages. Add more catalogs or explicit album URLs in `sources.json` to expand discovery.

On 2026-10-31, Our Ceasing Voice's Bandcamp album URL and custom music domain redirect to each other. The updater reports this source failure and retains the researched observation; it does not treat the failed refresh as an empty album. This also means a cached-only update currently exits 1 for that uncached page.

## Dates and duplicates

The updater reads Bandcamp's **album release date**, not page publication, upload time or vinyl shipping dates. MusicBrainz supplies its release group's **first-release date**. Those claims can still be wrong, so provenance and disagreements are retained.

| CSV `date_status` | Meaning |
| --- | --- |
| `primary` | Date stated on an artist/label album page or a linked primary announcement |
| `reported` | Date from a catalog/community observation |
| `corrected` | A manually pinned date with a supporting source URL |
| `conflict` | Sources disagree; only agreed date precision is displayed |
| `unknown` | No usable release date |

`2026`, `2026-10`, and `2026-10-16` have different precision. Missing components are never filled with January or day 1. Conflicting October days produce `2026-10`; conflicting years leave the date unresolved in its discovery-year bucket. The original claims remain in `date_candidates`. Future dates are announcements, not guarantees.

Albums merge by normalized artist/title, shared **nonzero** source identifiers, or explicit aliases. `S/T` maps to a self-titled album. There is no automatic fuzzy matching. Different works with identical names may require a correction using a `source_id` and a disambiguated canonical title.

## Corrections

Edit [`corrections.json`](corrections.json), then rerun the updater. **Do not edit generated CSV/Markdown directly.** Corrections are reapplied on every run.

```json
{
  "artist": "Example Act",
  "album": "Album title",
  "release_date": "2011-10-26",
  "source_url": "https://example.bandcamp.com/album/original",
  "note": "The 2026 date belongs to a repress."
}
```

Match with `artist` + `album` or a `source_id` such as `musicbrainz:…` or `bandcamp:123`. Prefer a source identifier when upstream spelling may change. The generated CSV `id` is read-only and cannot match corrections. Optional fields:

- `canonical_artist`, `canonical_album`: aliases and spelling/title corrections.
- `release_date` + `source_url`: pin an original date; this can move a record to another year's files.
- `release_type`: `album`, `ep`, `single`, `live`, `compilation`, `remix`, `demo`, or `reissue`. Only albums are output.
- `exclude: true`: exclude the whole matched album.
- `note`: explain the decision.

The CSV is persistent state: old source claims and conflicts remain available through partial updates, including after a date is corrected. Resolve an obsolete/conflicting date with a sourced correction rather than silently replacing it.

## Import other sources

Import a JSON array of observations without network access:

```sh
python3 update.py --year 2026 --input observations.json
```

```json
[
  {
    "artist": "Example Act",
    "album": "Album title",
    "year": 2026,
    "release_date": "2026-10",
    "date_status": "reported",
    "release_type": "album",
    "source_ids": [],
    "source_urls": ["https://example.bandcamp.com/album/title"],
    "notes": "Day not announced."
  }
]
```

The imported observations merge with saved rows and corrections. List-valued CSV fields (`date_candidates`, `source_ids`, `source_urls`) are JSON arrays inside standard UTF-8 CSV cells. Additional years present in imported/corrected data receive their own files.
