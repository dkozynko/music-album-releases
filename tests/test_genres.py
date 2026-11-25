import json
import unittest
from urllib.parse import parse_qs, urlsplit

from test_discogs import page, release, summary
from test_sputnik import BASE, BAND_URL, FixtureClient, album, band, listing
from test_update import bandcamp_document, updater
import sputnik


GENRES = ["post-rock", "post-metal"]


class GenreTests(unittest.TestCase):
    def test_sputnik_modern_released_label_supplies_only_catalog_year(self):
        metadata = '<span class="ag-k">Released</span><div class="ag-v">November 7, 2026</div>'
        body = album().replace('Release Date: <b>2026</b>', metadata)
        row = sputnik.parse_album(body, 101)
        self.assertEqual(row["year"], 2026)
        with self.assertRaises(ValueError):
            sputnik.parse_album(body.replace(metadata, ''), 101)
        misleading = album(title='Released 2000').replace('Release Date: <b>2026</b>', metadata)
        self.assertEqual(sputnik.parse_album(misleading, 101)["year"], 2026)

    def test_sputnik_modern_listing_validates_genre_and_scopes_months(self):
        document = ('<link rel="canonical" href="https://www.sputnikmusic.com/newreleases/post-metal/">'
                    '<b class="ch-pill ch-on" aria-current="page" data-g="post-metal">Post Metal</b>'
                    '<div class="nr-month" id="m2026-11"><h2>New Post Metal Albums of November 2026</h2>'
                    '<div class="nr-grid"><a class="nr-tile" href="/album-ratings/101/Band-Record/">New Album</a></div></div>'
                    '<div class="nr-month" id="m2026-10"><a class="nr-tile" href="/review/999/Band-Other/">'
                    '<img src="/images/albums/102.jpg-thumbl">Earlier Album</a></div>'
                    '<footer><a href="/album/999/Related/">Related</a></footer>')
        self.assertEqual(sputnik.parse_listing(document, "post-metal"), {101: {"2026-11"}, 102: {"2026-10"}})
        with self.assertRaises(ValueError):
            sputnik.parse_listing(document, "post-rock")

    def test_each_discogs_style_has_independent_pagination(self):
        m = updater()
        pages_seen = []
        class Client:
            def get(self, url):
                if "/database/search?" in url:
                    query = parse_qs(urlsplit(url).query)
                    style, number = query["style"][0], int(query["page"][0])
                    pages_seen.append((style, number))
                    identifier = (0 if style == "Post Rock" else 2) + number
                    return json.dumps(page([summary(identifier, 10, style=[style])],
                                           current=number, pages=2, items=101))
                return json.dumps(dict(id=10, year=2026, title="Shared Work", artists=[{"name": "Band"}]))
        rows, errors = m.collect_discogs(Client(), {2026}, GENRES)
        self.assertEqual(errors, [])
        self.assertEqual(pages_seen, [("Post Rock", 1), ("Post Rock", 2), ("Post-Metal", 1), ("Post-Metal", 2)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["source_ids"]), {"discogs-master:10", *(f"discogs-release:{i}" for i in range(1, 5))})

    def test_discogs_searches_both_styles_and_fetches_shared_master_once(self):
        m = updater()
        calls = []
        class Client:
            def get(self, url):
                calls.append(url)
                if "/database/search?" in url:
                    style = parse_qs(urlsplit(url).query)["style"][0]
                    if style == "Post Rock":
                        return json.dumps(page([summary(1, 10)]))
                    if style == "Post-Metal":
                        return json.dumps(page([summary(1, 10, style=["Post-Metal"]),
                                                summary(2, style=["Post-Metal"])]))
                if url.endswith("/masters/10"):
                    return json.dumps(dict(id=10, year=2026, title="Shared Work", artists=[{"name": "Band"}], styles=["Post Rock"]))
                if url.endswith("/releases/2"):
                    return json.dumps(release(2, title="Metal Only", styles=["Post-Metal"]))
                raise AssertionError(url)
        rows, errors = m.collect_discogs(Client(), {2026}, GENRES)
        self.assertEqual(errors, [])
        self.assertEqual({r["album"] for r in rows}, {"Shared Work", "Metal Only"})
        self.assertEqual(sum(url.endswith("/masters/10") for url in calls), 1)
        shared = next(r for r in rows if r["album"] == "Shared Work")
        self.assertEqual(set(shared["styles"]), {"Post Rock", "Post-Metal"})

    def test_one_discogs_style_failure_does_not_erase_other_style(self):
        m = updater()
        class Client:
            def get(self, url):
                if "/database/search?" in url:
                    if parse_qs(urlsplit(url).query)["style"] == ["Post Rock"]:
                        raise ValueError("unavailable")
                    return json.dumps(page([summary(2, style=["Post-Metal"])]))
                return json.dumps(release(2, styles=["Post-Metal"]))
        rows, errors = m.collect_discogs(Client(), {2026}, GENRES)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)

    def test_musicbrainz_uses_or_and_accepts_metal_only_album(self):
        m = updater()
        class Client:
            def get(self, url):
                query = parse_qs(urlsplit(url).query)["query"][0]
                self.assert_query(query)
                return json.dumps({"count": 1, "release-groups": [{"id": "metal", "title": "Heavy Record",
                    "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-11-01",
                    "artist-credit": [{"name": "Metal Band"}], "tags": [{"name": "post-metal"}]}]})
            def assert_query(self, query):
                if '(tag:"post-rock" OR tag:"post-metal")' not in query:
                    raise AssertionError(query)
        rows, errors = m.collect_sources({"genres": GENRES}, {2026}, Client(), [])
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["album"], "Heavy Record")

    def test_bandcamp_discovers_either_tag_without_requiring_both(self):
        m = updater()
        class Client:
            def get(self, url):
                if url.endswith("/music"):
                    return '<ol id="music-grid"><a href="/album/metal">Metal</a></ol>'
                return bandcamp_document("01 Nov 2026 00:00:00 GMT").replace("post-rock", "post-metal")
        rows, errors = m.collect_sources({"genres": GENRES, "musicbrainz": False,
            "bandcamp_catalogs": ["https://example.bandcamp.com/music"]}, {2026}, Client(), [])
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["styles"], ["post-metal"])

    def test_sputnik_metal_listing_has_its_own_provenance_without_false_rock_tag(self):
        metal = BASE + "/newreleases.php?t=38"
        data = {BASE + "/newreleases.php?t=6": listing(), metal: listing().replace("t=6", "t=38").replace("Post Rock", "Post Metal"),
                BASE + "/soundoff.php?albumid=101": album(), BAND_URL: band().replace("Post Rock", "Post Metal")}
        client = FixtureClient(data)
        rows, errors = sputnik.collect_sputnik(client, {2026}, genres=["post-metal"])
        self.assertEqual(errors, [])
        self.assertIn(metal, rows[0]["source_urls"])
        self.assertNotIn(BASE + "/newreleases.php?t=6", rows[0]["source_urls"])
        self.assertIn("Post Metal", rows[0]["styles"])
        self.assertNotIn("Post Rock", rows[0]["styles"])

    def test_sputnik_shared_album_combines_listing_months_and_genres(self):
        metal = BASE + "/newreleases.php?t=38"
        data = {BASE + "/newreleases.php?t=6": listing(), metal: listing().replace("t=6", "t=38").replace("Post Rock", "Post Metal"),
                BASE + "/soundoff.php?albumid=101": album(), BAND_URL: band()}
        rows, errors = sputnik.collect_sputnik(FixtureClient(data), {2026}, genres=GENRES)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["release_date"], "2026-10")
        self.assertTrue({"Post Rock", "Post Metal"}.issubset(rows[0]["styles"]))


if __name__ == "__main__":
    unittest.main()
