"""Group documents into orders and flag the ones that broke.

Everything here is seen from the supplier's side (Acme): it receives the
850 and the 945, and sends the 855, 940, 856 and 810.

Problem types, in the order they are reported:
    MISSING  a step happened, but the next one never did (past its limit)
    QTY      units don't line up: shipped > ordered, or invoiced > shipped
    LATE     a step happened, but after its time limit
    NO 997   a document was never acknowledged (past the 997 limit)
    QUIET    an unfinished order with no activity at all for too long

When an order is QUIET, its MISSING documents are folded into the QUIET
line, because the real problem is that the whole order stopped.

Try it on the sample data:
    python -m x12_checker.checks sample_data --as-of "2026-03-07 18:00"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from x12_checker.parser import Document, parse_folder

H = timedelta(hours=1)

KIND_ORDER = ["MISSING", "QTY", "LATE", "NO 997", "QUIET"]

# Documents the supplier receives. Everything else, the supplier sends.
INBOUND = {"850", "945"}


@dataclass(frozen=True)
class Limits:
    """Time limits from the README. All of them can be changed."""

    ack: timedelta = 24 * H
    steps: tuple[tuple[str, str, timedelta], ...] = (
        ("850", "855", 24 * H),
        ("850", "940", 24 * H),
        ("940", "945", 72 * H),
        ("945", "856", 48 * H),
        ("856", "810", 48 * H),
    )
    quiet: timedelta = 7 * 24 * H


DEFAULT_LIMITS = Limits()


@dataclass
class Order:
    po: str
    documents: list[Document] = field(default_factory=list)  # 997s included, time order

    def first(self, txset: str) -> Document | None:
        return next((d for d in self.documents if d.txset == txset), None)

    @property
    def partner(self) -> str:
        """The retail partner: sender of the 850, or receiver of what Acme sends them."""
        for txset, side in (("850", "sender"), ("855", "receiver"),
                            ("856", "receiver"), ("810", "receiver")):
            doc = self.first(txset)
            if doc:
                return getattr(doc, side)
        return "?"

    @property
    def last_activity(self) -> Document:
        return self.documents[-1]


@dataclass
class Problem:
    kind: str
    po: str
    partner: str
    message: str


# --- Small helpers ----------------------------------------------------------


def verb(txset: str) -> str:
    """Did Acme send or receive this kind of document?"""
    return "received" if txset in INBOUND else "sent"


def ack_verb(txset: str) -> str:
    """Did Acme send or receive the 997 for this kind of document?"""
    # If Acme received the document, Acme owes the 997, and the other way round.
    return "sent" if txset in INBOUND else "received"


def fmt_duration(td: timedelta) -> str:
    """30h, 48h, 3d, 4d 6h. Hours below 3 days, days from there."""
    hours = int(td.total_seconds() // 3600)
    if hours < 72:
        return f"{hours}h"
    days, rest = divmod(hours, 24)
    return f"{days}d" + (f" {rest}h" if rest else "")


# --- Grouping ---------------------------------------------------------------


def group_orders(docs: list[Document]) -> dict[str, Order]:
    """One Order per PO number, documents in time order.

    Documents with no PO (for example a 997 that matched nothing) are left out.
    """
    orders: dict[str, Order] = {}
    for doc in sorted(docs, key=lambda d: d.sent_at):
        if doc.po:
            orders.setdefault(doc.po, Order(doc.po)).documents.append(doc)
    return orders


# --- The checks -------------------------------------------------------------


def _step_checks(order: Order, as_of: datetime, limits: Limits) -> tuple[list[Problem], list[str]]:
    """LATE and MISSING for each step in the flow.

    Returns the problems plus the list of overdue documents, so the QUIET
    check can fold them in.
    """
    problems, overdue = [], []
    for before, after, limit in limits.steps:
        a, b = order.first(before), order.first(after)
        if a is None:
            continue
        if b is not None:
            gap = b.sent_at - a.sent_at
            if gap > limit:
                problems.append(Problem("LATE", order.po, order.partner,
                    f"{after} {verb(after)} {fmt_duration(gap)} after {before} "
                    f"(limit {fmt_duration(limit)})"))
        elif as_of - a.sent_at > limit:
            overdue.append(after)
            problems.append(Problem("MISSING", order.po, order.partner,
                f"{before} {verb(before)}, no {after} {verb(after)}"))
    return problems, overdue


def _qty_checks(order: Order) -> list[Problem]:
    """Shipped > ordered and invoiced > shipped, SKU by SKU.

    "Shipped" comes from the warehouse's 945. If there is no 945, the 856.
    """
    def units(txset: str) -> dict[str, int] | None:
        doc = order.first(txset)
        if doc is None:
            return None
        totals: dict[str, int] = {}
        for line in doc.lines:
            totals[line.sku] = totals.get(line.sku, 0) + line.qty
        return totals

    ordered = units("850")
    shipped = units("945") or units("856")
    invoiced = units("810")

    problems = []
    for bigger, bigger_name, smaller, smaller_name in (
        (shipped, "shipped", ordered, "ordered"),
        (invoiced, "invoiced", shipped, "shipped"),
    ):
        if bigger is None or smaller is None:
            continue
        for sku, qty in bigger.items():
            limit = smaller.get(sku, 0)
            if qty > limit:
                problems.append(Problem("QTY", order.po, order.partner,
                    f"{bigger_name} {qty} > {smaller_name} {limit} for {sku}"))
    return problems


def _ack_checks(order: Order, as_of: datetime, limits: Limits) -> list[Problem]:
    """NO 997 when the 997 is overdue, LATE when it came after the limit."""
    problems = []
    for doc in order.documents:
        if doc.txset == "997":
            continue
        # Ignore 997s that, as of the check time, have not happened yet.
        acks = [a for a in doc.acked_by if a.sent_at <= as_of]
        if acks:
            gap = min(a.sent_at for a in acks) - doc.sent_at
            if gap > limits.ack:
                problems.append(Problem("LATE", order.po, order.partner,
                    f"997 for {doc.txset} {ack_verb(doc.txset)} {fmt_duration(gap)} "
                    f"after it (limit {fmt_duration(limits.ack)})"))
        elif as_of - doc.sent_at > limits.ack:
            problems.append(Problem("NO 997", order.po, order.partner,
                f"{doc.txset} {verb(doc.txset)}, no 997 {ack_verb(doc.txset)}"))
    return problems


def check_order(order: Order, as_of: datetime, limits: Limits = DEFAULT_LIMITS) -> list[Problem]:
    problems: list[Problem] = []

    if order.first("850") is None:
        problems.append(Problem("MISSING", order.po, order.partner,
            "documents on file for this PO, but no 850"))

    step_problems, overdue = _step_checks(order, as_of, limits)

    idle = as_of - order.last_activity.sent_at
    finished = order.first("810") is not None
    if not finished and idle > limits.quiet:
        # Fold the MISSING lines into one QUIET line.
        step_problems = [p for p in step_problems if p.kind != "MISSING"]
        waiting = f", waiting on {', '.join(overdue)}" if overdue else ""
        problems.append(Problem("QUIET", order.po, order.partner,
            f"no activity for {idle.days} days{waiting}"))

    problems += step_problems
    problems += _qty_checks(order)
    problems += _ack_checks(order, as_of, limits)
    return problems


def next_due(order: Order, as_of: datetime, limits: Limits = DEFAULT_LIMITS) -> list[tuple[str, datetime]]:
    """Documents that are not here yet but still inside their time limit,
    with the moment each one becomes overdue."""
    due: dict[str, datetime] = {}
    for before, after, limit in limits.steps:
        a = order.first(before)
        if a is not None and order.first(after) is None and as_of - a.sent_at <= limit:
            deadline = a.sent_at + limit
            due[after] = min(due.get(after, deadline), deadline)
    return sorted(due.items(), key=lambda item: item[1])


def problem_sort_key(p: Problem) -> tuple[int, str]:
    return (KIND_ORDER.index(p.kind), p.po)


def visible_orders(docs: list[Document], as_of: datetime) -> dict[str, Order]:
    """Group into orders, ignoring documents dated after `as_of`, as if they
    had not happened yet."""
    return group_orders([d for d in docs if d.sent_at <= as_of])


def check_documents(
    docs: list[Document], as_of: datetime, limits: Limits = DEFAULT_LIMITS
) -> list[Problem]:
    """Check every order as of the given moment."""
    problems = []
    for order in visible_orders(docs, as_of).values():
        problems += check_order(order, as_of, limits)
    problems.sort(key=problem_sort_key)
    return problems


# --- Command line -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List problems in a folder of X12 files.")
    ap.add_argument("folder")
    ap.add_argument("--as-of", help='check as of this time, e.g. "2026-03-07 18:00" (default: now)')
    args = ap.parse_args(argv)

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M") if args.as_of else datetime.now()
    result = parse_folder(args.folder)
    problems = check_documents(result.documents, as_of)

    for p in problems:
        print(f"{p.kind:<8}  PO {p.po}  {p.partner:<10}  {p.message}")
    print(f"\n{len(problems)} problems as of {as_of:%Y-%m-%d %H:%M}")
    for name, error in result.errors:
        print(f"  could not read {name}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
