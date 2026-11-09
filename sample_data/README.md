# Sample data

123 fake X12 files covering 12 orders for **Acme Home Goods**, one transaction per file. Every company, partner, SKU, PO number and address is made up.

These files were made by [`tools/make_sample_data.py`](../tools/make_sample_data.py). To rebuild them, run this from the repo root:

```
python tools/make_sample_data.py
```

## Who's who

| X12 ID | Name | Role |
|---|---|---|
| `ACMEHOME` | Acme Home Goods | Supplier (us) |
| `BLUEBIRD` | Bluebird Home Stores | Retail partner |
| `MAPLEPINE` | Maple and Pine Market | Retail partner |
| `TIDEWATER` | Tidewater Living | Retail partner |
| `RIVERBEND` | Riverbend Fulfillment | 3PL warehouse |

The report reads these names from [`partners.json`](partners.json), which is written by hand. The generator script only replaces the `.x12` files.

## Format

- X12 version 004010, test indicator `T` in the ISA
- Separators: `*` between elements, `~` at the end of each segment, `>` for sub-elements
- One segment per line, so the files are easy to read
- 997s point back to the document they acknowledge through the GS control number (AK1) and the ST control number (AK2)

File names look like `20260302-0914_850_in_BLUEBIRD_4500012.x12` (date-time, transaction set, direction from Acme's side, other party, PO). They are only there to make the folder easy to browse. The checker reads what's inside the files, not the names.

## Answer key

Run the checker **as of 2026-03-07 18:00**. At that moment it should find exactly these problems:

| PO | Partner | Expected flag | Why |
|---|---|---|---|
| 4500012 | Bluebird | MISSING | 945 received 2026-03-05 17:22, no 856 sent (due within 48h) |
| 4500082 | Tidewater | MISSING | 856 sent 2026-03-01 18:30, no 810 sent (due within 48h) |
| 4500031 | Maple and Pine | QTY | Invoiced 50, but only 48 shipped |
| 4500070 | Maple and Pine | QTY | Shipped 26, but only 24 ordered |
| 4500044 | Tidewater | LATE | 855 sent 30h after the 850 (limit 24h) |
| 4500050 | Bluebird | NO 997 | 850 received, Acme never sent a 997 back |
| 4500057 | Tidewater | QUIET | 850 received 2026-02-27, nothing for 8 days (so the 855 is also missing) |

These orders should come out **clean**:

| PO | Partner | Note |
|---|---|---|
| 4500018 | Maple and Pine | Full flow |
| 4500025 | Tidewater | Full flow |
| 4500038 | Bluebird | Full flow, three lines |
| 4500091 | Bluebird | Full flow |
| 4500063 | Maple and Pine | In progress: 945 arrived 2026-03-07 10:05, so the 856 is not due until 2026-03-09 |

Two traps to watch for:

- **4500012** also ships short (118 of 120). Shipping less than ordered is not a problem by itself. The only problem on this order is the missing 856.
- **4500063** has no 856 yet, but it is still inside its time limit, so it should not be flagged.
