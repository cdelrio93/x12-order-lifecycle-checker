"""Tests for x12_checker.report, using the files in sample_data/.

Run from the repo root:
    python -m unittest discover tests
"""

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from x12_checker.parser import parse_folder
from x12_checker.report import build_report, load_names, main

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"
AS_OF = datetime(2026, 3, 7, 18, 0)


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_folder(SAMPLE_DATA)
        cls.names = load_names(SAMPLE_DATA / "partners.json")
        cls.text = build_report(cls.result, AS_OF, cls.names)

    def test_summary_counts(self):
        self.assertIn("12 orders: 7 with problems, 4 done, 1 open", self.text)
        self.assertIn("PROBLEMS (7)", self.text)

    def test_problem_list_uses_partner_names(self):
        self.assertIn("MISSING   PO 4500012  Bluebird Home Stores   945 received, no 856 sent", self.text)

    def test_timeline_rows(self):
        self.assertIn("PO 4500012  Bluebird Home Stores  [PROBLEM]", self.text)
        self.assertIn("  2026-03-02 09:14  850 received   120 units", self.text)
        self.assertIn("  2026-03-02 09:15  997 sent      for 850", self.text)
        self.assertIn("  2026-03-02 15:45  997 received  for 855", self.text)
        self.assertIn("  !! MISSING  945 received, no 856 sent", self.text)

    def test_open_order_shows_what_is_due_next(self):
        self.assertIn("PO 4500063  Maple & Pine Market  [OPEN]", self.text)
        self.assertIn("  .. waiting  856 due by 2026-03-09 10:05", self.text)

    def test_every_order_has_a_timeline(self):
        self.assertEqual(self.text.count("\nPO 45000"), 12)

    def test_problems_only(self):
        text = build_report(self.result, AS_OF, self.names, problems_only=True)
        self.assertEqual(text.count("\nPO 45000"), 7)
        self.assertNotIn("[DONE]", text)
        self.assertNotIn("[OPEN]", text)

    def test_without_names_shows_x12_ids(self):
        text = build_report(self.result, AS_OF)
        self.assertIn("PO 4500012  BLUEBIRD  [PROBLEM]", text)

    def test_no_files_section_when_everything_was_readable(self):
        self.assertNotIn("FILES", self.text)


class CommandLineTests(unittest.TestCase):
    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(args))
        return code, buf.getvalue()

    def test_runs_on_sample_data_and_finds_partner_names(self):
        code, out = self.run_main(str(SAMPLE_DATA), "--as-of", "2026-03-07 18:00")
        self.assertEqual(code, 0)
        self.assertIn("PROBLEMS (7)", out)
        self.assertIn("Tidewater Living", out)

    def test_bad_files_are_listed_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            for f in SAMPLE_DATA.glob("*4500031*.x12"):
                shutil.copy(f, tmp)
            Path(tmp, "broken.x12").write_text("not edi", encoding="ascii")
            _, out = self.run_main(tmp, "--as-of", "2026-03-07 18:00")
        self.assertIn("1 order: 1 with problems, 0 done, 0 open", out)
        self.assertIn("FILES", out)
        self.assertIn("could not read broken.x12", out)

    def test_bad_as_of_is_a_clear_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                main([str(SAMPLE_DATA), "--as-of", "March 7"])
        self.assertIn("--as-of must look like", err.getvalue())


if __name__ == "__main__":
    unittest.main()
