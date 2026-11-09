"""Generate fake X12 sample files for Acme Home Goods.

Every company, partner, SKU, PO number and address in this file is made up.

Each order below follows the normal flow
    850 -> 855 -> 940 -> 945 -> 856 -> 810
with a 997 sent back for every document. Some orders are clean. Others are
broken on purpose, so the checker has something to find. The answer key is in
sample_data/README.md.

Run from the repo root:
    python tools/make_sample_data.py

It deletes the old .x12 files in sample_data/ and writes a fresh set,
one transaction per file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "sample_data"

# The checker should be run "as of" this moment. No sample document is later.
AS_OF = datetime(2026, 3, 7, 18, 0)

# --- Trading partners (all fictional) ---------------------------------------

ACME = "ACMEHOME"
WAREHOUSE = "RIVERBEND"

PARTNERS = {
    "BLUEBIRD": {
        "name": "BLUEBIRD HOME STORES",
        "dc_name": "BLUEBIRD DC 12",
        "dc_code": "0012",
        "address": ("100 EXAMPLE PKWY", "ANYTOWN", "OH", "45000"),
        "dept": "045",
        "item_prefix": "BB-",
    },
    "MAPLEPINE": {
        "name": "MAPLE AND PINE MARKET",
        "dc_name": "MAPLE AND PINE DC 3",
        "dc_code": "0003",
        "address": ("200 SAMPLE RD", "SAMPLEVILLE", "TX", "75000"),
        "dept": "112",
        "item_prefix": "MP-",
    },
    "TIDEWATER": {
        "name": "TIDEWATER LIVING",
        "dc_name": "TIDEWATER DC 7",
        "dc_code": "0007",
        "address": ("300 DEMO DR", "EXAMPLE CITY", "GA", "30000"),
        "dept": "027",
        "item_prefix": "TW-",
    },
}

# --- Acme catalog (all fictional): SKU -> (description, unit price) ---------

ITEMS = {
    "AHG-1001": ("LINEN THROW PILLOW 18IN", 12.50),
    "AHG-1002": ("CERAMIC TABLE LAMP", 34.00),
    "AHG-1003": ("WOVEN STORAGE BASKET", 18.75),
    "AHG-1004": ("BAMBOO BATH MAT", 15.25),
    "AHG-1005": ("GLASS VASE SET OF 3", 22.00),
}

# X12 functional group ID for each transaction set (GS01).
FUNC_ID = {
    "850": "PO",
    "855": "PR",
    "940": "OW",
    "945": "SW",
    "856": "SH",
    "810": "IN",
    "997": "FA",
}

# --- The normal flow and its default timing ---------------------------------

FLOW = ["850", "855", "940", "945", "856", "810"]

# Each step happens this long after an earlier step, unless an order overrides
# it. All defaults are inside the README time limits.
GAPS = {
    "855": ("850", timedelta(hours=6)),
    "940": ("850", timedelta(hours=7)),
    "945": ("940", timedelta(days=2)),
    "856": ("945", timedelta(hours=1)),
    "810": ("856", timedelta(hours=20)),
}


def parties(step: str, partner: str) -> tuple[str, str]:
    """Who sends and who receives each document."""
    return {
        "850": (partner, ACME),
        "855": (ACME, partner),
        "940": (ACME, WAREHOUSE),
        "945": (WAREHOUSE, ACME),
        "856": (ACME, partner),
        "810": (ACME, partner),
    }[step]


# --- Data model -------------------------------------------------------------


@dataclass
class Order:
    po: str
    partner: str
    placed: datetime
    lines: list[tuple[str, int]]  # (SKU, quantity ordered)
    times: dict[str, datetime] = field(default_factory=dict)  # override a step's time
    stop_after: str | None = None  # last document that exists for this order
    no_997_for: set[str] = field(default_factory=set)  # documents nobody acknowledged
    shipped: dict[str, int] = field(default_factory=dict)  # SKU -> units shipped, if not equal to ordered
    invoiced: dict[str, int] = field(default_factory=dict)  # SKU -> units invoiced, if not equal to shipped
    docs: dict[str, "Doc"] = field(default_factory=dict)

    @property
    def sales_order(self) -> str:
        return f"SO-{self.po}"

    def ordered_qty(self, sku: str) -> int:
        return dict(self.lines)[sku]

    def shipped_qty(self, sku: str) -> int:
        return self.shipped.get(sku, self.ordered_qty(sku))

    def invoiced_qty(self, sku: str) -> int:
        return self.invoiced.get(sku, self.shipped_qty(sku))


@dataclass
class Doc:
    when: datetime
    txset: str
    sender: str
    receiver: str
    order: Order
    acks: Doc | None = None  # for a 997: the document it acknowledges
    ctrl: int = 0  # control number, assigned after sorting by time


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# --- The sample orders ------------------------------------------------------

ORDERS = [
    # Clean, two lines.
    Order("4500018", "MAPLEPINE", at("2026-02-25 10:00"),
          [("AHG-1002", 12), ("AHG-1003", 24)]),

    # Clean, two lines.
    Order("4500025", "TIDEWATER", at("2026-02-26 14:30"),
          [("AHG-1001", 36), ("AHG-1004", 24)]),

    # BROKEN: invoiced 50 but only 48 shipped.
    Order("4500031", "MAPLEPINE", at("2026-02-26 09:00"),
          [("AHG-1003", 50)],
          shipped={"AHG-1003": 48}, invoiced={"AHG-1003": 50}),

    # BROKEN: PO received and acknowledged, then nothing for 8 days.
    Order("4500057", "TIDEWATER", at("2026-02-27 11:00"),
          [("AHG-1005", 18), ("AHG-1002", 6)],
          stop_after="850"),

    # BROKEN: 856 sent, but the 810 never went out.
    Order("4500082", "TIDEWATER", at("2026-02-27 10:30"),
          [("AHG-1004", 30)],
          stop_after="856"),

    # Clean, three lines.
    Order("4500038", "BLUEBIRD", at("2026-03-02 08:45"),
          [("AHG-1001", 24), ("AHG-1002", 12), ("AHG-1005", 12)]),

    # BROKEN: 945 received, but the 856 never went out.
    # Also a short ship (118 of 120), which is NOT a problem on its own.
    Order("4500012", "BLUEBIRD", at("2026-03-02 09:14"),
          [("AHG-1001", 48), ("AHG-1004", 36), ("AHG-1005", 36)],
          times={"855": at("2026-03-02 15:40"), "940": at("2026-03-03 08:00"),
                 "945": at("2026-03-05 17:22")},
          shipped={"AHG-1005": 34},
          stop_after="945"),

    # BROKEN: Acme never sent a 997 back for the 850.
    Order("4500050", "BLUEBIRD", at("2026-03-02 13:00"),
          [("AHG-1003", 20)],
          no_997_for={"850"}),

    # BROKEN: 855 sent 30 hours after the 850 (limit is 24).
    Order("4500044", "TIDEWATER", at("2026-03-03 08:10"),
          [("AHG-1002", 10), ("AHG-1003", 10)],
          times={"855": at("2026-03-04 14:10"), "940": at("2026-03-03 16:00"),
                 "945": at("2026-03-05 10:00"), "856": at("2026-03-05 11:00"),
                 "810": at("2026-03-06 08:00")}),

    # BROKEN: warehouse shipped 26 but only 24 were ordered.
    Order("4500070", "MAPLEPINE", at("2026-03-03 10:00"),
          [("AHG-1001", 24)],
          shipped={"AHG-1001": 26}),

    # Clean, two lines.
    Order("4500091", "BLUEBIRD", at("2026-03-03 09:30"),
          [("AHG-1004", 18), ("AHG-1003", 6)]),

    # Clean but still in progress: 945 arrived this morning, so the 856 is
    # not due yet. The checker should NOT flag it.
    Order("4500063", "MAPLEPINE", at("2026-03-05 09:00"),
          [("AHG-1002", 8), ("AHG-1005", 16)],
          times={"855": at("2026-03-05 13:00"), "940": at("2026-03-05 14:00"),
                 "945": at("2026-03-07 10:05")},
          stop_after="945"),
]


# --- Segment builders, one per transaction set ------------------------------


def ship_to(p: dict) -> list[str]:
    street, city, state, zip_code = p["address"]
    return [
        f"N1*ST*{p['dc_name']}*92*{p['dc_code']}",
        f"N3*{street}",
        f"N4*{city}*{state}*{zip_code}*US",
    ]


def body_850(doc: Doc) -> list[str]:
    o = doc.order
    p = PARTNERS[o.partner]
    seg = [
        f"BEG*00*SA*{o.po}**{o.placed:%Y%m%d}",
        f"REF*DP*{p['dept']}",
        f"DTM*010*{o.placed + timedelta(days=3):%Y%m%d}",
        *ship_to(p),
    ]
    for n, (sku, qty) in enumerate(o.lines, 1):
        desc, price = ITEMS[sku]
        seg.append(f"PO1*{n}*{qty}*EA*{price:.2f}**VN*{sku}*BP*{p['item_prefix']}{sku[-4:]}")
        seg.append(f"PID*F****{desc}")
    seg.append(f"CTT*{len(o.lines)}*{sum(q for _, q in o.lines)}")
    return seg


def body_855(doc: Doc) -> list[str]:
    o = doc.order
    seg = [f"BAK*00*AD*{o.po}*{o.placed:%Y%m%d}****{o.sales_order}*{doc.when:%Y%m%d}"]
    for n, (sku, qty) in enumerate(o.lines, 1):
        _, price = ITEMS[sku]
        seg.append(f"PO1*{n}*{qty}*EA*{price:.2f}**VN*{sku}")
        seg.append(f"ACK*IA*{qty}*EA")
    seg.append(f"CTT*{len(o.lines)}")
    return seg


def body_940(doc: Doc) -> list[str]:
    o = doc.order
    seg = [
        f"W05*N*{o.sales_order}*{o.po}",
        *ship_to(PARTNERS[o.partner]),
        f"G62*10*{o.placed + timedelta(days=3):%Y%m%d}",
        "W66*PP*M",
    ]
    for n, (sku, qty) in enumerate(o.lines, 1):
        desc, _ = ITEMS[sku]
        seg += [f"LX*{n}", f"W01*{qty}*EA**VN*{sku}", f"G69*{desc}"]
    seg.append(f"W76*{sum(q for _, q in o.lines)}")
    return seg


def body_945(doc: Doc) -> list[str]:
    o = doc.order
    p = PARTNERS[o.partner]
    shipped_on = doc.when
    seg = [
        f"W06*F*{o.sales_order}*{shipped_on:%Y%m%d}*RB-{o.po}**{o.po}",
        f"N1*ST*{p['dc_name']}*92*{p['dc_code']}",
        f"G62*11*{shipped_on:%Y%m%d}",
        "W27*M*FAKE",
    ]
    total = 0
    for n, (sku, qty) in enumerate(o.lines, 1):
        shipped = o.shipped_qty(sku)
        total += shipped
        status = "CP" if shipped < qty else "CC"
        seg += [f"LX*{n}", f"W12*{status}*{qty}*{shipped}**EA**VN*{sku}"]
    seg.append(f"W03*{total}*{total * 2}*LB")
    return seg


def body_856(doc: Doc) -> list[str]:
    o = doc.order
    p = PARTNERS[o.partner]
    shipped_on = o.docs["945"].when
    total = sum(o.shipped_qty(sku) for sku, _ in o.lines)
    cartons = math.ceil(total / 12)
    seg = [
        # BSN05 0004 = Shipment, Order, Item hierarchy
        f"BSN*00*ASN-{o.po}*{doc.when:%Y%m%d}*{doc.when:%H%M}*0004",
        "HL*1**S",
        f"TD1*CTN*{cartons}****G*{total * 2}*LB",
        "TD5**2*FAKE*M",
        f"REF*BM*BOL-{o.po}",
        f"DTM*011*{shipped_on:%Y%m%d}",
        f"N1*ST*{p['dc_name']}*92*{p['dc_code']}",
        "N1*SF*ACME HOME GOODS",
        "HL*2*1*O",
        f"PRF*{o.po}",
    ]
    hl = 2
    for n, (sku, _) in enumerate(o.lines, 1):
        hl += 1
        seg += [f"HL*{hl}*2*I", f"LIN*{n}*VN*{sku}", f"SN1**{o.shipped_qty(sku)}*EA"]
    seg.append(f"CTT*{hl}")
    return seg


def body_810(doc: Doc) -> list[str]:
    o = doc.order
    p = PARTNERS[o.partner]
    seg = [
        f"BIG*{doc.when:%Y%m%d}*INV-{o.po}*{o.placed:%Y%m%d}*{o.po}",
        "N1*RE*ACME HOME GOODS",
        f"N1*ST*{p['dc_name']}*92*{p['dc_code']}",
        "ITD*01*3*****30",
    ]
    total_cents = 0
    for n, (sku, _) in enumerate(o.lines, 1):
        _, price = ITEMS[sku]
        qty = o.invoiced_qty(sku)
        total_cents += round(qty * price * 100)
        seg.append(f"IT1*{n}*{qty}*EA*{price:.2f}**VN*{sku}")
    seg += [f"TDS*{total_cents}", f"CTT*{len(o.lines)}"]
    return seg


def body_997(doc: Doc) -> list[str]:
    t = doc.acks
    return [
        f"AK1*{FUNC_ID[t.txset]}*{t.ctrl}",
        f"AK2*{t.txset}*{t.ctrl:04d}",
        "AK5*A",
        "AK9*A*1*1*1",
    ]


BODIES = {
    "850": body_850,
    "855": body_855,
    "940": body_940,
    "945": body_945,
    "856": body_856,
    "810": body_810,
    "997": body_997,
}


# --- Envelope and file writing ----------------------------------------------


def render(doc: Doc) -> str:
    """Wrap a transaction in ISA / GS / ST ... SE / GE / IEA."""
    w = doc.when
    blank = " " * 10
    isa = (
        f"ISA*00*{blank}*00*{blank}"
        f"*ZZ*{doc.sender:<15}*ZZ*{doc.receiver:<15}"
        f"*{w:%y%m%d}*{w:%H%M}*U*00401*{doc.ctrl:09d}*0*T*>"
    )
    gs = f"GS*{FUNC_ID[doc.txset]}*{doc.sender}*{doc.receiver}*{w:%Y%m%d}*{w:%H%M}*{doc.ctrl}*X*004010"
    body = BODIES[doc.txset](doc)
    st_ctrl = f"{doc.ctrl:04d}"
    segments = [
        isa,
        gs,
        f"ST*{doc.txset}*{st_ctrl}",
        *body,
        f"SE*{len(body) + 2}*{st_ctrl}",
        f"GE*1*{doc.ctrl}",
        f"IEA*1*{doc.ctrl:09d}",
    ]
    return "".join(s + "~\n" for s in segments)


def file_name(doc: Doc) -> str:
    """Example: 20260302-0914_850_in_BLUEBIRD_4500012.x12

    The name is only for people browsing the folder. The checker reads the
    file contents, not the name.
    """
    inbound = doc.receiver == ACME
    direction = "in" if inbound else "out"
    other = doc.sender if inbound else doc.receiver
    suffix = f"-ack{doc.acks.txset}" if doc.acks else ""
    return f"{doc.when:%Y%m%d-%H%M}_{doc.txset}_{direction}_{other}_{doc.order.po}{suffix}.x12"


def build_docs() -> list[Doc]:
    docs: list[Doc] = []
    for o in ORDERS:
        times: dict[str, datetime] = {}
        for step in FLOW:
            if step == "850":
                when = o.placed
            else:
                base, gap = GAPS[step]
                when = o.times.get(step, times[base] + gap)
            times[step] = when

            sender, receiver = parties(step, o.partner)
            doc = Doc(when, step, sender, receiver, o)
            o.docs[step] = doc
            docs.append(doc)

            # The receiver sends a 997 back. Acme answers in 1 minute,
            # partners and the warehouse in 5.
            if step not in o.no_997_for:
                lag = timedelta(minutes=1 if receiver == ACME else 5)
                docs.append(Doc(when + lag, "997", receiver, sender, o, acks=doc))

            if step == o.stop_after:
                break

    # Control numbers go up in time order, like a real EDI system.
    docs.sort(key=lambda d: d.when)
    for n, doc in enumerate(docs, start=1001):
        doc.ctrl = n

    late = [d for d in docs if d.when > AS_OF]
    if late:
        raise ValueError(f"{len(late)} documents are dated after AS_OF {AS_OF}")
    return docs


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for old in OUT_DIR.glob("*.x12"):
        old.unlink()

    docs = build_docs()
    for doc in docs:
        (OUT_DIR / file_name(doc)).write_text(render(doc), encoding="ascii", newline="\n")

    print(f"Wrote {len(docs)} files for {len(ORDERS)} orders to {OUT_DIR}")
    print(f"Run the checker as of {AS_OF:%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    main()
