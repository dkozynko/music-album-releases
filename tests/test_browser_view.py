import csv
import json
from pathlib import Path
import tempfile
import unittest

from test_update import observation, updater


class BrowserViewTests(unittest.TestCase):
    def test_previous_style_first_csv_migrates_without_losing_tags_or_dates(self):
        m = updater()
        row = m.merge_catalog([observation(styles=['Post Rock'], lastfm_artist_tags=['ambient', 'post-rock'])],
                              added_on='2026-11-24')[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '2026.csv'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=m.STYLE_FIRST_FIELDS)
                writer.writeheader()
                writer.writerow({key: json.dumps(value) if isinstance(value, list) else value for key, value in row.items()})
            saved = m.read_catalog(root)
            m.write_catalog(root, saved, {2026})
            self.assertEqual(m.read_catalog(root), saved)
            with path.open(newline='') as stream:
                header = next(csv.reader(stream))
            self.assertLess(header.index('lastfm_artist_tags'), header.index('styles'))
            for extension in ('md', 'html'):
                document = path.with_suffix('.' + extension).read_text()
                self.assertLess(document.index('Last.fm artist tags'), document.index('Styles'))
                values = ('<td nowrap><sub>ambient, post-rock</sub></td><td nowrap><sub>Post Rock</sub></td>' if extension == 'md'
                          else '<td nowrap>ambient, post-rock</td><td nowrap>Post Rock</td>')
                self.assertIn(values, document)

    def test_standalone_view_preserves_escaped_values_and_year_navigation(self):
        m = updater()
        row = m.merge_catalog([observation(artist='A < B', album='<script>not code</script>',
                                          lastfm_artist_tags=['ambient & drone'])])[0]
        document = m.render_html(2026, [row], {2025, 2026})
        self.assertNotIn('<script>', document)
        self.assertIn('A &lt; B', document)
        self.assertIn('&lt;script&gt;not code&lt;/script&gt;', document)
        self.assertIn('ambient &amp; drone', document)
        self.assertIn('href="2025.html"', document)
        self.assertIn('href="2026.csv" download', document)
        self.assertIn('aria-current="page">2026', document)


if __name__ == '__main__':
    unittest.main()
