"""Tests for x12_checker.checks.

The first group runs on sample_data/ and must match the answer key in
sample_data/README.md exactly. The second group builds tiny orders by hand
to test one rule at a time.

Run from the repo root:
    python -m unittest discover tests
"""

import itertools
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from x12_checker.checks import check_documents, fmt_duration
from x12_checker.parser import AckRef, Document, Line, link_acks, parse_folder

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"
AS_OF = datetime(2026, 3, 7, 18, 0)

ANSWER_KEY = {
    ("MISSING", "4500012"),
    ("MISSING", "4500082"),
    ("QTY", "4500031"),
    ("QTY", "4500070"),
    ("LATE", "4500044"),
    ("NO 997", "4500050"),
    ("QUIET", "4500057"),
}


class AnswerKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = parse_folder(SAMPLE_DATA).documents

    def test_finds_exactly_the_answer_key(self):
        problems = check_documents(self.docs, AS_OF)
        self.assertEqual({(p.kind, p.po) for p in problems}, ANSWER_KEY)
        self.assertEqual(len(problems), len(ANSWER_KEY))

    def test_messages(self):
        by_po = {p.po: p for p in check_documents(self.docs, AS_OF)}
        self.assertEqual(by_po["4500012"].message, "945 received, no 856 sent")
        self.assertEqual(by_po["4500082"].message, "856 sent, no 810 sent")
        self.assertEqual(by_po["4500031"].message, "invoiced 50 > shipped 48 for AHG-1003")
        self.assertEqual(by_po["4500070"].message, "shipped 26 > ordered 24 for AHG-1001")
        self.assertEqual(by_po["4500044"].message, "855 sent 30h after 850 (limit 24h)")
        self.assertEqual(by_po["4500050"].message, "850 received, no 997 sent")
        self.assertEqual(by_po["4500057"].message, "no activity for 8 days, waiting on 855, 940")

    def test_partner_comes_from_the_order(self):
        by_po = {p.po: p for p in check_documents(self.docs, AS_OF)}
        self.assertEqual(by_po["4500012"].partner, "BLUEBIRD")
        self.assertEqual(by_po["4500031"].partner, "MAPLEPINE")

    def test_problems_are_sorted_by_kind(self):
        kinds = [p.kind for p in check_documents(self.docs, AS_OF)]
        self.assertEqual(kinds, ["MISSING", "MISSING", "QTY", "QTY", "LATE", "NO 997", "QUIET"])

    def test_one_hour_earlier_the_856_is_not_overdue_yet(self):
        # 945 for 4500012 arrived 2026-03-05 17:22, so the 856 is due 2026-03-07 17:22.
        problems = check_documents(self.docs, datetime(2026, 3, 7, 17, 0))
        self.assertNotIn(("MISSING", "4500012"), {(p.kind, p.po) for p in problems})

    def test_documents_after_as_of_are_ignored(self):
        # On 2026-03-03 at noon, 4500082 had no problems yet. Its 810 was
        # still days away from being due.
        problems = check_documents(self.docs, datetime(2026, 3, 3, 12, 0))
        self.assertNotIn("4500082", {p.po for p in problems})


# --- Hand-built orders ------------------------------------------------------

T0 = datetime(2026, 1, 5, 9, 0)
_ctrl = itertools.count(1)


def doc(txset, hours, sender, receiver, lines=(), po="9000001", ctrl=None):
    """A Document sent `hours` after T0."""
    n = ctrl or str(next(_ctrl))
    return Document(
        txset=txset, sender=sender, receiver=receiver,
        sent_at=T0 + timedelta(hours=hours),
        group_ctrl=n, set_ctrl=n, po=po,
        lines=[Line(sku, qty) for sku, qty in lines],
    )


def ack(target, hours):
    """A 997 for `target`, linked the same way the parser links them."""
    a = doc("997", hours, target.receiver, target.sender, po=None)
    a.acknowledges.append(target)
    target.acked_by.append(a)
    a.po = target.po
    return a


class SingleRuleTests(unittest.TestCase):
    def kinds(self, docs, as_of_hours):
        problems = check_documents(docs, T0 + timedelta(hours=as_of_hours))
        return sorted(p.kind for p in problems), problems

    def test_late_997(self):
        po = doc("850", 0, "SHOP", "ACME", [("SKU1", 5)])
        docs = [po, ack(po, 30)]
        kinds, problems = self.kinds(docs, 31)
        self.assertEqual(kinds, ["LATE", "MISSING", "MISSING"])  # 997 late; 855 and 940 overdue
        late = next(p for p in problems if p.kind == "LATE")
        self.assertEqual(late.message, "997 for 850 sent 30h after it (limit 24h)")

    def test_no_997_from_partner_for_outbound_document(self):
        po = doc("850", 0, "SHOP", "ACME", [("SKU1", 5)])
        poa = doc("855", 2, "ACME", "SHOP", [("SKU1", 5)])
        wh = doc("940", 3, "ACME", "WH", [("SKU1", 5)])
        docs = [po, ack(po, 0.1), poa, wh, ack(wh, 3.1)]
        _, problems = self.kinds(docs, 30)
        self.assertEqual([p.message for p in problems], ["855 sent, no 997 received"])

    def test_shipped_comes_from_856_when_there_is_no_945(self):
        docs = [
            doc("850", 0, "SHOP", "ACME", [("SKU1", 10)]),
            doc("856", 1, "ACME", "SHOP", [("SKU1", 12)]),
        ]
        problems = check_documents(docs, T0 + timedelta(hours=2))
        qty = [p.message for p in problems if p.kind == "QTY"]
        self.assertEqual(qty, ["shipped 12 > ordered 10 for SKU1"])

    def test_no_850_on_file(self):
        docs = [doc("810", 0, "ACME", "SHOP", [("SKU1", 1)])]
        problems = check_documents(docs, T0 + timedelta(hours=1))
        self.assertEqual([(p.kind, p.message) for p in problems],
                         [("MISSING", "documents on file for this PO, but no 850")])

    def test_finished_order_is_never_quiet(self):
        po = doc("850", 0, "SHOP", "ACME", [("SKU1", 1)])
        flow = [po, ack(po, 0.1)]
        for i, (tx, s, r) in enumerate([("855", "ACME", "SHOP"), ("940", "ACME", "WH"),
                                        ("945", "WH", "ACME"), ("856", "ACME", "SHOP"),
                                        ("810", "ACME", "SHOP")], start=1):
            d = doc(tx, i, s, r, [("SKU1", 1)])
            flow += [d, ack(d, i + 0.1)]
        self.assertEqual(check_documents(flow, T0 + timedelta(days=60)), [])

    def test_link_acks_is_what_the_helpers_imitate(self):
        # Keeps the hand-built 997s honest: the real linker agrees with them.
        po = doc("850", 0, "SHOP", "ACME", ctrl="77")
        real = doc("997", 1, "ACME", "SHOP", po=None, ctrl="78")
        real.ack_refs.append(AckRef("PO", "77", "850", "77", "A"))
        self.assertEqual(link_acks([po, real]), [])
        self.assertEqual(real.po, po.po)


class FormatTests(unittest.TestCase):
    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(timedelta(hours=30)), "30h")
        self.assertEqual(fmt_duration(timedelta(hours=48)), "48h")
        self.assertEqual(fmt_duration(timedelta(days=3)), "3d")
        self.assertEqual(fmt_duration(timedelta(days=4, hours=6, minutes=40)), "4d 6h")


if __name__ == "__main__":
    unittest.main()
