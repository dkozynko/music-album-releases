import csv
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_update import observation, updater


class CatalogDateTests(unittest.TestCase):
    def test_new_album_date_survives_refresh_and_year_correction(self):
        m = updater()
        first = m.merge_catalog([observation()], added_on="2026-11-24")
        self.assertEqual(first[0]["added_date"], "2026-11-24")
        correction = dict(artist="Example Act", album="New Album", release_date="2025-10-16",
                          source_url="https://example.bandcamp.com/album/new-album")
        refreshed = m.merge_catalog([observation()], first, [correction], added_on="2026-12-01")
        self.assertEqual(refreshed[0]["added_date"], "2026-11-24")
        self.assertEqual(refreshed[0]["year"], 2025)

    def test_deduplicated_albums_keep_the_earliest_known_added_date(self):
        m = updater()
        rows = m.merge_catalog([observation(added_date="2026-11-20"), observation(added_date="2026-11-01")],
                               added_on="2026-11-24")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["added_date"], "2026-11-01")

    def test_legacy_album_is_not_misrepresented_as_new(self):
        m = updater()
        rows = m.merge_catalog([observation()], [observation(id="saved")], added_on="2026-11-24")
        self.assertEqual(rows[0]["added_date"], "")

    def test_added_date_requires_a_real_full_date(self):
        m = updater()
        for invalid in ("2026", "2026-11", "2026-02-30", "2026-11-24T12:00:00Z"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                m.prepare(observation(added_date=invalid))

    def test_csv_round_trip_and_markdown_include_added_date(self):
        m = updater()
        rows = m.merge_catalog([observation()], added_on="2026-11-24")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            m.write_catalog(root, rows, {2026})
            self.assertEqual(m.read_catalog(root)[0]["added_date"], "2026-11-24")
            document = (root / "2026.md").read_text()
            self.assertIn('<th nowrap align="left"><sub>Added</sub></th>', document)
            self.assertIn("<td nowrap><sub>2026-11-24</sub></td>", document)

    def test_render_only_reads_legacy_csv_without_backfilling_or_rewriting_it(self):
        m = updater()
        row = m.merge_catalog([observation()], added_on="2026-11-24")[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "2026.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=m.PREVIOUS_FIELDS)
                writer.writeheader()
                writer.writerow({key: json.dumps(row[key]) if isinstance(row[key], list) else row[key]
                                 for key in m.PREVIOUS_FIELDS})
            original = path.read_bytes()
            with patch.object(m, "backfill_added_dates", side_effect=AssertionError("render-only must not migrate")):
                self.assertEqual(m.main(["--year", "2026", "--output-dir", str(root), "--render-only"]), 0)
            self.assertEqual(path.read_bytes(), original)
            self.assertIn('<th nowrap align="left"><sub>Added</sub></th>', (root / "2026.md").read_text())

    def test_git_backfill_uses_first_appearance_by_id_across_year_files(self):
        m = updater()
        def snapshot(added_date=""):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=["id", "added_date"])
            writer.writeheader()
            writer.writerow(dict(id="saved", added_date=added_date))
            return stream.getvalue()
        def git(command, **kwargs):
            tail = command[3:]
            if tail == ["rev-parse", "--show-toplevel"]:
                output = "/catalog\n"
            elif tail == ["rev-parse", "--is-shallow-repository"]:
                output = "false\n"
            elif tail[0] == "log":
                self.assertEqual(tail[-1], ":(top)releases/*.csv")
                # A removed year CSV must not produce an unreadable snapshot.
                self.assertIn("--diff-filter=AM", tail)
                self.assertIn("--no-renames", tail)
                output = "a\t2026-11-01T12:00:00+00:00\nreleases/2026.csv\nb\t2026-11-24T12:00:00+00:00\nreleases/2025.csv\n"
            elif tail == ["show", "a:releases/2026.csv"]:
                output = snapshot()
            elif tail == ["show", "b:releases/2025.csv"]:
                output = snapshot()
            else:
                raise AssertionError(command)
            return subprocess.CompletedProcess(command, 0, output, "")
        rows = [m.prepare(observation(id="saved")), m.prepare(observation(id="already", added_date="2026-10-31"))]
        with patch.object(m.subprocess, "run", side_effect=git):
            m.backfill_added_dates(rows, Path("/catalog/releases"))
        self.assertEqual([r["added_date"] for r in rows], ["2026-11-01", "2026-10-31"])

    def test_no_git_history_keeps_unknown_date(self):
        m = updater()
        rows = [m.prepare(observation(id="saved"))]
        with patch.object(m.subprocess, "run", side_effect=FileNotFoundError):
            m.backfill_added_dates(rows, Path("/catalog/releases"))
        self.assertEqual(rows[0]["added_date"], "")

    def test_shallow_history_cannot_supply_a_false_first_appearance_date(self):
        m = updater()
        rows = [m.prepare(observation(id="saved"))]
        def git(command, **kwargs):
            output = "/catalog\n" if command[-1] == "--show-toplevel" else "true\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch.object(m.subprocess, "run", side_effect=git):
            m.backfill_added_dates(rows, Path("/catalog/releases"))
        self.assertEqual(rows[0]["added_date"], "")


if __name__ == "__main__":
    unittest.main()
