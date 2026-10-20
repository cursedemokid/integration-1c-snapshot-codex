import asyncio
import io
import json
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch
from xml.etree import ElementTree as ET

import requests

from app.config import (
    ConfigurationError,
    OneCBaseConfig,
    Settings,
    WarehouseMappingConfig,
    load_settings,
)
from app.models import Warehouse
from app.normalization import NormalizationError, normalize_rows
from app.onec_client import OneCClient, OneCClientError, normalize_warehouses
from app.reconciliation import AMBIGUOUS_MATCH, DOCUMENT_QUANTITY_MISMATCH, reconcile
from main import app
from scripts.verify_onec import workbook_rows


BASE = OneCBaseConfig(
    name="Trade", url="https://internal.example.invalid/endpoint/export", base_id="trade", request_payload="json",
    date_format="%Y%m%d", period_begin_key="periodBegin", period_end_key="periodEnd",
)
BEGIN, END = date(2026, 3, 1), date(2026, 4, 1)
RECORD = {
    "period": "01.03.2026 0:00:00",
    "document": "Реализация товаров и услуг УТ1 от 01.03.2026 0:00:00",
    "nomenclature": "Item 1",
    "opening_quantity": "0.125", "opening_amount": "10.025",
    "turnover_quantity": "1", "turnover_amount": "20.025",
    "closing_quantity": "1.125", "closing_amount": "30.050",
}


def response(payload=None, status=200, body=None):
    result = requests.Response()
    result.status_code = status
    result._content = body if body is not None else json.dumps(payload).encode()
    result.encoding = "utf-8"
    result.url = BASE.url
    return result


async def asgi_request(path, payload=None, method="POST", include_headers=False):
    sent = []

    async def receive():
        body = b"" if payload is None else json.dumps(payload).encode()
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": method, "scheme": "http", "path": path,
               "raw_path": path.encode(), "query_string": b"", "root_path": "",
               "headers": [(b"content-type", b"application/json")],
               "client": ("127.0.0.1", 1), "server": ("test", 80)}, receive, send)
    body = b"".join(m.get("body", b"") for m in sent[1:])
    if include_headers:
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in sent[0].get("headers", [])
        }
        return sent[0]["status"], headers, body
    return sent[0]["status"], body


class ClientContractTests(TestCase):
    def test_default_timeout_is_release_value(self):
        self.assertEqual(OneCClient().timeout_seconds, 120.0)

    @patch("app.onec_client.requests.get")
    def test_get_json_period_without_warehouse(self, get):
        get.return_value = response({"code": 200, "data": [RECORD]})
        rows = OneCClient().fetch_rows(BASE, BEGIN, END)
        self.assertEqual(rows[0].opening_quantity, Decimal("0.125"))
        self.assertEqual(get.call_args.kwargs["json"], {"periodBegin": "20260301", "periodEnd": "20260401"})
        self.assertNotIn("params", get.call_args.kwargs)

    @patch("app.onec_client.requests.get")
    def test_get_json_with_single_warehouse_and_mapping(self, get):
        get.return_value = response({"code": 200, "data": [RECORD | {"warehouse_code": "W1"}]})
        row = OneCClient().fetch_rows(
            replace(BASE, supports_warehouse_filter=True), BEGIN, END, " W1 ", "main"
        )[0]
        self.assertEqual(
            get.call_args.kwargs["json"],
            {"periodBegin": "20260301", "periodEnd": "20260401", "warehouse": "W1"},
        )
        self.assertEqual(row.warehouse_mapping_id, "main")

    @patch("app.onec_client.requests.get")
    def test_utf8_auth(self, get):
        import base64
        get.return_value = response([])
        base = replace(BASE, username="Пользователь", REDACTED="Пароль")
        OneCClient().fetch_rows(base, BEGIN, END)
        encoded = get.call_args.kwargs["headers"]["Authorization"].split()[1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), "Пользователь:Пароль")

    @patch("app.onec_client.requests.get")
    def test_source_errors_never_become_empty_success(self, get):
        for value in (response(body=b""), response(body=b"not json"),
                      response({"code": 500, "data": []}), response([], status=401),
                      response([], status=500), response([{"opening_quantity": "1"}])):
            with self.subTest(body=value.content):
                get.return_value = value
                with self.assertRaises(OneCClientError):
                    OneCClient().fetch_rows(BASE, BEGIN, END)
        get.side_effect = requests.Timeout("test timeout")
        with self.assertRaises(OneCClientError):
            OneCClient().fetch_rows(BASE, BEGIN, END)

    @patch("app.onec_client.requests.get")
    def test_empty_valid_export(self, get):
        get.return_value = response({"code": 200, "data": []})
        self.assertEqual(OneCClient().fetch_rows(BASE, BEGIN, END), [])

    @patch("app.onec_client.requests.get")
    @patch("app.onec_client.requests.post")
    def test_unsupported_filter_fails_before_network(self, post, get):
        for base in (BASE, replace(BASE, request_method="POST")):
            with self.assertRaises(OneCClientError):
                OneCClient().fetch_rows(base, BEGIN, END, "W1")
        get.assert_not_called()
        post.assert_not_called()

    @patch("app.onec_client.requests.get")
    def test_filtered_response_with_wrong_warehouse_fails(self, get):
        get.return_value = response({"code": 200, "data": [RECORD | {"warehouse_code": "W2"}]})
        with self.assertRaisesRegex(OneCClientError, "не по складу W1"):
            OneCClient().fetch_rows(replace(BASE, supports_warehouse_filter=True), BEGIN, END, "W1")

    @patch("app.onec_client.requests.get")
    def test_forbidden_response_explains_onec_method_permission(self, get):
        get.return_value = response(body="Недостаточно прав".encode("utf-8"), status=403)
        with self.assertRaisesRegex(OneCClientError, "нет права на HTTP-метод GET"):
            OneCClient().fetch_rows(BASE, BEGIN, END)


class NormalizationContractTests(TestCase):
    def test_buh_aliases_and_dimensions(self):
        data = RECORD | {"СуммаНачальныйОстаток": "10,025", "СуммаОборот": "20,025",
                         "СуммаКонечныйОстаток": "30,050", "СкладКод": "001",
                         "Склад": "Warehouse", "НоменклатураКод": "002", "ОрганизацияИНН": "003"}
        for key in ("opening_amount", "turnover_amount", "closing_amount"):
            del data[key]
        row = normalize_rows({"code": 200, "data": [data]}, "period")[0]
        self.assertEqual(row.closing_amount, Decimal("30.050"))
        self.assertEqual((row.warehouse_code, row.nomenclature_code, row.organization_inn), ("001", "002", "003"))

    def test_rejects_nonfinite_and_missing_numbers(self):
        for value in ("NaN", "Infinity", "-Infinity", "sNaN", "", None):
            with self.subTest(value=value), self.assertRaises(NormalizationError):
                normalize_rows([RECORD | {"closing_amount": value}], "period")


class WarehouseAndMatchingTests(TestCase):
    def test_different_organizations_do_not_match(self):
        rows = normalize_rows([RECORD | {"organization_inn": "111"}], "period")
        result = reconcile(rows, {"Tax": [replace(rows[0], organization_inn="222")]}, "period")
        self.assertEqual(result.discrepancies[0].discrepancy_type, AMBIGUOUS_MATCH)

    def test_two_warehouses_are_not_duplicates(self):
        rows = normalize_rows([RECORD | {"warehouse_code": "W1"}, RECORD | {"warehouse_code": "W2"}], "period")
        self.assertEqual(reconcile(rows, {"Tax": rows}, "period").discrepancies, [])

    def test_same_name_different_item_codes_remain_distinct(self):
        rows = normalize_rows([RECORD | {"nomenclature_code": "N1"}, RECORD | {"nomenclature_code": "N2"}], "period")
        self.assertEqual(reconcile(rows, {"Tax": rows}, "period").discrepancies, [])

    def test_item_name_change_with_same_code_matches(self):
        rows = normalize_rows([RECORD | {"nomenclature_code": "N1"}], "period")
        self.assertEqual(reconcile(rows, {"Tax": [replace(rows[0], nomenclature="New name")]}, "period").discrepancies, [])

    def test_quantity_difference_is_not_hidden_by_amount_tolerance(self):
        rows = normalize_rows([RECORD], "period")
        tax = replace(rows[0], turnover_quantity=Decimal("1.001"))
        differences = reconcile(rows, {"Tax": [tax]}, "period").discrepancies
        self.assertEqual(differences[0].discrepancy_type, DOCUMENT_QUANTITY_MISMATCH)
        self.assertEqual(reconcile(rows, {"Tax": [tax]}, "period", quantity_tolerance=Decimal("0.001")).discrepancies, [])

    def test_same_warehouse_names_with_distinct_codes_are_preserved(self):
        warehouses = normalize_warehouses(
            [{"Склад": "Warehouse", "Код": "W1"}, {"Склад": "Warehouse", "Код": "W2"}]
        )
        self.assertEqual([item.code for item in warehouses], ["W1", "W2"])

    def test_duplicate_warehouse_codes_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_warehouses([{"name": "One", "id": "W1"}, {"name": "Two", "id": "W1"}])

    def test_identical_warehouse_records_are_deduplicated(self):
        record = {"Склад": "Склад Ламода", "Код": "УПР000253", "Группа": ""}
        self.assertEqual(
            normalize_warehouses({"code": 200, "data": [record, record.copy()]}),
            [Warehouse("УПР000253", "Склад Ламода")],
        )

    @patch("app.onec_client.requests.get")
    def test_warehouse_directory_errors_and_empty_response(self, get):
        base = replace(BASE, warehouses_url="https://internal.example.invalid/endpoint/warehouses")
        for reply in (response(body=b""), response([], status=401), response({"code": 500, "data": []})):
            with self.subTest(body=reply.content):
                get.return_value = reply
                with self.assertRaises(OneCClientError):
                    OneCClient().fetch_warehouses(base)
        get.return_value = response({"code": 200, "data": []})
        self.assertEqual(OneCClient().fetch_warehouses(base), [])

        get.return_value = response(body="Недостаточно прав".encode("utf-8"), status=403)
        with self.assertRaisesRegex(OneCClientError, "нет права на HTTP-метод GET"):
            OneCClient().fetch_warehouses(base)

    def test_normalizes_live_warehouse_contract(self):
        self.assertEqual(
            normalize_warehouses({"code": 200, "data": [{
                "Склад": " Основной  склад ",
                "Код": " W1 ",
                "Группа": " G ",
                "СкладБухгалтерия": " Бухгалтерский  склад ",
            }]}),
            [Warehouse(
                code="W1",
                name="Основной склад",
                group="G",
                accounting_name="Бухгалтерский склад",
            )],
        )


class EndpointContractTests(TestCase):
    def setUp(self):
        self.mapping = WarehouseMappingConfig(
            "main", "Main", {"trade": "T1", "buh2019": "B1", "buh2026": "B1"}
        )
        self.settings = Settings(
            BASE,
            [replace(BASE, name="BUH2019", base_id="buh2019"), replace(BASE, name="BUH2026", base_id="buh2026")],
            warehouse_mappings=[self.mapping],
        )
        self.payload = {"start_date": "2026-03-01", "end_date": "2026-04-01", "warehouses": []}

    def test_web_form_requires_mapped_warehouse_and_exposes_report_download(self):
        status, body = asyncio.run(asgi_request("/", method="GET"))
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('id="reconcile-form"', html)
        self.assertIn('id="download-report"', html)
        self.assertIn("Сверка документов 1С", html)
        self.assertIn("Сформировать список исправлений", html)
        self.assertIn("Выберите хотя бы один сопоставленный склад", html)
        self.assertIn("downloadReportLink.click()", html)
        self.assertIn('baseNames[baseId] || baseId', html)
        self.assertIn("Не проверено баз", html)
        self.assertIn('id="warehouse-diagnostics"', html)
        self.assertIn("startReconciliationTimer", html)
        self.assertNotIn('role="progressbar"', html)
        self.assertIn('message === "Internal Server Error"', html)
        self.assertIn("Перезапустите приложение и повторите запрос", html)

    @patch("main.build_action_excel_report", side_effect=RuntimeError("test failure"))
    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_unexpected_report_error_returns_explanation_instead_of_plain_500(
        self,
        get,
        settings,
        _build_report,
    ):
        settings.return_value = self.settings
        get.side_effect = [response([RECORD]), response([RECORD]), response([RECORD])]

        with self.assertLogs("main", level="ERROR"):
            status, headers, body = asyncio.run(
                asgi_request("/reconcile", self.payload, include_headers=True)
            )

        self.assertEqual(status, 500)
        self.assertIn("application/json", headers["content-type"])
        detail = json.loads(body)["detail"]
        self.assertIn("Не удалось сформировать Excel-отчёт", detail)
        self.assertIn("журнал сервера", detail)
        self.assertNotEqual(detail, "Internal Server Error")

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_standard_report_excludes_technical_value_differences(self, get, settings):
        settings.return_value = self.settings
        get.side_effect = [response([RECORD]), response([RECORD]),
                           response([RECORD | {"turnover_amount": "21.025", "closing_amount": "31.075"}])]
        status, body = asyncio.run(asgi_request("/reconcile", self.payload))
        self.assertEqual(status, 200)
        self.assertEqual(get.call_count, 3)
        with zipfile.ZipFile(io.BytesIO(body)) as workbook:
            for name in workbook.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(workbook.read(name))
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            summary = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            action_sheets = [
                workbook.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
                for index in range(3, 6)
            ]
            balance_sheet = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn('name="Расхождения остатков"', workbook_xml)
            self.assertIn('name="Довнести в БУХ"', workbook_xml)
            self.assertIn('name="Лишние в БУХ"', workbook_xml)
            self.assertIn('name="Проверить вручную"', workbook_xml)
            self.assertIn("Статистика по базам", summary)
            self.assertIn("Документов УТ", summary)
            self.assertIn("Сопоставлено", summary)
            self.assertTrue(all(sheet.count("<row ") == 1 for sheet in action_sheets))
            self.assertTrue(all("31.075" not in sheet for sheet in action_sheets))
            self.assertEqual(balance_sheet.count("<row "), 1)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_report_contains_separate_opening_and_closing_balance_rows_by_base(self, get, settings):
        settings.return_value = self.settings

        def snapshot(period, code, article, quantity, amount, warehouse):
            return RECORD | {
                "period": period,
                "document": "",
                "nomenclature": f"Item {code}",
                "nomenclature_code": code,
                "article": article,
                "warehouse": warehouse,
                "warehouse_code": "LOGICAL",
                "opening_quantity": str(quantity),
                "opening_amount": str(amount),
                "turnover_quantity": "0",
                "turnover_amount": "0",
                "closing_quantity": str(quantity),
                "closing_amount": str(amount),
            }

        trade_rows = [
            snapshot("01.03.2026 0:00:00", "N1", "ART-1", 10, 100, "Warehouse UT"),
            snapshot("01.04.2026 23:59:59", "N1", "ART-1", 15, 150, "Warehouse UT"),
            snapshot("01.03.2026 0:00:00", "MATCH", "ART-M", 5, 50, "Warehouse UT"),
        ]
        buh2019_rows = [
            snapshot("01.03.2026 0:00:00", "N1", "ART-1", 8, 80, "Warehouse BUH2019"),
            snapshot("01.04.2026 23:59:59", "N1", "ART-1", 15, 150, "Warehouse BUH2019"),
            snapshot("01.03.2026 0:00:00", "MATCH", "ART-M", 5, 50, "Warehouse BUH2019"),
        ]
        buh2026_rows = [
            snapshot("01.03.2026 0:00:00", "N1", "ART-1", 10, 100, "Warehouse BUH2026"),
            snapshot("01.04.2026 23:59:59", "N1", "ART-1", 14, 140, "Warehouse BUH2026"),
            snapshot("01.03.2026 0:00:00", "MATCH", "ART-M", 5, 50, "Warehouse BUH2026"),
        ]
        get.side_effect = [response(trade_rows), response(buh2019_rows), response(buh2026_rows)]

        status, body = asyncio.run(asgi_request("/reconcile", self.payload))

        self.assertEqual(status, 200)
        sheets = workbook_rows(body)
        self.assertEqual(
            list(sheets),
            ["Итоги", "Расхождения остатков", "Довнести в БУХ", "Лишние в БУХ", "Проверить вручную"],
        )
        balance_rows = sheets["Расхождения остатков"]
        self.assertEqual(len(balance_rows), 3)
        by_base = {row[6]: row for row in balance_rows[1:]}
        self.assertEqual(by_base["BUH2019"][:9], [
            "01.03.2026", "ART-1", "Item N1", "10", "8", "-2",
            "BUH2019", "Начальный остаток", "Не совпадает количество",
        ])
        self.assertEqual(by_base["BUH2019"][10:13], ["N1", "Warehouse UT", "Warehouse BUH2019"])
        self.assertEqual(by_base["BUH2026"][:9], [
            "01.04.2026", "ART-1", "Item N1", "15", "14", "-1",
            "BUH2026", "Конечный остаток", "Не совпадает количество",
        ])
        self.assertNotIn("Item MATCH", " ".join(" ".join(row) for row in balance_rows))

        summary_rows = sheets["Итоги"]
        statistics_header = next(
            index for index, row in enumerate(summary_rows)
            if row and row[0] == "База" and row[-1] == "Расхождений остатков"
        )
        statistics = {row[0]: row for row in summary_rows[statistics_header + 1:] if row and row[0] in {"BUH2019", "BUH2026"}}
        self.assertEqual(statistics["BUH2019"][-1], "1")
        self.assertEqual(statistics["BUH2026"][-1], "1")

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_report_uses_utf8_russian_download_filename(self, get, settings):
        settings.return_value = self.settings
        get.side_effect = [response([RECORD]), response([RECORD]), response([RECORD])]
        status, headers, body = asyncio.run(
            asgi_request("/reconcile", self.payload, include_headers=True)
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        disposition = headers["content-disposition"]
        self.assertIn('filename="report_2026-03-01_2026-04-01.xlsx"', disposition)
        self.assertIn(
            "filename*=UTF-8''%D0%A1%D0%BF%D0%B8%D1%81%D0%BE%D0%BA_"
            "%D0%B4%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D0%BE%D0%B2_",
            disposition,
        )

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_source_failure_does_not_block_another_accounting_base(self, get, settings):
        settings.return_value = self.settings
        get.side_effect = [response([RECORD]), response(body=b""), response([RECORD])]
        with self.assertLogs("main", level="ERROR"):
            status, body = asyncio.run(asgi_request("/reconcile", self.payload))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            unchecked = workbook.read("xl/worksheets/sheet6.xml").decode("utf-8")
        self.assertIn('name="Базы не проверены"', workbook_xml)
        self.assertIn("BUH2019", unchecked)
        self.assertNotIn("BUH2026", unchecked)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_snapshot_only_tax_export_is_valid_zero_document_export(self, get, settings):
        settings.return_value = replace(self.settings, tax_bases=[self.settings.tax_bases[1]])
        snapshot = RECORD | {
            "document": "",
            "turnover_quantity": "0",
            "turnover_amount": "0",
            "closing_quantity": RECORD["opening_quantity"],
            "closing_amount": RECORD["opening_amount"],
        }
        get.side_effect = [response([RECORD]), response([snapshot])]
        status, body = asyncio.run(asgi_request("/reconcile", self.payload))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            add_sheet = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
        self.assertNotIn('name="Базы не проверены"', workbook_xml)
        self.assertEqual(add_sheet.count("<row "), 2)
        self.assertIn("BUH2026", add_sheet)
        self.assertIn("Реализация товаров и услуг УТ1", add_sheet)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_movement_without_document_is_reported_as_unchecked_base(self, get, settings):
        settings.return_value = replace(self.settings, tax_bases=[self.settings.tax_bases[1]])
        get.side_effect = [response([RECORD]), response([RECORD | {"document": ""}])]
        with self.assertLogs("main", level="ERROR"):
            status, body = asyncio.run(asgi_request("/reconcile", self.payload))
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            unchecked = workbook.read("xl/worksheets/sheet6.xml").decode("utf-8")
        self.assertIn('name="Базы не проверены"', workbook_xml)
        self.assertIn("BUH2026", unchecked)
        self.assertIn("ненулевым оборотом", unchecked)
        self.assertIn("пустым полем «Документ»", unchecked)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_trade_movement_without_document_rejects_reconciliation(self, get, settings):
        settings.return_value = self.settings
        get.return_value = response([RECORD | {"document": ""}])
        with self.assertLogs("main", level="ERROR"):
            status, body = asyncio.run(asgi_request("/reconcile", self.payload))
        self.assertEqual(status, 502)
        self.assertIn("ненулевым оборотом", body.decode("utf-8"))
        self.assertEqual(get.call_count, 1)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_mixed_warehouse_capability_rejected_before_loading(self, get, settings):
        settings.return_value = replace(self.settings, trade_base=replace(BASE, supports_warehouse_filter=True))
        status, _ = asyncio.run(asgi_request("/reconcile", self.payload | {"warehouses": ["main"]}))
        self.assertEqual(status, 422)
        get.assert_not_called()

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_warehouse_endpoint_returns_all_bases_and_mapping(self, get, settings):
        settings.return_value = replace(
            self.settings,
            trade_base=replace(BASE, warehouses_url="https://internal.example.invalid/endpoint/trade/warehouses"),
            tax_bases=[replace(base, warehouses_url=f"https://internal.example.invalid/endpoint/{base.base_id}/warehouses")
                       for base in self.settings.tax_bases],
        )
        get.side_effect = [
            response({"code": 200, "data": [{"Склад": "Trade warehouse", "Код": "T1", "Группа": "G"}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B1", "Группа": ""}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B1", "Группа": ""}]}),
        ]
        status, body = asyncio.run(asgi_request("/warehouses", method="GET"))
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([base["id"] for base in payload["bases"]], ["trade", "buh2019", "buh2026"])
        self.assertEqual(payload["warehouses"][0]["sources"]["trade"][0]["code"], "T1")
        self.assertEqual(payload["warehouses"][0]["sources"]["buh2026"][0]["name"], "Tax warehouse")

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_warehouse_endpoint_builds_automatic_many_to_one_mapping(self, get, settings):
        settings.return_value = replace(
            self.settings,
            trade_base=replace(BASE, warehouses_url="https://internal.example.invalid/endpoint/trade/warehouses"),
            tax_bases=[replace(base, warehouses_url=f"https://internal.example.invalid/endpoint/{base.base_id}/warehouses")
                       for base in self.settings.tax_bases],
            warehouse_mappings=[],
        )
        get.side_effect = [
            response({"code": 200, "data": [
                {"Склад": "Section 1", "Код": "T1", "Группа": "G", "СкладБухгалтерия": "Tax warehouse"},
                {"Склад": "Section 2", "Код": "T2", "Группа": "G", "СкладБухгалтерия": "Tax warehouse"},
                {"Склад": "No link", "Код": "T3", "Группа": ""},
                {"Склад": "Bad link", "Код": "T4", "Группа": "", "СкладБухгалтерия": "Missing"},
            ]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B19", "Группа": ""}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B26", "Группа": ""}]}),
        ]

        status, body = asyncio.run(asgi_request("/warehouses", method="GET"))
        payload = json.loads(body)

        self.assertEqual(status, 200)
        mapping = payload["warehouses"][0]
        self.assertEqual(mapping["id"], "auto:buh2019:B19")
        self.assertEqual([item["code"] for item in mapping["sources"]["trade"]], ["T1", "T2"])
        self.assertEqual(payload["bases"][0]["warehouses"][0]["accounting_name"], "Tax warehouse")
        self.assertEqual(payload["mapping_diagnostics"]["mapped_trade_count"], 2)
        self.assertEqual(payload["mapping_diagnostics"]["blank_trade_count"], 1)
        self.assertEqual(payload["mapping_diagnostics"]["unresolved_trade_count"], 1)

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_automatic_mapping_uses_one_full_trade_export(self, get, settings):
        settings.return_value = replace(
            self.settings,
            trade_base=replace(
                BASE,
                warehouses_url="https://internal.example.invalid/endpoint/trade/warehouses",
                supports_warehouse_filter=True,
            ),
            tax_bases=[replace(
                base,
                warehouses_url=f"https://internal.example.invalid/endpoint/{base.base_id}/warehouses",
                supports_warehouse_filter=True,
            ) for base in self.settings.tax_bases],
            warehouse_mappings=[],
        )
        get.side_effect = [
            response({"code": 200, "data": [
                {"Склад": "Section 1", "Код": "T1", "Группа": "", "СкладБухгалтерия": "Tax warehouse"},
                {"Склад": "Section 2", "Код": "T2", "Группа": "", "СкладБухгалтерия": "Tax warehouse"},
            ]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B19", "Группа": ""}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B26", "Группа": ""}]}),
            response({"code": 200, "data": [
                RECORD | {"warehouse_code": "T1", "warehouse": "Section 1"},
                RECORD | {"warehouse_code": "T2", "warehouse": "Section 2"},
                RECORD | {"warehouse_code": "T3", "warehouse": "Other"},
            ]}),
            response({"code": 200, "data": [RECORD | {"warehouse_code": "B19"}]}),
            response({"code": 200, "data": [RECORD | {"warehouse_code": "B26"}]}),
        ]

        status, _ = asyncio.run(asgi_request(
            "/reconcile",
            self.payload | {"warehouses": ["auto:buh2019:B19"]},
        ))

        self.assertEqual(status, 200)
        self.assertNotIn("warehouse", get.call_args_list[3].kwargs["json"])
        self.assertEqual(get.call_args_list[4].kwargs["json"]["warehouse"], "B19")
        self.assertEqual(get.call_args_list[5].kwargs["json"]["warehouse"], "B26")

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_filtered_reconciliation_reports_selected_warehouse(self, get, settings):
        settings.return_value = replace(
            self.settings,
            trade_base=replace(BASE, warehouses_url="https://internal.example.invalid/endpoint/trade/warehouses", supports_warehouse_filter=True),
            tax_bases=[replace(base, warehouses_url=f"https://internal.example.invalid/endpoint/{base.base_id}/warehouses",
                               supports_warehouse_filter=True) for base in self.settings.tax_bases],
        )
        directories = [
            response({"code": 200, "data": [{"Склад": "Trade warehouse", "Код": "T1", "Группа": ""}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B1", "Группа": ""}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B1", "Группа": ""}]}),
        ]
        exports = [
            response({"code": 200, "data": [RECORD | {
                "warehouse_code": "T1", "warehouse": "Trade warehouse"
            }]}),
            response({"code": 200, "data": [RECORD | {"warehouse_code": "B1"}]}),
            response({"code": 200, "data": []}),
        ]
        get.side_effect = directories + exports
        status, body = asyncio.run(asgi_request("/reconcile", self.payload | {"warehouses": ["main"]}))
        self.assertEqual(status, 200)
        self.assertEqual([call.kwargs["json"]["warehouse"] for call in get.call_args_list[3:]], ["T1", "B1", "B1"])
        with zipfile.ZipFile(io.BytesIO(body)) as workbook:
            ns = {"s": "http://internal.internal.example.invalid/spreadsheetml/2006/main"}
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet3.xml"))
            cells = {cell.attrib["r"]: cell for cell in sheet.findall(".//s:c", ns)}
            self.assertEqual(cells["B2"].find("s:is/s:t", ns).text, "BUH2026")
            self.assertIn("Реализация товаров и услуг УТ1", cells["G2"].find("s:is/s:t", ns).text)
            self.assertEqual(cells["I2"].find("s:is/s:t", ns).text, "Trade warehouse")

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_unknown_mapping_is_rejected_before_network(self, get, settings):
        settings.return_value = self.settings
        status, _ = asyncio.run(asgi_request("/reconcile", self.payload | {"warehouses": ["unknown"]}))
        self.assertEqual(status, 422)
        get.assert_not_called()

    @patch("main.load_settings")
    @patch("app.onec_client.requests.get")
    def test_unknown_dynamic_mapping_is_rejected_after_directory_refresh(self, get, settings):
        settings.return_value = replace(
            self.settings,
            trade_base=replace(
                BASE,
                warehouses_url="https://internal.example.invalid/endpoint/trade/warehouses",
                supports_warehouse_filter=True,
            ),
            tax_bases=[replace(
                base,
                warehouses_url=f"https://internal.example.invalid/endpoint/{base.base_id}/warehouses",
                supports_warehouse_filter=True,
            ) for base in self.settings.tax_bases],
            warehouse_mappings=[],
        )
        get.side_effect = [
            response({"code": 200, "data": [{
                "Склад": "Trade", "Код": "T1", "СкладБухгалтерия": "Tax warehouse"
            }]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B19"}]}),
            response({"code": 200, "data": [{"Склад": "Tax warehouse", "Код": "B26"}]}),
        ]

        status, body = asyncio.run(asgi_request(
            "/reconcile",
            self.payload | {"warehouses": ["unknown"]},
        ))

        self.assertEqual(status, 422)
        self.assertIn("Обновите список складов", body.decode("utf-8"))
        self.assertEqual(get.call_count, 3)

    def test_bad_date_returns_422(self):
        status, _ = asyncio.run(asgi_request("/reconcile", self.payload | {"start_date": "invalid"}))
        self.assertEqual(status, 422)


class ConfigurationContractTests(TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_env_example_contains_both_accounting_bases(self):
        settings = load_settings(".env.example")
        self.assertEqual([base.base_id for base in settings.tax_bases], ["buh2019", "buh2026"])
        self.assertEqual(settings.warehouse_mappings, [])

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_explicit_credential_references(self, load):
        load.return_value = {"ONEC_TRADE_URL": BASE.url, "USER_REF": "user", "PASS_REF": "REDACTED",
                             "ONEC_TAX_BASES_JSON": json.dumps([{"url": BASE.url,
                                 "username_env": "USER_REF", "REDACTED_env": "PASS_REF"}])}
        base = load_settings().tax_bases[0]
        self.assertEqual((base.username, base.REDACTED), ("user", "REDACTED"))
        del load.return_value["PASS_REF"]
        with self.assertRaises(ConfigurationError):
            load_settings()

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_loads_live_transport_and_explicit_capabilities(self, load):
        load.return_value = {
            "ONEC_TRADE_URL": BASE.url, "ONEC_TRADE_REQUEST_PAYLOAD": "json",
            "ONEC_TRADE_SUPPORTS_WAREHOUSE_FILTER": "false",
            "ONEC_TAX_BASES_JSON": json.dumps([
                {"name": "Tax", "url": BASE.url, "request_payload": "json", "supports_warehouse_filter": False}]),
        }
        settings = load_settings()
        self.assertEqual(settings.timeout_seconds, 120.0)
        self.assertEqual(settings.trade_base.request_payload, "json")
        self.assertEqual(settings.tax_bases[0].request_payload, "json")
        self.assertFalse(settings.trade_base.supports_warehouse_filter)
        self.assertFalse(settings.tax_bases[0].supports_warehouse_filter)

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_duplicate_base_names_do_not_silently_overwrite_an_export(self, load):
        load.return_value = {"ONEC_TRADE_URL": BASE.url, "ONEC_TAX_BASES_JSON": json.dumps([
            {"name": "Tax", "url": BASE.url}, {"name": "Tax", "url": BASE.url + "2"}])}
        with self.assertRaises(ConfigurationError):
            load_settings()

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_loads_and_validates_warehouse_mappings(self, load):
        load.return_value = {
            "ONEC_TRADE_URL": BASE.url,
            "ONEC_TAX_BASES_JSON": json.dumps([{"base_id": "buh", "name": "Tax", "url": BASE.url}]),
            "ONEC_WAREHOUSE_MAPPINGS_JSON": json.dumps([
                {"id": "main", "label": "Main", "codes": {"trade": "T1", "buh": "B1"}}
            ]),
        }
        settings = load_settings()
        self.assertEqual(settings.warehouse_mappings[0].codes_for("trade"), ("T1",))
        self.assertEqual(settings.warehouse_mappings[0].codes_for("buh"), ("B1",))

        load.return_value["ONEC_WAREHOUSE_MAPPINGS_JSON"] = json.dumps([
            {"id": "main", "codes": {"trade": ["T1", "T2"], "buh": "B1"}}
        ])
        settings = load_settings()
        self.assertEqual(settings.warehouse_mappings[0].codes_for("trade"), ("T1", "T2"))

        load.return_value["ONEC_WAREHOUSE_MAPPINGS_JSON"] = json.dumps([
            {"id": "main", "codes": {"trade": "T1"}}
        ])
        with self.assertRaises(ConfigurationError):
            load_settings()

        load.return_value["ONEC_WAREHOUSE_MAPPINGS_JSON"] = json.dumps([
            {"id": "main", "codes": {"trade": [], "buh": "B1"}}
        ])
        with self.assertRaises(ConfigurationError):
            load_settings()


class TransientFailureRetryTests(TestCase):
    """Транзитные сбои 1С не должны отменять всю сверку (H-02)."""

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_single_5xx_is_retried_and_succeeds(self, get, sleep):
        get.side_effect = [response([], status=502), response({"code": 200, "data": [RECORD]})]
        rows = OneCClient().fetch_rows(BASE, BEGIN, END)
        self.assertEqual(len(rows), 1)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(sleep.called)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_single_timeout_is_retried_and_succeeds(self, get, sleep):
        get.side_effect = [requests.Timeout("test timeout"), response({"code": 200, "data": [RECORD]})]
        self.assertEqual(len(OneCClient().fetch_rows(BASE, BEGIN, END)), 1)
        self.assertEqual(get.call_count, 2)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_warehouse_directory_is_retried_too(self, get, sleep):
        base = replace(BASE, warehouses_url="https://internal.example.invalid/endpoint/warehouses")
        get.side_effect = [
            requests.ConnectionError("reset by peer"),
            response({"code": 200, "data": [{"Код": "W1", "Склад": "Основной"}]}),
        ]
        self.assertEqual(len(OneCClient().fetch_warehouses(base)), 1)
        self.assertEqual(get.call_count, 2)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_persistent_5xx_gives_up_after_max_attempts(self, get, sleep):
        get.return_value = response([], status=503)
        with self.assertRaises(OneCClientError):
            OneCClient(max_attempts=3).fetch_rows(BASE, BEGIN, END)
        self.assertEqual(get.call_count, 3)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_client_errors_are_not_retried(self, get, sleep):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                get.reset_mock()
                get.return_value = response([], status=status)
                with self.assertRaises(OneCClientError):
                    OneCClient().fetch_rows(BASE, BEGIN, END)
                self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    @patch("app.onec_client.requests.get")
    def test_retries_can_be_disabled(self, get):
        get.return_value = response([], status=500)
        with self.assertRaises(OneCClientError):
            OneCClient(max_attempts=1).fetch_rows(BASE, BEGIN, END)
        self.assertEqual(get.call_count, 1)


class BlockingHandlerOffloadTests(TestCase):
    """Блокирующие маршруты обязаны уходить в пул потоков (H-01).

    Клиент 1С синхронный, а одна сверка держит соединение минутами. Объявленные
    через `async def` маршруты выполнялись бы прямо в цикле событий и блокировали
    весь процесс, из-за чего `/health` отвечал 15 с вместо 0.02 с.
    """

    def test_endpoints_touching_onec_are_not_coroutines(self):
        import inspect

        import main

        for handler in (main.index, main.warehouses, main.run_reconciliation):
            with self.subTest(handler=handler.__name__):
                self.assertFalse(inspect.iscoroutinefunction(handler))

    def test_health_stays_on_the_event_loop(self):
        import inspect

        import main

        self.assertTrue(inspect.iscoroutinefunction(main.health))


class RetryCoverageGapTests(TestCase):
    """Пробелы, оставшиеся после первого исправления H-02 (N-01, N-05…N-07)."""

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_chunked_encoding_error_is_retried(self, get, sleep):
        # ChunkedEncodingError не наследуется от ConnectionError, поэтому обрыв
        # внутри тела ответа раньше не повторялся.
        self.assertFalse(issubclass(requests.exceptions.ChunkedEncodingError, requests.ConnectionError))
        get.side_effect = [
            requests.exceptions.ChunkedEncodingError("connection broken"),
            response({"code": 200, "data": [RECORD]}),
        ]
        self.assertEqual(len(OneCClient().fetch_rows(BASE, BEGIN, END)), 1)
        self.assertEqual(get.call_count, 2)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_throttling_and_request_timeout_statuses_are_retried(self, get, sleep):
        for status in (408, 429):
            with self.subTest(status=status):
                get.reset_mock()
                get.side_effect = [response([], status=status), response({"code": 200, "data": [RECORD]})]
                self.assertEqual(len(OneCClient().fetch_rows(BASE, BEGIN, END)), 1)
                self.assertEqual(get.call_count, 2)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.post")
    def test_post_branch_is_retried_too(self, post, sleep):
        post.side_effect = [response([], status=503), response({"code": 200, "data": [RECORD]})]
        base = replace(BASE, request_method="POST")
        self.assertEqual(len(OneCClient().fetch_rows(base, BEGIN, END)), 1)
        self.assertEqual(post.call_count, 2)

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.post")
    def test_post_branch_does_not_retry_client_errors(self, post, sleep):
        post.return_value = response([], status=403)
        with self.assertRaises(OneCClientError):
            OneCClient().fetch_rows(replace(BASE, request_method="POST"), BEGIN, END)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    @patch("app.onec_client.time.sleep")
    @patch("app.onec_client.requests.get")
    def test_backoff_is_bounded(self, get, sleep):
        get.return_value = response([], status=500)
        with self.assertRaises(OneCClientError):
            OneCClient(max_attempts=6, backoff_seconds=10.0).fetch_rows(BASE, BEGIN, END)
        from app.onec_client import MAX_BACKOFF_SECONDS

        for call in sleep.call_args_list:
            self.assertLessEqual(call.args[0], MAX_BACKOFF_SECONDS)


class RetryPolicyConfigurationTests(TestCase):
    """Политика повторов и лимит параллелизма настраиваются из .env (N-02, N-04)."""

    ENV = {
        "ONEC_TRADE_URL": "https://internal.example.invalid/endpoint/export",
        "ONEC_TAX_BASES_JSON": json.dumps([
            {"name": "Tax", "url": "https://internal.example.invalid/endpoint/export", "request_payload": "json",
             "supports_warehouse_filter": False}
        ]),
    }

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_policy_is_read_from_env(self, load):
        load.return_value = self.ENV | {
            "ONEC_RETRY_MAX_ATTEMPTS": "5",
            "ONEC_RETRY_BACKOFF_SECONDS": "1.5",
            "ONEC_MAX_CONCURRENT_RECONCILIATIONS": "4",
        }
        settings = load_settings()
        self.assertEqual(settings.retry_max_attempts, 5)
        self.assertEqual(settings.retry_backoff_seconds, 1.5)
        self.assertEqual(settings.max_concurrent_reconciliations, 4)

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_policy_defaults_when_absent(self, load):
        load.return_value = dict(self.ENV)
        settings = load_settings()
        self.assertEqual(settings.retry_max_attempts, 3)
        self.assertEqual(settings.retry_backoff_seconds, 0.2)
        self.assertEqual(settings.max_concurrent_reconciliations, 2)

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.config._load_env_file")
    def test_invalid_policy_is_rejected_with_a_clear_message(self, load):
        for key, value in (
            ("ONEC_RETRY_MAX_ATTEMPTS", "0"),
            ("ONEC_RETRY_MAX_ATTEMPTS", "не число"),
            ("ONEC_RETRY_BACKOFF_SECONDS", "-1"),
            ("ONEC_MAX_CONCURRENT_RECONCILIATIONS", "0"),
        ):
            with self.subTest(key=key, value=value):
                load.return_value = self.ENV | {key: value}
                with self.assertRaisesRegex(ConfigurationError, key):
                    load_settings()


class ReconciliationConcurrencyLimitTests(TestCase):
    """Одновременных сверок не больше настроенного числа (N-04)."""

    def setUp(self):
        import main

        main._reconciliation_slots = None
        main._reconciliation_slots_size = 0

    @staticmethod
    def _request():
        import main

        return main.ReconcileRequest(start_date=BEGIN, end_date=END, warehouses=[])

    def test_slots_are_bounded_and_released(self):
        import main

        first = main._acquire_reconciliation_slot(2)
        second = main._acquire_reconciliation_slot(2)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(main._acquire_reconciliation_slot(2))
        main._release_reconciliation_slot(first)
        third = main._acquire_reconciliation_slot(2)
        self.assertIsNotNone(third)
        main._release_reconciliation_slot(second)
        main._release_reconciliation_slot(third)

    def test_changing_the_limit_keeps_running_reconciliations_bounded(self):
        """Слот возвращается тому семафору, у которого был взят.

        Настройки читаются на каждый запрос, поэтому правка лимита в `.env`
        пересоздаёт семафор прямо во время работающих сверок. Если бы слот
        освобождался у текущего объекта, новая граница превышалась бы.
        """
        import main

        running = main._acquire_reconciliation_slot(1)
        self.assertIsNotNone(running)
        self.assertIsNone(main._acquire_reconciliation_slot(1))

        after_change = main._acquire_reconciliation_slot(2)
        self.assertIsNotNone(after_change)
        second_after_change = main._acquire_reconciliation_slot(2)
        self.assertIsNotNone(second_after_change)
        self.assertIsNone(main._acquire_reconciliation_slot(2))

        main._release_reconciliation_slot(running)
        self.assertIsNone(main._acquire_reconciliation_slot(2))

        main._release_reconciliation_slot(after_change)
        main._release_reconciliation_slot(second_after_change)

    def test_excess_reconciliation_is_rejected_with_503(self):
        import main

        settings = Settings(trade_base=BASE, tax_bases=[], max_concurrent_reconciliations=1)
        with patch("main.load_settings", return_value=settings), \
             patch("main._reconcile", return_value=object()):
            slot = main._acquire_reconciliation_slot(1)
            self.assertIsNotNone(slot)
            try:
                with self.assertRaises(main.HTTPException) as caught:
                    main._run_reconciliation(self._request())
            finally:
                main._release_reconciliation_slot(slot)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.headers.get("Retry-After"), "60")

    def test_slot_is_released_when_reconciliation_fails(self):
        import main

        settings = Settings(trade_base=BASE, tax_bases=[], max_concurrent_reconciliations=1)
        with patch("main.load_settings", return_value=settings), \
             patch("main._reconcile", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                main._run_reconciliation(self._request())
        slot = main._acquire_reconciliation_slot(1)
        self.assertIsNotNone(slot)
        main._release_reconciliation_slot(slot)
