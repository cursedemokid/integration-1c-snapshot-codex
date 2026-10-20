"""Replay captured 1C exports for one logical warehouse mapping."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_excel_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--trade-warehouse", required=True)
    parser.add_argument("--tax-warehouse", required=True)
    parser.add_argument("--mapping-id", default="capture")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    entries = json.loads((args.capture / "summary.json").read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("summary.json must contain captured source entries")

    exports = {}
    period_begin = ""
    period_end = ""
    for entry in entries:
        base = entry["base"]
        body_file = entry.get("body_file")
        if not body_file:
            raise ValueError(f"Capture for {base} does not contain a body file")
        payload = json.loads((args.capture / body_file).read_text(encoding="utf-8"))
        period_begin = entry["parameters"]["periodBegin"]
        period_end = entry["parameters"]["periodEnd"]
        rows = normalize_rows(payload, f"{period_begin}-{period_end}")
        warehouse = args.trade_warehouse if base == "trade" else args.tax_warehouse
        exports[base] = [
            replace(row, warehouse_mapping_id=args.mapping_id)
            for row in rows
            if row.warehouse_code == warehouse
        ]

    if "trade" not in exports:
        raise ValueError("Capture does not contain the trade export")

    result = reconcile(
        exports["trade"],
        {base: rows for base, rows in exports.items() if base != "trade"},
        f"{period_begin}-{period_end}",
    )
    output = args.output or args.capture / "reconciliation_replay.xlsx"
    output.write_bytes(build_excel_report(result))

    summary = {
        "periodBegin": period_begin,
        "periodEnd": period_end,
        "warehouses": {"trade": args.trade_warehouse, "tax": args.tax_warehouse},
        "rows": {base: len(rows) for base, rows in exports.items()},
        "discrepancies": dict(Counter(item.discrepancy_type for item in result.discrepancies)),
        "matching": [asdict(item) for item in result.summaries or []],
        "report": str(output),
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
