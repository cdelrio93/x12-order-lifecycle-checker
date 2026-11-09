"""Tests for x12_checker.parser, using the files in sample_data/.

Run from the repo root:
    python -m unittest discover tests
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from x12_checker.parser import ParseError, link_acks, parse_folder, parse_text

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"


class SampleFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_folder(SAMPLE_DATA)
        cls.docs = cls.result.documents

    def find(self, po, txset):
        matches = [d for d in self.docs if d.po == po and d.txset == txset]
        self.assertEqual(len(matches), 1, f"expected one {txset} for PO {po}")
        return matches[0]

    def test_reads_every_file_without_errors(self):
        self.assertEqual(len(self.docs), 123)
        self.assertEqual(self.result.errors, [])
        self.assertEqual(self.result.orphan_acks, [])

    def test_documents_are_in_time_order(self):
        times = [d.sent_at for d in self.docs]
        self.assertEqual(times, sorted(times))

    def test_850_lines_and_envelope(self):
        po = self.find("4500012", "850")
        self.assertEqual(po.sender, "BLUEBIRD")
        self.assertEqual(po.receiver, "ACMEHOME")
        self.assertEqual(po.sent_at.strftime("%Y-%m-%d %H:%M"), "2026-03-02 09:14")
        self.assertEqual([line.sku for line in po.lines], ["AHG-1001", "AHG-1004", "AHG-1005"])
        self.assertEqual(po.total_qty, 120)

    def test_945_reads_shipped_and_ordered(self):
        ship = self.find("4500012", "945")
        self.assertEqual(ship.total_qty, 118)
        short = ship.lines[2]
        self.assertEqual((short.sku, short.qty, short.ordered), ("AHG-1005", 34, 36))

    def test_856_and_810_quantities(self):
        self.assertEqual(self.find("4500031", "856").total_qty, 48)
        self.assertEqual(self.find("4500031", "810").total_qty, 50)

    def test_855_and_940(self):
        self.assertEqual(self.find("4500044", "855").total_qty, 20)
        self.assertEqual(self.find("4500044", "940").total_qty, 20)

    def test_every_997_takes_the_po_of_what_it_acknowledges(self):
        acks = [d for d in self.docs if d.txset == "997"]
        self.assertTrue(acks)
        for ack in acks:
            self.assertEqual(len(ack.acknowledges), 1, ack.source)
            self.assertEqual(ack.po, ack.acknowledges[0].po, ack.source)

    def test_only_the_planted_gap_is_missing_a_997(self):
        unacked = [(d.po, d.txset) for d in self.docs if d.txset != "997" and not d.acked_by]
        self.assertEqual(unacked, [("4500050", "850")])


class ParserEdgeCaseTests(unittest.TestCase):
    def sample_text(self, pattern):
        path = next(SAMPLE_DATA.glob(pattern))
        return path.read_text(encoding="ascii")

    def test_separators_are_read_from_the_isa(self):
        normal = self.sample_text("*_810_*4500031.x12")
        # Same file with different separators and everything on one line.
        odd = normal.replace("~\n", "|").replace("*", "^")
        a, b = parse_text(normal)[0], parse_text(odd)[0]
        self.assertEqual((a.po, a.total_qty, a.sender), (b.po, b.total_qty, b.sender))
        self.assertEqual(b.total_qty, 50)

    def test_control_numbers_match_with_or_without_leading_zeros(self):
        doc = parse_text(self.sample_text("*_850_*4500031.x12"))[0]
        ack_text = self.sample_text("*_997_*4500031-ack850.x12")
        padded = ack_text.replace(f"AK1*PO*{doc.group_ctrl}", f"AK1*PO*{int(doc.group_ctrl):09d}")
        self.assertNotEqual(ack_text, padded)
        ack = parse_text(padded)[0]
        orphans = link_acks([doc, ack])
        self.assertEqual(orphans, [])
        self.assertEqual(ack.po, "4500031")

    def test_missing_se_is_an_error(self):
        text = self.sample_text("*_850_*4500031.x12")
        cut = text[: text.index("SE*")]
        with self.assertRaises(ParseError):
            parse_text(cut)

    def test_not_x12_is_an_error(self):
        with self.assertRaises(ParseError):
            parse_text("hello, this is not EDI")

    def test_bad_file_in_folder_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = next(SAMPLE_DATA.glob("*_850_*4500031.x12"))
            shutil.copy(good, tmp)
            Path(tmp, "broken.x12").write_text("not edi at all", encoding="ascii")
            Path(tmp, "notes.txt").write_text("ignored", encoding="ascii")
            result = parse_folder(tmp)
        self.assertEqual(len(result.documents), 1)
        self.assertEqual([name for name, _ in result.errors], ["broken.x12"])


if __name__ == "__main__":
    unittest.main()
