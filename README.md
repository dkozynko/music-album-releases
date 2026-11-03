# Music album releases

A personal post-rock album calendar: **one album per row**, a script-owned CSV for each year, and Markdown rendered from that CSV.

The `styles` column includes all available Discogs styles, MusicBrainz tags, Bandcamp tags, Post-Rock Nation artist tags and Sputnikmusic artist genre tags (including all voted tags, not only the displayed top three). Tags from matching albums and editions are combined, sorted and deduplicated across case/spacing/hyphen variations. Already-cached Discogs metadata also supplies tags for saved albums that no longer match discovery filters. Source labels are preserved; Bandcamp's free-form tags can also be descriptive or geographic. Missing tags remain `[]` in CSV and `—` in Markdown. Earlier CSV files without this column are read and upgraded automatically on the next update.

The separate `lastfm_artist_tags` column contains **only the artist/band's Last.fm tags**, scraped from its public artist tag pages using headless Chrome. These are not album tags and are never mixed into `styles`. No Last.fm login, API or API key is used. Earlier CSV schemas are upgraded automatically.

- [2026 calendar](releases/post-rock/2026.md) · [CSV](releases/post-rock/2026.csv)
- [2025 calendar](releases/post-rock/2025.md) · [CSV](releases/post-rock/2025.csv)

## Run

Requires **Python 3.10+**. The core updater uses the standard library, with no database or server. Bandcamp uses the system **curl** when available; otherwise Python's HTTP client is used and may receive access challenges. Reddit's browser mode additionally needs **Playwright and Google Chrome**; see the setup below. An API mode is also available.

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
python3 update.py --year 2026 --refresh       # Refresh source caches now
python3 update.py --year 2026 --render-only   # Markdown from CSV; no network or CSV edits
.venv/bin/python update.py --year 2025 2026 --lastfm-only  # Refresh only saved artists' Last.fm tags
python3 -m unittest discover -s tests -v
```

The first online run takes longer because it reads individual Discogs records and Bandcamp album pages. Public HTTP responses are cached in ignored `.cache/http/` for 24 hours. Requests are sequential, at least 1.1 seconds apart; unauthenticated Discogs requests are spaced 2.5 seconds apart to stay below 25/minute. Temporary rate limits are retried. A 403 is reported, never bypassed.

If a source fails, existing albums remain, successful sources can still update the files, and the command exits **1** with warnings. A clean update exits **0**. Malformed local input or a missing corrections file aborts before outputs are written. Source-reported exclusions persist in script-owned `releases/post-rock/excluded.json`, so an EP cannot return from an old seed when Reddit later fails. Individual output files are replaced atomically. Run one updater at a time.

## Daily automatic updates

The [GitHub Actions workflow](.github/workflows/update-releases.yml) runs daily at **07:17 Europe/Warsaw**, including daylight-saving changes. It updates the current and previous years (2025 and 2026 during 2026), then commits changed CSV, Markdown and exclusion state directly to `main` as `github-actions[bot]`. Unchanged files produce no commit. The repository's built-in `GITHUB_TOKEN` supplies write access; no personal token or local computer is required. You can also start it with **Actions → Update album releases → Run workflow** on `main`.

The runner installs Python, Playwright and Chrome, runs the tests, and reuses only public HTTP/wiki response caches. Browser profiles and login sessions are never uploaded. Source warnings appear in the job summary; successful partial updates are still published with previous data retained for unavailable sources. Fatal updater errors stop publication. Simultaneous updates are serialized, and pushes retry after rebasing without force-pushing. The first uncached run can take considerably longer because source rate limits are respected.

All configured sources are attempted without required secrets. Optional repository Actions secrets `DISCOGS_TOKEN`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `REDDIT_ACCESS_TOKEN` are passed only to the updater if provided. Reddit can still deny access from GitHub-hosted runners; this produces a source warning and preserves existing Reddit observations rather than waiting for a person.

GitHub can delay scheduled jobs and disables public-repository schedules after 60 days without repository activity. Regular catalog commits keep the repository active; after an unusually long unchanged period the schedule may need re-enabling. See [GitHub's scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Sources and coverage

1. **Discogs:** all release-search pages for the requested years, with exact local `Post Rock` style and `Album` checks. Editions with a nonzero master ID are grouped; **masterless releases are included** and their detail-level master association is rechecked. A master's original year must match the requested year. Masterless records explicitly marked Reissue, Repress, Remastered, EP, Single, Compilation or Unofficial Release are skipped, as are explicitly identified live/demo/remix titles and Various-artist compilations. Reissue is an edition flag: it never excludes a whole valid master. Enabled by `"discogs": true` in [`sources.json`](sources.json).
2. **Reddit:** the complete yearly [r/postrock release wiki](https://www.reddit.com/r/postrock/wiki/albums/2026/), fetched by the script for **every requested year**, not a manually selected month. It reads rendered HTML tables using headless Chrome or the official wiki API, preserves fuzzy dates, and filters explicitly identified singles, EPs, live albums, demos, compilations and reissues. Bandcamp album links from the wiki are refreshed automatically too. Enabled by `"reddit": true` in [`sources.json`](sources.json).
3. **Sputnikmusic:** the public [Post Rock new-release list](https://www.sputnikmusic.com/newreleases.php?t=6), linked album pages and artist discographies. Enabled by `"sputnikmusic": true` in [`sources.json`](sources.json); no login, API key, browser or extra package is required. See coverage and date precision below.
4. **Post-Rock Nation:** all pages of the public [Release Archive](https://postrocknation.com/page/release-archive) spanning the requested years, plus assigned tags from linked artist profiles. Enabled by `"postrocknation": true` in [`sources.json`](sources.json); no login, browser or extra package is required. See pagination and date behavior below.
5. **MusicBrainz:** release groups with the `post-rock` tag, primary type Album, and first-release dates in the requested years. Editions are grouped at the source. Live albums, compilations, remixes and other non-album secondary types are excluded; soundtrack albums are allowed.
6. **Bandcamp:** public album pages exposed by the artist/label catalogs in [`sources.json`](sources.json). Catalog discoveries need a post-rock tag. Explicit album URLs, wiki album links and seed-linked albums are curated post-rock selections and can have different tags on Bandcamp. Saved albums' linked Bandcamp pages are refreshed too, including dates that contradict the selected year.
7. **Starting observations:** [`seeds.json`](seeds.json) preserves earlier bibliographic research and its source links. It is not the Reddit, Discogs, Sputnikmusic or Post-Rock Nation import mechanism. New releases from those sources require no edits to this file. AOTY seed links are not automatically crawled.

This is a growing personal selection, **not a complete genre census**. MusicBrainz tagging is incomplete; Bandcamp coverage depends on the configured catalogs and the albums exposed by their public pages. Reddit mixes albums and shorter releases and may omit type annotations. The updater reports source failures; a failed wiki refresh is never counted as successful coverage.

### Last.fm artist tags

`"lastfm_artist_tags": true` in [`sources.json`](sources.json) enables artist-tag enrichment after albums are merged. The scraper opens each band's public `/music/<artist>/+tags` page in the same headless Chrome implementation used for Reddit, with a separate temporary profile. It reads only the artist's global tag list and follows same-artist next-page links. Artist punctuation is preserved, and URLs are encoded; it does not guess tags from albums, related artists, descriptions or other release sources.

Complete results are cached per artist for 24 hours in ignored `.cache/http/lastfm-artists/` and shared across that artist's albums. The daily GitHub Actions job already installs Chrome/Playwright and saves this public cache, so no additional setup or secret is required. Use `--lastfm-only` for a focused refresh without rescanning release catalogs, or select the `lastfm_only` checkbox in GitHub's **Run workflow** form. `--offline` uses only parsed cache, and `--refresh` ignores its age. `--render-only` never opens a browser. This source needs the optional browser dependencies described under Reddit access.

A successful refresh replaces the saved Last.fm tag list, including an explicitly empty list. Inaccessible pages, service errors, changed markup and incomplete pagination preserve previous tags and produce source warnings. After three service/challenge failures the scraper stops the batch and retries on the next run. If Last.fm has not yet supplied tags, the column remains `[]` / `—`; it is not filled from `styles`. Only parsed artist names, public URLs and tags are cached, never HTML, cookies or browser profiles. Last.fm can combine different bands with the same name on one artist page; those are the site's shared artist tags.

### Post-Rock Nation archive

The archive table is available in public HTML even when the page includes a login prompt. The importer follows `?page=N`, validates row ranges/counts and unique native album IDs, and checks newest-year-first ordering. The site's filter/per-page URL parameters are ignored, so the script filters years and release types locally and stops once it crosses below the earliest requested year. A repeated/changed page produces an incomplete-source warning while preserving already-read observations and the existing catalog.

Source album/single classifications and explicit EP/live/demo/remix/reissue title markers determine eligibility. Catalog dates retain their stated day/month/year precision and use `reported` status; conflicting primary dates remain visible. Saved `postrocknation:<album-id>` records are refreshed through their detail pages if they disappear from the scanned year range, including when the upstream year changes. Generic archive and artist pages are provenance, never album identity keys.

The `styles` column includes every assigned label in the artist profile's `TAGS:` group. These describe the artist, not necessarily the individual album. Profiles are cached and shared across that artist's releases; unavailable profiles produce warnings and leave the release facts intact. Unassigned menus, descriptions, track durations and login text do not become tags or dates. HTTP caching, `--offline` and `--refresh` work as for the other HTTP sources.

### Sputnikmusic dates and coverage

The supplied page is a rolling list of recent months, with no exposed yearly archive or pagination controls. The importer reads every release link currently shown under the selected Post Rock filter, then refreshes already-saved Sputnikmusic IDs for the requested years even after they leave that list. Older-year discovery is limited to what the current page actually exposes; it does not claim full historical coverage. Existing CSV rows survive page changes and failed refreshes.

Album pages supply the stored artist/title and a catalog release year; that name takes precedence over the listing label. A title already shortened in Sputnikmusic's database is retained without guessing. A compatible listing month refines the date to `YYYY-MM`. Without a compatible month, it remains `YYYY`. Rating dates, review dates and the current date never supply a release day. An older catalog year is never promoted to the listing's newer year. Changed years on saved Sputnikmusic IDs still reach the shared date-conflict handling.

Artist discographies distinguish LPs, EPs, live albums and compilations. The initial unheaded discography section follows the site's default LP convention; explicit non-album sections and EP/single/live/reissue title annotations are filtered out. Catalog mistakes or unmarked shorter releases can still need a sourced correction. Sputnikmusic's genre labels describe the **artist**, so they may cover more styles than an individual album. The script collects displayed genre links and all `EXISTING TAGS` votes; it ignores the unrelated `ALL GENRES` selection options.

Native `sputnikmusic:<album-id>` identifiers merge repeat observations. Artist and listing URLs provide provenance and never merge different albums merely because they share those pages. Sputnikmusic uses the same 24-hour HTTP cache, `--offline`, `--refresh` and per-source warnings as the other HTTP sources.

### Discogs dates and access

Discogs masters supply **original-year precision** (`2026`), not the date of a selected edition or the master record's creation. The importer does not assume that `main_release` is the earliest edition. Other sources can refine the month/day when their dates are compatible. A masterless release supplies its stated `released` date or release year; upload/edit timestamps are never used. An unmarked reissue, a wrong master year or missing genre/type tags can still require a sourced correction.

The public API worked without a token during development. If Discogs requires authentication, configure a personal token through the environment:

```sh
export DISCOGS_TOKEN='your personal access token'
python3 update.py --year 2026
```

Tokens go only to `api.discogs.com` in an authorization header, never into URLs, cache files or generated data, and are not forwarded on redirects. Authenticated requests are spaced 1.1 seconds apart. The initial full import can take several minutes under API limits; repeated runs use the local cache. See the [Discogs API documentation](https://www.discogs.com/developers/).

### Reddit access

The default `"reddit_mode": "auto"` in [`sources.json`](sources.json) selects the API when `REDDIT_ACCESS_TOKEN` or `REDDIT_CLIENT_ID` is configured, and headless Chrome otherwise. Set `"reddit_mode": "browser"` or `"api"` to choose explicitly. Older source configurations without `reddit_mode` retain API behavior. `--reddit-browser` forces browser mode for that run.

Install the optional dependency into a local environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-browser.txt
```

With Google Chrome installed, run an unattended update directly; no Reddit login, app credentials or session setup is required:

```sh
.venv/bin/python update.py --year 2025 2026 --reddit-browser
```

The updater obtains the installed Chrome's native User-Agent on a blank page and uses its normal `Chrome/…` token instead of `HeadlessChrome/…`, retaining the actual version and platform. Playwright applies that User-Agent to both HTTP requests and JavaScript; Chrome supplies its usual navigation headers. The browser locale is `en-US`. No hard-coded browser version or CAPTCHA interaction is involved.

An optional visible diagnostic/session setup command remains available:

```sh
.venv/bin/python update.py --year 2026 --reddit-login
```

This opens a visible window and caches wiki observations without updating CSV/Markdown or fetching other sources. It is not a prerequisite for headless updates. The default wait is 300 seconds per year; `--reddit-timeout 600` allows more time.

Chrome uses its own persistent profile at `.cache/reddit-browser/`, separate from your normal Chrome profile. The directory is private (mode `0700`) and ignored by Git; do not share it. With a custom `--cache-dir`, the profile is its sibling `reddit-browser/` directory. Run only one browser/updater using that profile at a time. No passwords or cookies are exported by the script.

Only parsed public release observations are cached in `.cache/http/reddit-wiki/YYYY.json`; full-page HTML, account UI and browser state are not stored in that cache. Valid observations are reused for 24 hours. `--refresh` forces a new visit, and `--offline` reads the cache without importing Playwright or opening Chrome. Failed refreshes preserve the previous cache and produce a warning and exit code 1; an unattended run never waits for a person to log in or solve a CAPTCHA. Reddit can still change its access controls.

Without system Chrome, install Playwright's Chromium and select it instead:

```sh
.venv/bin/python -m playwright install chromium
REDDIT_BROWSER_CHANNEL=chromium .venv/bin/python update.py --year 2026 --reddit-browser
```

See Playwright's [browser channels](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge) and [persistent context documentation](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).

As an alternative, configure the credentials of an approved confidential Reddit app (web/script) for API updates:

```sh
export REDDIT_CLIENT_ID='your client ID'
export REDDIT_CLIENT_SECRET='your client secret'
python3 update.py --year 2025 2026
```

The script requests an application-only bearer token with `wikiread` scope on each online run. It does not require a Reddit account password, store tokens on disk, or forward authorization to redirects. `--offline` uses cached wiki responses and never requests a token. Credentials are read from the environment; `.env` files are ignored by Git but are not loaded by the script.

Alternatively, set `REDDIT_ACCESS_TOKEN` to an existing bearer token with wiki-read access. It takes precedence over app credentials; short-lived access tokens need replacement when they expire. See Reddit's [API access guidance](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) and [application-only OAuth documentation](https://github.com/reddit-archive/reddit/wiki/OAuth2#application-only-oauth). API approval is controlled by Reddit.

API mode without credentials attempts the anonymous wiki JSON endpoint; it returned **403** during development. On 2026-11-01, anonymous headless Chrome with the normal Chrome User-Agent successfully fetched the actual full yearly wiki: **441 observations for 2025 and 503 for 2026**, including **640 classified as albums** across the two years. The same browser's default HeadlessChrome User-Agent received a challenge. A real headless regression test serves a challenge to that header and verifies release-table extraction with the normal header, matching network/JavaScript User-Agent, native browser version and persistent profile reuse. Separate parser/CLI tests cover discovery, reclassification, repeated updates and blocked refreshes. Access challenges are reported and never cached as release data.

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

Albums merge by normalized artist/title, shared **nonzero** source identifiers, or explicit aliases. Direct Bandcamp album links from wiki album cells and parsed Bandcamp pages also carry a shared URL identifier, so alternate titles merge without fuzzy guessing. When the fetched Bandcamp page identifies a different artist, the updater rejects the community link's URL identity and records the mismatch in notes. An explicit canonical-artist correction can approve a real alias. Strong native-ID artist-credit changes retain their usual merge behavior. A generic bibliographic source URL is not an identity key: one announcement can describe multiple works. `S/T` maps to a self-titled album. Different works with identical names may require a correction using a `source_id` and a disambiguated canonical title.

An old record fused by a bad community Bandcamp link is separated automatically only when fresh observations cover every old source ID, date claim, tag and provenance URL and preserve the original primary work's saved ID. Otherwise its historical evidence is retained conservatively. New generated IDs cannot reuse a saved ID held by another album after such a split. Title-only corrections do not approve artist aliases, and applying a manual type correction does not turn it into a source-reported exclusion.

An album can share its name with a title-track single. When a native release catalog supplies distinct IDs for the album and non-album release, name matching and synthetic wiki/Bandcamp-URL keys respect that distinction, including through partial updates. PRN's single entry alone cannot remove a same-named album from another catalog; it can still reclassify a saved record with the same native PRN ID. Explicit EP/live/demo/remix/reissue title markers retain their exclusions. A shared strong native ID still permits reclassification of an existing record; separate release formats do not override an explicit correction.

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

The updater also retains automatic non-album classifications in `excluded.json`. Do not edit this generated file. To overturn a mistaken classification, add a `release_type: "album"` correction; corrections take precedence over saved exclusions.

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
    "styles": ["Post Rock", "Ambient"],
    "source_ids": [],
    "source_urls": ["https://example.bandcamp.com/album/title"],
    "notes": "Day not announced."
  }
]
```

The imported observations merge with saved rows and corrections. List-valued CSV fields (`styles`, `date_candidates`, `source_ids`, `source_urls`) are JSON arrays inside standard UTF-8 CSV cells. Styles are retained through partial or failed source refreshes. Additional years present in imported/corrected data receive their own files.
