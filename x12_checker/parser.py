"""Read X12 files and pull out the fields the checker needs.

For every transaction it keeps:
- who sent it, who received it, and when (from the GS envelope)
- the GS and ST control numbers
- the PO number
- the item lines: SKU and quantity

What "quantity" means depends on the document:
    850 ordered, 855 acknowledged, 940 to ship, 945 shipped,
    856 shipped, 810 invoiced

For a 997 it keeps which document it acknowledges, and link_acks() connects
each 997 to that document.

The parser does not judge anything. Finding problems is the checker's job.

Try it on the sample data:
    python -m x12_checker.parser sample_data
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

X12_SUFFIXES = {".x12", ".edi"}


class ParseError(Exception):
    """The file is not X12 we can read."""


@dataclass
class Line:
    sku: str
    qty: int
    ordered: int | None = None  # 855 and 945 also carry the ordered quantity


@dataclass
class AckRef:
    """One AK2 loop inside a 997: which document it acknowledges."""

    func_id: str  # AK101, e.g. PO, SH, IN
    group_ctrl: str  # AK102, the GS control number being acknowledged
    txset: str  # AK201, e.g. 850
    set_ctrl: str  # AK202, the ST control number being acknowledged
    status: str  # AK501: A accepted, E accepted with errors, R rejected


@dataclass
class Document:
    txset: str
    sender: str
    receiver: str
    sent_at: datetime
    group_ctrl: str
    set_ctrl: str
    po: str | None = None
    lines: list[Line] = field(default_factory=list)
    ack_refs: list[AckRef] = field(default_factory=list)  # only on a 997
    source: str = ""  # file name

    # Filled in by link_acks(). Left out of repr and == to avoid loops.
    acknowledges: list[Document] = field(default_factory=list, repr=False, compare=False)
    acked_by: list[Document] = field(default_factory=list, repr=False, compare=False)

    @property
    def total_qty(self) -> int:
        return sum(line.qty for line in self.lines)


@dataclass
class ParseResult:
    documents: list[Document]
    errors: list[tuple[str, str]]  # (file name, what went wrong)
    orphan_acks: list[Document]  # 997s that match no document we have


# --- Low level helpers ------------------------------------------------------


def split_segments(text: str) -> list[list[str]]:
    """Split X12 text into segments, and each segment into elements.

    The separators are read from the ISA segment itself, which is always
    106 characters long: position 3 is the element separator, 104 the
    sub-element separator, and 105 the segment terminator.
    """
    text = text.lstrip("﻿ \r\n\t")
    if not text.startswith("ISA") or len(text) < 106:
        raise ParseError("file does not start with a valid ISA segment")
    element_sep = text[3]
    terminator = text[105]

    segments = []
    for raw in text.split(terminator):
        raw = raw.strip()
        if raw:
            segments.append(raw.split(element_sep))
    return segments


def _el(seg: list[str], i: int) -> str:
    """Element i of a segment, or "" if the segment is shorter."""
    return seg[i].strip() if i < len(seg) else ""


def _qty(value: str) -> int:
    try:
        return int(round(float(value)))
    except ValueError:
        raise ParseError(f"quantity {value!r} is not a number") from None


def _product_id(seg: list[str], start: int, qualifier: str = "VN") -> str:
    """Find the product ID in qualifier/ID pairs, starting at element `start`.

    Prefers the vendor item number (VN). Falls back to the first ID found.
    """
    pairs = [(_el(seg, i), _el(seg, i + 1)) for i in range(start, len(seg) - 1, 2)]
    for qual, value in pairs:
        if qual == qualifier and value:
            return value
    for _, value in pairs:
        if value:
            return value
    return ""


def _gs_datetime(date: str, time: str) -> datetime:
    try:
        return datetime.strptime(date + time[:4], "%Y%m%d%H%M")
    except ValueError:
        raise ParseError(f"GS date/time {date} {time} is not valid") from None


def _norm_ctrl(ctrl: str) -> str:
    """Control numbers are compared without leading zeros."""
    return ctrl.lstrip("0") or "0"


# --- One reader per transaction set ----------------------------------------
# Each reader looks at the segments between ST and SE and fills in the doc.


def _read_850(segs: list[list[str]], doc: Document) -> None:
    for s in segs:
        if s[0] == "BEG":
            doc.po = _el(s, 3)
        elif s[0] == "PO1":
            doc.lines.append(Line(_product_id(s, 6), _qty(_el(s, 2))))


def _read_855(segs: list[list[str]], doc: Document) -> None:
    for s in segs:
        if s[0] == "BAK":
            doc.po = _el(s, 3)
        elif s[0] == "PO1":
            qty = _qty(_el(s, 2))
            doc.lines.append(Line(_product_id(s, 6), qty, ordered=qty))
        elif s[0] == "ACK" and doc.lines and _el(s, 2):
            # The ACK after a PO1 says how many units Acme accepted.
            doc.lines[-1].qty = _qty(_el(s, 2))


def _read_940(segs: list[list[str]], doc: Document) -> None:
    for s in segs:
        if s[0] == "W05":
            doc.po = _el(s, 3)
        elif s[0] == "W01":
            doc.lines.append(Line(_product_id(s, 4), _qty(_el(s, 1))))


def _read_945(segs: list[list[str]], doc: Document) -> None:
    for s in segs:
        if s[0] == "W06":
            doc.po = _el(s, 6)
        elif s[0] == "W12":
            ordered = _el(s, 2)
            doc.lines.append(Line(
                _product_id(s, 7),
                _qty(_el(s, 3)),
                ordered=_qty(ordered) if ordered else None,
            ))


def _read_856(segs: list[list[str]], doc: Document) -> None:
    # Reads the first PRF as the PO. One PO per ASN is all this project needs.
    sku = ""
    for s in segs:
        if s[0] == "PRF" and doc.po is None:
            doc.po = _el(s, 1)
        elif s[0] == "LIN":
            sku = _product_id(s, 2)
        elif s[0] == "SN1":
            doc.lines.append(Line(sku, _qty(_el(s, 2))))


def _read_810(segs: list[list[str]], doc: Document) -> None:
    for s in segs:
        if s[0] == "BIG":
            doc.po = _el(s, 4)
        elif s[0] == "IT1":
            doc.lines.append(Line(_product_id(s, 6), _qty(_el(s, 2))))


def _read_997(segs: list[list[str]], doc: Document) -> None:
    func_id = group_ctrl = ""
    for s in segs:
        if s[0] == "AK1":
            func_id, group_ctrl = _el(s, 1), _el(s, 2)
        elif s[0] == "AK2":
            doc.ack_refs.append(AckRef(func_id, group_ctrl, _el(s, 1), _el(s, 2), status=""))
        elif s[0] == "AK5" and doc.ack_refs:
            doc.ack_refs[-1].status = _el(s, 1)


READERS = {
    "850": _read_850,
    "855": _read_855,
    "940": _read_940,
    "945": _read_945,
    "856": _read_856,
    "810": _read_810,
    "997": _read_997,
}


# --- Putting it together ----------------------------------------------------


def parse_text(text: str, source: str = "") -> list[Document]:
    """Parse X12 text into one Document per ST/SE transaction."""
    docs: list[Document] = []
    gs: list[str] | None = None
    current: Document | None = None
    body: list[list[str]] = []

    for seg in split_segments(text):
        tag = seg[0]
        if tag == "GS":
            gs = seg
        elif tag == "ST":
            if gs is None:
                raise ParseError("ST segment found before any GS segment")
            current = Document(
                txset=_el(seg, 1),
                sender=_el(gs, 2),
                receiver=_el(gs, 3),
                sent_at=_gs_datetime(_el(gs, 4), _el(gs, 5)),
                group_ctrl=_el(gs, 6),
                set_ctrl=_el(seg, 2),
                source=source,
            )
            body = []
        elif tag == "SE":
            if current is None:
                raise ParseError("SE segment found without a matching ST")
            reader = READERS.get(current.txset)
            if reader:
                reader(body, current)
            docs.append(current)
            current = None
        elif current is not None:
            body.append(seg)

    if current is not None:
        raise ParseError(f"transaction {current.txset} has no SE segment")
    if not docs:
        raise ParseError("no transactions found")
    return docs


def parse_file(path: Path) -> list[Document]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_text(text, source=path.name)


def link_acks(docs: list[Document]) -> list[Document]:
    """Connect each 997 to the document it acknowledges.

    A 997 travels the opposite way from the document it answers, so the
    document's sender is the 997's receiver. Matching uses that sender plus
    the GS and ST control numbers from AK1 and AK2.

    The 997 also takes the PO number of the document it acknowledges.
    Returns the 997s that matched nothing.
    """
    index = {
        (d.sender, _norm_ctrl(d.group_ctrl), _norm_ctrl(d.set_ctrl)): d
        for d in docs
        if d.txset != "997"
    }
    orphans = []
    for ack in docs:
        if ack.txset != "997":
            continue
        for ref in ack.ack_refs:
            target = index.get((ack.receiver, _norm_ctrl(ref.group_ctrl), _norm_ctrl(ref.set_ctrl)))
            if target is not None and target.txset == ref.txset:
                target.acked_by.append(ack)
                ack.acknowledges.append(target)
        if ack.acknowledges:
            ack.po = ack.acknowledges[0].po
        else:
            orphans.append(ack)
    return orphans


def parse_folder(folder: Path | str) -> ParseResult:
    """Parse every X12 file in a folder. Bad files are reported, not fatal."""
    folder = Path(folder)
    docs: list[Document] = []
    errors: list[tuple[str, str]] = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in X12_SUFFIXES:
            continue
        try:
            docs.extend(parse_file(path))
        except ParseError as exc:
            errors.append((path.name, str(exc)))
    docs.sort(key=lambda d: (d.sent_at, d.source))
    orphans = link_acks(docs)
    return ParseResult(docs, errors, orphans)


# --- Quick look from the command line ---------------------------------------


def _describe(doc: Document) -> str:
    if doc.txset == "997":
        what = ", ".join(f"acks {t.txset}" for t in doc.acknowledges) or "acks ??? (no match)"
    else:
        acked = "997 back" if doc.acked_by else "no 997"
        what = f"{doc.total_qty:>4} units  {acked}"
    route = f"{doc.sender} -> {doc.receiver}"
    return f"{doc.sent_at:%Y-%m-%d %H:%M}  {doc.txset}  {route:<22}  PO {doc.po or '?':<8}  {what}"


def main(argv: list[str]) -> int:
    folder = Path(argv[1]) if len(argv) > 1 else Path("sample_data")
    result = parse_folder(folder)
    for doc in result.documents:
        print(_describe(doc))
    print()
    print(f"{len(result.documents)} documents, {len(result.errors)} unreadable files, "
          f"{len(result.orphan_acks)} 997s with no match")
    for name, error in result.errors:
        print(f"  could not read {name}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
