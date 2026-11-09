"""The full report: a summary, the list of problems, then a timeline per order.

Run it on the sample data:
    python -m x12_checker sample_data --as-of "2026-03-07 18:00"

Partner names come from a JSON file that maps X12 IDs to names, for example
{"BLUEBIRD": "Bluebird Home Stores"}. By default the tool looks for
partners.json inside the folder being checked. Without it, the report shows
the raw X12 IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from x12_checker.checks import (
    DEFAULT_LIMITS,
    Limits,
    Order,
    Problem,
    ack_verb,
    check_order,
    next_due,
    problem_sort_key,
    verb,
    visible_orders,
)
from x12_checker.parser import Document, ParseResult, parse_folder

NAMES_FILE = "partners.json"


def load_names(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def order_status(order: Order, problems: list[Problem]) -> str:
    if problems:
        return "PROBLEM"
    if order.first("810") is not None:
        return "DONE"
    return "OPEN"


def _timeline_row(doc: Document) -> str:
    if doc.txset == "997":
        targets = ", ".join(t.txset for t in doc.acknowledges)
        direction = ack_verb(doc.acknowledges[0].txset)
        detail = f"for {targets}"
    else:
        direction = verb(doc.txset)
        detail = f"{doc.total_qty:>4} units"
    return f"  {doc.sent_at:%Y-%m-%d %H:%M}  {doc.txset} {direction:<9} {detail}"


def build_report(
    result: ParseResult,
    as_of: datetime,
    names: dict[str, str] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    problems_only: bool = False,
    folder: str = "",
) -> str:
    names = names or {}

    def name(x12_id: str) -> str:
        return names.get(x12_id, x12_id)

    orders = visible_orders(result.documents, as_of)
    found = {po: check_order(order, as_of, limits) for po, order in orders.items()}
    status = {po: order_status(orders[po], found[po]) for po in orders}
    all_problems = sorted((p for ps in found.values() for p in ps), key=problem_sort_key)

    out: list[str] = []
    add = out.append

    # Summary
    add("X12 ORDER LIFECYCLE REPORT")
    add(f"As of {as_of:%Y-%m-%d %H:%M}" + (f"  |  folder: {folder}" if folder else ""))
    counts = {s: sum(1 for v in status.values() if v == s) for s in ("PROBLEM", "DONE", "OPEN")}
    add(f"{len(orders)} order{'' if len(orders) == 1 else 's'}: {counts['PROBLEM']} with problems, "
        f"{counts['DONE']} done, {counts['OPEN']} open")
    if result.errors or result.orphan_acks:
        add(f"{len(result.errors)} unreadable files, "
            f"{len(result.orphan_acks)} 997s that match no document")
    add("")

    # Problem list, like a to-do list
    add(f"PROBLEMS ({len(all_problems)})")
    if not all_problems:
        add("  none")
    for p in all_problems:
        add(f"  {p.kind:<8}  PO {p.po}  {name(p.partner):<22} {p.message}")
    add("")

    # One timeline per order, oldest first
    add("TIMELINES")
    for po, order in sorted(orders.items(), key=lambda kv: kv[1].documents[0].sent_at):
        if problems_only and status[po] != "PROBLEM":
            continue
        add("")
        add(f"PO {po}  {name(order.partner)}  [{status[po]}]")
        for doc in order.documents:
            add(_timeline_row(doc))
        for p in found[po]:
            add(f"  !! {p.kind:<8} {p.message}")
        for txset, deadline in next_due(order, as_of, limits):
            add(f"  .. waiting  {txset} due by {deadline:%Y-%m-%d %H:%M}")

    # Files that could not be used
    if result.errors or result.orphan_acks:
        add("")
        add("FILES")
        for file_name, error in result.errors:
            add(f"  could not read {file_name}: {error}")
        for ack in result.orphan_acks:
            add(f"  {ack.source}: 997 matches no document in the folder")

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m x12_checker",
        description="Follow each order through its X12 documents and report where it got stuck.",
    )
    ap.add_argument("folder", help="folder of X12 files, one transaction per file")
    ap.add_argument("--as-of", help='check as of this time, e.g. "2026-03-07 18:00" (default: now)')
    ap.add_argument("--names", help=f"JSON file of partner names (default: {NAMES_FILE} in the folder)")
    ap.add_argument("--problems-only", action="store_true",
                    help="only show timelines for orders with problems")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        ap.error(f"folder not found: {folder}")
    try:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M") if args.as_of else datetime.now()
    except ValueError:
        ap.error(f'--as-of must look like "2026-03-07 18:00", got {args.as_of!r}')

    names = load_names(Path(args.names) if args.names else folder / NAMES_FILE)
    result = parse_folder(folder)
    print(build_report(result, as_of, names, problems_only=args.problems_only,
                       folder=str(args.folder)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
