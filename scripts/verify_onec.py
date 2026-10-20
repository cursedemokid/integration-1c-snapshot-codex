"""Replay captured responses and independently audit exported numeric values."""

import argparse
import asyncio
import json
import io
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import requests

from app.config import OneCBaseConfig, Settings
from app.onec_client import OneCClient
from app.reconciliation import reconcile
from app.reporting import build_excel_report
from main import ReconcileRequest, run_reconciliation


FIELDS = {
    "opening_quantity": ("КоличествоНачальныйОстаток",),
    "opening_amount": ("СтоимостьНачальныйОстаток", "СуммаНачальныйОстаток"),
    "turnover_quantity": ("КоличествоОборот",),
    "turnover_amount": ("СтоимостьОборот", "СуммаОборот"),
    "closing_quantity": ("КоличествоКонечныйОстаток",),
    "closing_amount": ("СтоимостьКонечныйОстаток", "СуммаКонечныйОстаток"),
}


def number(record, aliases):
    value = next(record[key] for key in aliases if key in record)
    return Decimal("".join(str(value).split()).replace(",", "."))


def balance_diagnostics(rows, begin, end):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.organization_inn, row.nomenclature_code, row.warehouse_code)].append(row)
    failures = []
    for key, group in groups.items():
        opening = [r for r in group if not r.document and r.period == f"{begin:%d.%m.%Y} 0:00:00"]
        closing = [r for r in group if not r.document and r.period == f"{end:%d.%m.%Y} 23:59:59"]
        gaps = {kind: sum((getattr(r, f"closing_{kind}") for r in closing), Decimal(0))
                     - sum((getattr(r, f"opening_{kind}") for r in opening), Decimal(0))
                     - sum((getattr(r, f"turnover_{kind}") for r in group), Decimal(0))
                for kind in ("quantity", "amount")}
        if any(gaps.values()):
            failures.append({"organization_inn": key[0], "nomenclature_code": key[1],
                             "warehouse_code": key[2], "opening_rows": len(opening),
                             "closing_rows": len(closing), "gaps": gaps,
                             "source_rows": [asdict(r) for r in group]})
    return failures


def terminal_balance(rows, side):
    # Reconstruct the endpoints of an unordered balance-transition multigraph.
    # Do not infer the endpoint from the control total being tested.
    degree = Counter()
    neighbors = defaultdict(set)
    nodes = set()
    for row in rows:
        start = (row.opening_quantity, row.opening_amount)
        end = (row.closing_quantity, row.closing_amount)
        nodes.update((start, end))
        degree[start] += 1
        degree[end] -= 1
        neighbors[start].add(end)
        neighbors[end].add(start)
    if len(nodes) == 1:
        return next(iter(nodes))
    visited, pending = set(), [next(iter(nodes))]
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(neighbors[node] - visited)
    heads = [node for node, count in degree.items() if count == 1]
    tails = [node for node, count in degree.items() if count == -1]
    if visited != nodes or len(heads) != 1 or len(tails) != 1 or any(abs(n) > 1 for n in degree.values()):
        return None
    return heads[0] if side == "opening" else tails[0]


def period_balances(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.organization_inn, row.nomenclature_code, row.warehouse_code)].append(row)
    result, ambiguous = [], []
    for key, group in groups.items():
        times = {r.period: datetime.strptime(r.period, "%d.%m.%Y %H:%M:%S") for r in group}
        first, last = min(times, key=times.get), max(times, key=times.get)
        opening = terminal_balance([r for r in group if r.period == first], "opening")
        closing = terminal_balance([r for r in group if r.period == last], "closing")
        if opening is None or closing is None:
            ambiguous.append(key)
            continue
        turnover = (sum((r.turnover_quantity for r in group), Decimal(0)),
                    sum((r.turnover_amount for r in group), Decimal(0)))
        result.append({"organization_inn": key[0], "nomenclature_code": key[1], "warehouse_code": key[2],
                       "opening_quantity": opening[0], "opening_amount": opening[1],
                       "closing_quantity": closing[0], "closing_amount": closing[1],
                       "turnover_quantity": turnover[0], "turnover_amount": turnover[1],
                       "quantity_gap": closing[0] - opening[0] - turnover[0],
                       "amount_gap": closing[1] - opening[1] - turnover[1]})
    return result, ambiguous


def workbook_rows(content):
    namespace = {"s": "http://internal.internal.example.invalid/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet_names = [sheet.attrib["name"] for sheet in workbook.findall("s:sheets/s:sheet", namespace)]
        result = {}
        for index, name in enumerate(sheet_names, start=1):
            sheet = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            rows = []
            for row in sheet.findall("s:sheetData/s:row", namespace):
                values = []
                for cell in row.findall("s:c", namespace):
                    inline = cell.find("s:is/s:t", namespace)
                    number_value = cell.find("s:v", namespace)
                    values.append(
                        inline.text or "" if inline is not None
                        else number_value.text or "" if number_value is not None
                        else ""
                    )
                rows.append(values)
            result[name] = rows
        return result


def verify_diagnostic_excel(content, expected):
    rows = workbook_rows(content)["Расхождения"][1:]
    assert len(rows) == len(expected.discrepancies)
    numeric_columns = {
        "opening_quantity": (17, 18),
        "turnover_quantity": (20, 21),
        "closing_quantity": (23, 24),
        "opening_amount": (26, 27),
        "turnover_amount": (29, 30),
        "closing_amount": (32, 33),
    }
    for excel_row, item in zip(rows, expected.discrepancies):
        for source_row, side in ((item.trade_row, 0), (item.tax_row, 1)):
            if source_row is None:
                continue
            for field, columns in numeric_columns.items():
                assert Decimal(excel_row[columns[side]]) == getattr(source_row, field)
        row = item.tax_row or item.trade_row
        for column, value in ((9, row.nomenclature_code), (10, row.article),
                              (11, row.nomenclature), (12, row.warehouse_mapping_id)):
            assert excel_row[column] == value
    return {
        "rows": len(rows),
        "side_by_side_numeric_columns_and_identifiers_verified": True,
        "visual_review": "not_performed",
    }


def verify_action_excel(content, expected):
    sheets = workbook_rows(content)
    action_sheets = ("Довнести в БУХ", "Лишние в БУХ", "Проверить вручную")
    assert all(name in sheets for name in ("Итоги", "Расхождения остатков", *action_sheets))
    assert list(sheets)[:5] == [
        "Итоги",
        "Расхождения остатков",
        "Довнести в БУХ",
        "Лишние в БУХ",
        "Проверить вручную",
    ]
    summary = sheets["Итоги"]
    declared = {
        (row[0], row[1]): int(row[2])
        for row in summary
        if len(row) >= 3 and row[0] in action_sheets
    }
    actual = Counter(
        (sheet_name, row[1])
        for sheet_name in action_sheets
        for row in sheets[sheet_name][1:]
    )
    assert declared == actual
    metrics = {row[0]: int(row[1]) for row in summary if len(row) == 2 and row[1].isdigit()}
    assert metrics["Требуют исправления"] == sum(
        len(sheets[name]) - 1 for name in action_sheets[:2]
    )
    assert metrics["Требуют ручной проверки"] == len(sheets[action_sheets[2]]) - 1
    assert metrics["Баз проверено"] == len(expected.summaries or [])
    assert metrics["Баз не проверено"] == 0
    balance_rows = sheets["Расхождения остатков"]
    assert balance_rows[0][:6] == [
        "Дата расхождения",
        "Артикул",
        "Наименование",
        "Остаток в УТ",
        "Остаток в НБ",
        "Расхождение",
    ]
    assert not any("Сумма" in header or "Стоимость" in header for header in balance_rows[0])
    balance_by_base = Counter(row[6] for row in balance_rows[1:])
    statistics_header = next(
        index for index, row in enumerate(summary)
        if row and row[0] == "База" and row[-1] == "Расхождений остатков"
    )
    declared_balance_by_base = {
        row[0]: int(row[-1])
        for row in summary[statistics_header + 1:]
        if row and row[0] in {item.base for item in expected.summaries or []}
    }
    assert declared_balance_by_base == {
        base: balance_by_base[base]
        for base in declared_balance_by_base
    }
    return {
        "sheets": list(sheets),
        "action_rows": sum(len(sheets[name]) - 1 for name in action_sheets),
        "balance_rows": len(balance_rows) - 1,
        "summary_matches_action_sheets": True,
        "summary_matches_balance_sheet": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    entries = json.loads((args.capture / "summary.json").read_text(encoding="utf-8"))
    exports, balances_by_base, responses, configs = {}, {}, {}, []
    audit = {"source": str(args.capture), "bases": {}, "comparisons": {}}
    for entry in entries:
        assert entry["status"] == 200 and entry["transport"] == "json"
        body = (args.capture / entry["body_file"]).read_bytes()
        payload = json.loads(body, parse_float=Decimal)
        records = payload["data"]
        begin = datetime.strptime(entry["parameters"]["periodBegin"], "%Y%m%d").date()
        end = datetime.strptime(entry["parameters"]["periodEnd"], "%Y%m%d").date()
        base = OneCBaseConfig(name=entry["base"], url=entry["url"], request_payload="json",
                             date_format="%Y%m%d", period_begin_key="periodBegin", period_end_key="periodEnd")
        configs.append(base)
        response = requests.Response()
        response.status_code = 200
        response._content = body
        responses[base.url] = response
        with patch("app.onec_client.requests.get", return_value=response):
            rows = OneCClient().fetch_rows(base, begin, end)
            assert rows == OneCClient().fetch_rows(base, begin, end)
        assert len(rows) == len(records)
        for index, (record, row) in enumerate(zip(records, rows)):
            for field, aliases in FIELDS.items():
                assert number(record, aliases) == getattr(row, field), (base.name, index, field)
        periods = Counter(row.period for row in rows)
        outside = [p for p in periods if not begin <= datetime.strptime(p, "%d.%m.%Y %H:%M:%S").date() <= end]
        assert not outside, (base.name, "period mismatch", outside)
        grouped = defaultdict(list)
        for row in rows:
            grouped[row.match_key].append(row)
        collisions = [group for group in grouped.values() if len({r.warehouse_code for r in group}) > 1]
        boundary = {}
        for label, day, field in (("opening", begin, "opening"), ("closing", end, "closing")):
            clock = "0:00:00" if label == "opening" else "23:59:59"
            selected = [r for r in rows if r.period == f"{day:%d.%m.%Y} {clock}" and not r.document]
            boundary[label] = {
                "rows": len(selected),
                "quantity": sum((getattr(r, field + "_quantity") for r in selected), Decimal(0)),
                "amount": sum((getattr(r, field + "_amount") for r in selected), Decimal(0)),
            }
        audit["bases"][base.name] = {
            "raw_rows": len(records), "normalized_rows": len(rows), "all_numeric_values_preserved": True,
            "unique_match_keys": len(grouped), "cross_warehouse_key_collisions": len(collisions),
            "organization_inns": sorted({row.organization_inn for row in rows}),
            "boundary_totals": boundary,
            "integrity_sums_not_period_balances": {f: sum((getattr(r, f) for r in rows), Decimal(0)) for f in FIELDS},
        }
        client_file = args.capture / f"{base.name}.client.json"
        if client_file.exists():
            key = lambda r: json.dumps(r, sort_keys=True, ensure_ascii=True, default=str)
            stored_rows = json.loads(client_file.read_text(encoding="utf-8"))
            stored_fields = set(stored_rows[0]) if stored_rows else set()
            direct = Counter(key({field: asdict(r)[field] for field in stored_fields}) for r in rows)
            client = Counter(key(row) for row in stored_rows)
            audit["bases"][base.name]["live_client_multiset_matches_direct"] = direct == client
            assert direct == client, (base.name, "live client differs")
        audit["bases"][base.name]["boundary_rollforward_gap"] = {
            kind: boundary["closing"][kind] - boundary["opening"][kind]
                  - sum((getattr(r, f"turnover_{kind}") for r in rows), Decimal(0))
            for kind in ("quantity", "amount")
        }
        failures = balance_diagnostics(rows, begin, end)
        (args.capture / f"{base.name}.balance_diagnostics.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        audit["bases"][base.name]["balance_control_failed_groups"] = len(failures)
        balances, ambiguous = period_balances(rows)
        balances_by_base[base.name] = balances
        (args.capture / f"{base.name}.period_balances.json").write_text(
            json.dumps({"balances": balances, "ambiguous_keys": ambiguous}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        audit["bases"][base.name]["transition_based_control"] = {
            "ambiguous_groups": len(ambiguous),
            "failed_groups": sum(bool(r["quantity_gap"] or r["amount_gap"]) for r in balances),
            "totals": {field: sum((r[field] for r in balances), Decimal(0)) for field in FIELDS},
        }
        audit["bases"][base.name]["warehouse_directory_from_rows"] = sorted(
            { (r.warehouse_code, r.warehouse) for r in rows }
        )
        exports[base.name] = rows
    for name, rows in exports.items():
        if name == "trade":
            continue
        result = reconcile(exports["trade"], {name: rows}, period=f"{begin}/{end}")
        audit["comparisons"][name] = {
            "status": "diagnostic_only_pending_scope_and_document_mapping",
            "counts": dict(Counter(d.discrepancy_type for d in result.discrepancies)),
            "first_10": [asdict(d) for d in result.discrepancies[:10]],
        }
        trade_balances = {(r["nomenclature_code"], r["warehouse_code"]): r
                          for r in balances_by_base["trade"]}
        tax_balances = {(r["nomenclature_code"], r["warehouse_code"]): r
                        for r in balances_by_base[name]}
        common = trade_balances.keys() & tax_balances.keys()
        quantity_fields = ("opening_quantity", "turnover_quantity", "closing_quantity")
        amount_fields = ("opening_amount", "turnover_amount", "closing_amount")
        audit["comparisons"][name]["same_code_balance_comparison"] = {
            "matched_item_warehouse_pairs": len(common),
            "common_warehouse_codes": sorted({warehouse for _, warehouse in common}),
            "all_quantity_fields_equal": sum(
                all(Decimal(trade_balances[key][field]) == Decimal(tax_balances[key][field])
                    for field in quantity_fields) for key in common),
            "all_amount_fields_within_tolerance": sum(
                all(abs(Decimal(trade_balances[key][field]) - Decimal(tax_balances[key][field]))
                    <= Decimal("0.01") for field in amount_fields) for key in common),
            "closing_quantity_equal": sum(
                Decimal(trade_balances[key]["closing_quantity"])
                == Decimal(tax_balances[key]["closing_quantity"]) for key in common),
            "closing_amount_within_tolerance": sum(
                abs(Decimal(trade_balances[key]["closing_amount"])
                    - Decimal(tax_balances[key]["closing_amount"])) <= Decimal("0.01") for key in common),
        }
    with patch("main.load_settings", return_value=Settings(configs[0], configs[1:])), \
         patch("app.onec_client.requests.get", side_effect=lambda url, **kwargs: responses[url]):
        result = asyncio.run(run_reconciliation(ReconcileRequest(start_date=begin, end_date=end)))
    (args.capture / "reconciliation_actions.xlsx").write_bytes(result.body)
    expected = reconcile(exports["trade"], {k: v for k, v in exports.items() if k != "trade"}, f"{begin}/{end}")
    diagnostic_report = build_excel_report(expected)
    (args.capture / "reconciliation_diagnostic.xlsx").write_bytes(diagnostic_report)
    audit["excel"] = verify_diagnostic_excel(diagnostic_report, expected)
    audit["action_excel"] = verify_action_excel(result.body, expected)
    (args.capture / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"bases": {k: {f: v for f, v in b.items() if f != "warehouse_directory_from_rows"} for k, b in audit["bases"].items()}, "excel": audit["excel"], "action_excel": audit["action_excel"], "comparisons": {
        k: {"status": v["status"], "counts": v["counts"]} for k, v in audit["comparisons"].items()
    }}, ensure_ascii=True, default=str, indent=2))


if __name__ == "__main__":
    main()
