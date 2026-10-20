import base64
import io
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from app.config import OneCBaseConfig, WarehouseMappingConfig
from app.document_matching import is_reconcilable_document, parse_document
from app.models import LedgerRow, ReconciliationResult, Warehouse
from app.normalization import normalize_row
from app.onec_client import OneCClient, normalize_warehouses
from app.reconciliation import (
    AMBIGUOUS_MATCH,
    BUH_ONLY_DOCUMENT,
    DOCUMENT_QUANTITY_MISMATCH,
    DUPLICATE_IN_TAX,
    EXTRA_BALANCE_IN_TAX,
    EXTRA_IN_TAX,
    MISSING_BALANCE_IN_TAX,
    MISSING_IN_TAX,
    DOCUMENT_CONTENT_MATCH,
    reconcile,
)
from app.reporting import build_action_excel_report, build_excel_report
from scripts.verify_onec import workbook_rows
from main import ReconcileRequest, _fetch_rows_for_base, _format_period


def row(
    nomenclature: str,
    document: str = "",
    period: str = "01.08.2026 0:00:00",
    opening_quantity: str = "10.00",
    opening_amount: str = "100.00",
    turnover_quantity: str = "0.00",
    turnover_amount: str = "0.00",
    closing_quantity: str = "10.00",
    closing_amount: str = "100.00",
) -> LedgerRow:
    return LedgerRow(
        period=period,
        document=document,
        nomenclature=nomenclature,
        opening_quantity=Decimal(opening_quantity),
        opening_amount=Decimal(opening_amount),
        turnover_quantity=Decimal(turnover_quantity),
        turnover_amount=Decimal(turnover_amount),
        closing_quantity=Decimal(closing_quantity),
        closing_amount=Decimal(closing_amount),
    )


class NormalizationTests(TestCase):
    def test_normalizes_stock_turnover_fields_and_amounts(self) -> None:
        normalized = normalize_row(
            {
                "Период": " 01.08.2026 0:00:00 ",
                "Документ": "",
                "Номенклатура": "  Товар  1 ",
                "КоличествоНачальныйОстаток": "1 000,005",
                "СтоимостьНачальныйОстаток": "2 000,004",
                "КоличествоОборот": "-10",
                "СтоимостьОборот": "-20,1",
                "КоличествоКонечныйОстаток": "990,005",
                "СтоимостьКонечныйОстаток": "1 979,904",
            },
            default_period="I 2024",
        )

        self.assertEqual(normalized.document, "")
        self.assertEqual(normalized.nomenclature, "Товар 1")
        self.assertEqual(normalized.opening_quantity, Decimal("1000.005"))
        self.assertEqual(normalized.opening_amount, Decimal("2000.004"))
        self.assertEqual(normalized.closing_amount, Decimal("1979.904"))

    def test_parses_document_type_number_and_timestamp(self) -> None:
        identity = parse_document(
            "Передача товаров филиалу 4929211078 от 02.03.2026 8:48:28"
        )

        self.assertEqual(identity.family, "transfer")
        self.assertEqual(identity.number, "4929211078")
        self.assertEqual(identity.timestamp, "2026-03-02T08:48:28")

    def test_document_allowlist_normalizes_confirmed_aliases(self) -> None:
        aliases = {
            "Передача товаров филиалу (ПТФ)": "transfer",
            "Перемещение": "transfer",
            "Поступление товаров и услуг": "goods_receipt",
            "Поступление (акты, накладные, УПД)": "goods_receipt",
            "Оприходование": "receipt",
            "Списание товаров": "write_off",
            "Реализация (акт, накладная, УПД)": "sale",
        }
        for document_type, family in aliases.items():
            with self.subTest(document_type=document_type):
                document = f"{document_type} 1 от 01.03.2026 10:00:00"
                self.assertEqual(parse_document(document).family, family)
                self.assertTrue(is_reconcilable_document(document))

    def test_pending_and_non_movement_documents_are_not_reconciled(self) -> None:
        for document_type in (
            "Комплектация номенклатуры",
            "Требование-накладная",
            "Безвозмездная передача",
            "Поступление дополнительных расходов",
            "Заказ покупателя",
            "Неизвестный документ",
        ):
            with self.subTest(document_type=document_type):
                document = f"{document_type} 1 от 01.03.2026 10:00:00"
                self.assertFalse(is_reconcilable_document(document))

    def test_false_accounting_flags_do_not_exclude_confirmed_document(self) -> None:
        normalized = normalize_row(
            {
                "Период": "01.03.2026 10:00:00",
                "Документ": "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
                "Номенклатура": "Товар 1",
                "КоличествоНачальныйОстаток": "0",
                "СтоимостьНачальныйОстаток": "0",
                "КоличествоОборот": "-1",
                "СтоимостьОборот": "-100",
                "КоличествоКонечныйОстаток": "-1",
                "СтоимостьКонечныйОстаток": "-100",
                "УправленческийУчет": False,
                "БухгалтерскийУчет": False,
                "НалоговыйУчет": False,
            },
            default_period="I 2024",
        )

        result = reconcile([normalized], {"БУХ": []}, "I 2024")

        self.assertEqual(result.discrepancies[0].discrepancy_type, MISSING_IN_TAX)

    def test_normalizes_movement_endpoints_and_document_ids(self) -> None:
        normalized = normalize_row(
            {
                "period": "01.08.2026 0:00:00",
                "document": "Документ 1 от 01.08.2026 0:00:00",
                "nomenclature": "Товар",
                "opening_quantity": "0",
                "opening_amount": "0",
                "turnover_quantity": "1",
                "turnover_amount": "10",
                "closing_quantity": "1",
                "closing_amount": "10",
                "СкладОтправитель": "Секция 1",
                "СкладПолучатель": "Секция 2",
                "ДокументGUID": "source-id",
                "ДокументОснованиеGUID": "trade-id",
            },
            default_period="period",
        )

        self.assertEqual(normalized.sender_warehouse, "Секция 1")
        self.assertEqual(normalized.receiver_warehouse, "Секция 2")
        self.assertEqual(normalized.document_id, "source-id")
        self.assertEqual(normalized.source_document_id, "trade-id")


class RequestValidationTests(TestCase):
    def test_accepts_valid_date_range(self) -> None:
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))

        self.assertEqual(payload.start_date, date(2024, 1, 1))
        self.assertEqual(payload.end_date, date(2024, 1, 31))

    def test_normalizes_warehouses(self) -> None:
        payload = ReconcileRequest(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            warehouses=[" Основной  склад ", "", "Склад 2", "Основной склад"],
        )

        self.assertEqual(payload.warehouses, ["Основной склад", "Склад 2"])

    def test_rejects_end_date_before_start_date(self) -> None:
        with self.assertRaises(ValueError):
            ReconcileRequest(start_date=date(2024, 2, 1), end_date=date(2024, 1, 31))

    def test_formats_period_as_inclusive_date_range(self) -> None:
        self.assertEqual(
            _format_period(date(2024, 1, 1), date(2024, 1, 31)),
            "01.01.2024–31.01.2024",
        )

    def test_multiple_mappings_use_one_full_export(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
            replace(row("Товар 3"), warehouse_code="W3"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mappings = [
            WarehouseMappingConfig("first", "First", {"trade": "W1"}),
            WarehouseMappingConfig("second", "Second", {"trade": "W2"}),
        ]

        result = _fetch_rows_for_base(client, base, payload, mappings)

        self.assertEqual(len(result), 2)
        self.assertEqual(
            [(item.warehouse_code, item.warehouse_mapping_id) for item in result],
            [("W1", "first"), ("W2", "second")],
        )
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)

    def test_one_mapping_can_load_multiple_physical_warehouses(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mapping = WarehouseMappingConfig("main", "Main", {"trade": ["W1", "W2"]})

        result = _fetch_rows_for_base(client, base, payload, [mapping])

        self.assertEqual(len(result), 2)
        self.assertEqual({item.warehouse_mapping_id for item in result}, {"main"})
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)

    def test_logical_mapping_fetches_all_physical_warehouses(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mapping = WarehouseMappingConfig("main", "Main", {"trade": ["W1", "W2"]})

        result = _fetch_rows_for_base(client, base, payload, [mapping])

        self.assertEqual(len(result), 2)
        self.assertEqual([item.warehouse_code for item in result], ["W1", "W2"])
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)


class OneCClientTests(TestCase):
    @patch("app.onec_client.requests.get")
    def test_sends_start_and_end_date_params(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_get.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(name="Налоговая", url="https://internal.example.invalid/endpoint/export"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
        )

        mocked_get.assert_called_once()
        self.assertEqual(
            mocked_get.call_args.kwargs["params"],
            [("start_date", "2024-01-01"), ("end_date", "2024-01-31")],
        )

    @patch("app.onec_client.requests.get")
    def test_sends_single_warehouse_param(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_get.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(name="Налоговая", url="https://internal.example.invalid/endpoint/export", supports_warehouse_filter=True),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            warehouse=" Основной  склад ",
        )

        self.assertEqual(
            mocked_get.call_args.kwargs["params"],
            [
                ("start_date", "2024-01-01"),
                ("end_date", "2024-01-31"),
                ("warehouse", "Основной склад"),
            ],
        )

    @patch("app.onec_client.requests.post")
    def test_sends_postman_json_period_payload(self, mocked_post: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_post.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(
                name="Trade",
                url="https://internal.example.invalid/endpoint/stocksturnover",
                username="user",
                REDACTED="REDACTED",
                request_method="POST",
                date_format="%Y%m%d",
                period_begin_key="periodBegin",
                period_end_key="periodEnd",
            ),
            start_date=date(2026, 1, 24),
            end_date=date(2026, 1, 31),
        )

        mocked_post.assert_called_once()
        self.assertEqual(
            mocked_post.call_args.kwargs["json"],
            {"periodBegin": "20260124", "periodEnd": "20260131"},
        )
        expected_token = base64.b64encode("user:REDACTED".encode("utf-8")).decode("ascii")
        self.assertEqual(mocked_post.call_args.kwargs["headers"], {"Authorization": f"Basic {expected_token}"})

    def test_rejects_warehouse_without_code(self) -> None:
        with self.assertRaises(ValueError):
            normalize_warehouses(["Основной склад"])

    def test_normalizes_warehouse_object_list(self) -> None:
        self.assertEqual(
            normalize_warehouses(
                {
                    "data": [
                        {"name": "Основной склад", "code": "W1"},
                        {"Название": "Склад 2", "id": "W2"},
                        {"Склад": "Склад 3", "Код": "W3", "Группа": "Группа 1"},
                    ]
                }
            ),
            [
                Warehouse("W1", "Основной склад"),
                Warehouse("W2", "Склад 2"),
                Warehouse("W3", "Склад 3", "Группа 1"),
            ],
        )


class ReconciliationTests(TestCase):
    def test_no_discrepancies_within_tolerance(self) -> None:
        result = reconcile(
            trade_rows=[row("Товар 1", opening_amount="100.00")],
            tax_exports={"Бухгалтерия": [row("Товар 1", opening_amount="100.01")]},
            period="I 2024",
            amount_tolerance=Decimal("0.01"),
        )

        self.assertEqual(result.discrepancies, [])

    def test_detects_missing_balance_in_tax_base(self) -> None:
        result = reconcile(
            trade_rows=[row("Товар 1")],
            tax_exports={"Бухгалтерия": []},
            period="I 2024",
        )

        self.assertEqual(result.discrepancies[0].discrepancy_type, MISSING_BALANCE_IN_TAX)

    def test_detects_extra_balance_in_tax_base(self) -> None:
        result = reconcile(
            trade_rows=[],
            tax_exports={"Бухгалтерия": [row("Товар 1")]},
            period="I 2024",
        )

        self.assertEqual(result.discrepancies[0].discrepancy_type, EXTRA_BALANCE_IN_TAX)

    def test_detects_duplicate_document_in_tax_base(self) -> None:
        result = reconcile(
            trade_rows=[row("Товар 1")],
            tax_exports={"Бухгалтерия": [row("Товар 1"), row("Товар 1")]},
            period="I 2024",
        )

        self.assertTrue(any(item.discrepancy_type == DUPLICATE_IN_TAX for item in result.discrepancies))
        self.assertFalse(any(item.discrepancy_type == MISSING_BALANCE_IN_TAX for item in result.discrepancies))

    def test_ignores_amount_only_balance_mismatch(self) -> None:
        result = reconcile(
            trade_rows=[row("Товар 1", closing_amount="100.00")],
            tax_exports={"Бухгалтерия": [row("Товар 1", closing_amount="100.02")]},
            period="I 2024",
            amount_tolerance=Decimal("0.01"),
        )

        self.assertEqual(result.discrepancies, [])

    def test_document_compares_turnover_but_not_running_balances(self) -> None:
        trade = row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
            opening_quantity="10",
            opening_amount="100",
            turnover_quantity="-2",
            turnover_amount="-20",
            closing_quantity="8",
            closing_amount="80",
        )
        tax = replace(
            trade,
            opening_quantity=Decimal("1000"),
            opening_amount=Decimal("9000"),
            closing_quantity=Decimal("998"),
            closing_amount=Decimal("8980"),
        )

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])

    def test_document_reports_quantity_and_ignores_amount(self) -> None:
        trade = row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
            turnover_quantity="-2",
            turnover_amount="-20",
        )
        tax = replace(trade, turnover_quantity=Decimal("-3"), turnover_amount=Decimal("-30"))

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(
            {item.discrepancy_type for item in result.discrepancies},
            {DOCUMENT_QUANTITY_MISMATCH},
        )

    def test_repeated_item_lines_are_aggregated_before_comparison(self) -> None:
        document = "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00"
        first = replace(row("Товар 1", document, turnover_quantity="-1", turnover_amount="-10"), nomenclature_code="N1")
        second = replace(first, turnover_quantity=Decimal("-2"), turnover_amount=Decimal("-20"))
        tax = replace(first, turnover_quantity=Decimal("-3"), turnover_amount=Decimal("-30"))

        result = reconcile([first, second], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])

    def test_marks_repeated_trade_keys_as_ambiguous(self) -> None:
        result = reconcile(
            trade_rows=[
                row("Товар 1", opening_quantity="2.00", opening_amount="20.00", closing_quantity="2.00", closing_amount="20.00"),
                row("Товар 1", opening_quantity="3.00", opening_amount="30.00", closing_quantity="3.00", closing_amount="30.00"),
            ],
            tax_exports={
                "Бухгалтерия": [
                    row("Товар 1", opening_quantity="5.00", opening_amount="50.00", closing_quantity="5.00", closing_amount="50.00")
                ]
            },
            period="I 2024",
        )

        self.assertEqual(len(result.discrepancies), 2)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_matches_different_source_warehouse_codes_by_mapping_id(self) -> None:
        trade = row("Товар 1")
        trade = LedgerRow(**{**trade.__dict__, "warehouse_code": "УПР000234", "warehouse_mapping_id": "main"})
        tax = LedgerRow(**{**trade.__dict__, "warehouse_code": "БД-000001"})

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])

    def test_matches_transfer_aliases_by_number_and_date(self) -> None:
        trade = replace(
            row("Товар 1", "Передача товаров филиалу 4929211078 от 02.03.2026 8:48:28"),
            nomenclature_code="N1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        tax = replace(
            trade,
            document="Перемещение товаров, материалов 4929211078 от 02.03.2026 8:48:28",
            warehouse_code="B1",
        )

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].matched_documents, 1)
        self.assertEqual(result.summaries[0].matched_by_number_date, 1)

    def test_matches_confirmed_document_type_matrix(self) -> None:
        pairs = (
            ("Поступление товаров и услуг", "Поступление (акты, накладные, УПД)"),
            ("Оприходование товаров", "Оприходование"),
            ("Списание товаров", "Списание товаров, материалов"),
            ("Реализация товаров и услуг", "Реализация (акт, накладная, УПД)"),
        )
        for trade_type, tax_type in pairs:
            with self.subTest(trade_type=trade_type, tax_type=tax_type):
                trade = replace(
                    row("Товар 1", f"{trade_type} 1 от 01.03.2026 10:00:00"),
                    nomenclature_code="N1",
                    warehouse_mapping_id="main",
                )
                tax = replace(
                    trade,
                    document=f"{tax_type} 1 от 01.03.2026 10:00:00",
                    warehouse_code="B1",
                )

                result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

                self.assertEqual(result.discrepancies, [])
                self.assertEqual(result.summaries[0].matched_documents, 1)

    def test_matches_sale_with_different_number_by_content(self) -> None:
        trade = replace(
            row(
                "Товар 1",
                "Реализация товаров и услуг ДЖ000039101 от 23.03.2026 0:00:00",
                turnover_quantity="-2",
            ),
            nomenclature_code="N1",
            warehouse_mapping_id="main",
        )
        tax = replace(
            trade,
            document="Реализация (акт, накладная, УПД) 515 от 23.03.2026 0:00:00",
            turnover_amount=Decimal("1"),
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertEqual(differences, [])

    def test_source_document_id_has_priority_over_number_and_date(self) -> None:
        trade = replace(
            row("Товар 1", "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
            document_id="shared-id",
        )
        tax = replace(
            trade,
            document="Перемещение товаров 99 от 02.03.2026 11:00:00",
            document_id="tax-id",
            source_document_id="shared-id",
            turnover_quantity=Decimal("1"),
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0].match_method, "Идентификатор исходного документа")

    def test_duplicate_canonical_document_in_tax_is_reported(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
        )
        first = replace(
            trade,
            document="Перемещение товаров, материалов ДЖ1 от 01.03.2026 10:00:00",
        )
        second = replace(
            trade,
            document="Передача товаров филиалу ДЖ1 от 01.03.2026 10:00:00",
        )

        differences = reconcile([trade], {"БУХ": [first, second]}, "I 2024").discrepancies

        self.assertEqual(len(differences), 3)
        self.assertEqual(sum(item.discrepancy_type == DUPLICATE_IN_TAX for item in differences), 2)
        self.assertEqual(sum(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences), 1)

    def test_same_document_number_in_different_organizations_is_ambiguous(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
        )
        first = replace(
            trade,
            document="Перемещение товаров, материалов ДЖ1 от 01.03.2026 10:00:00",
            organization_inn="111",
        )
        second = replace(
            trade,
            document="Передача товаров филиалу ДЖ1 от 01.03.2026 10:00:00",
            organization_inn="222",
        )

        result = reconcile([trade], {"БУХ": [first, second]}, "I 2024")

        self.assertEqual(len(result.discrepancies), 3)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))
        self.assertEqual(result.summaries[0].duplicate_documents, 0)

    def test_does_not_match_same_number_on_different_date(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ000001381 от 05.03.2026 14:22:23"),
            nomenclature_code="N1",
        )
        tax = replace(
            trade,
            document="Комплектация номенклатуры ДЖ000001381 от 10.03.2026 17:49:43",
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertEqual(
            {item.discrepancy_type for item in differences},
            {MISSING_IN_TAX},
        )

    def test_classifies_accounting_only_document_separately(self) -> None:
        tax = row(
            "Товар 1",
            "Списание товаров, материалов БД1 от 10.03.2026 17:49:43",
            turnover_quantity="-1",
        )

        result = reconcile([], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies[0].discrepancy_type, BUH_ONLY_DOCUMENT)
        self.assertEqual(result.summaries[0].buh_only_documents, 1)

    def test_ambiguous_sale_signature_requires_manual_check(self) -> None:
        first = replace(
            row(
                "Товар 1",
                "Реализация товаров и услуг ДЖ1 от 23.03.2026 10:00:00",
                turnover_quantity="-1",
            ),
            nomenclature_code="N1",
        )
        second = replace(first, document="Реализация товаров и услуг ДЖ2 от 23.03.2026 10:00:00")
        tax = replace(first, document="Реализация (акт, накладная, УПД) 10 от 23.03.2026 10:00:00")

        differences = reconcile([first, second], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertEqual(len(differences), 3)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences))

    def test_internal_transfer_inside_logical_warehouse_is_ignored(self) -> None:
        document = "Перемещение товаров ДЖ1 от 23.03.2026 10:00:00"
        first = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="Секция 1",
            receiver_warehouse="Секция 2",
        )
        second = replace(first, warehouse="Секция 2", warehouse_code="T2")

        self.assertEqual(reconcile([first, second], {"БУХ": []}, "I 2024").discrepancies, [])

    def test_ptf_inside_logical_warehouse_is_ignored(self) -> None:
        document = "Передача товаров филиалу (ПТФ) ДЖ1 от 23.03.2026 10:00:00"
        first = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="",
            receiver_warehouse="",
        )
        second = replace(
            first,
            warehouse="Секция 2",
            warehouse_code="T2",
            turnover_quantity=Decimal("1"),
        )

        result = reconcile([first, second], {"БУХ": []}, "I 2024")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 1)

    def test_transfer_between_logical_warehouses_is_checked(self) -> None:
        trade = replace(
            row(
                "Товар 1",
                "Перемещение товаров ДЖ2 от 23.03.2026 11:00:00",
                turnover_quantity="-1",
            ),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="Секция 1",
            receiver_warehouse="Другой логический склад",
        )

        result = reconcile([trade], {"БУХ": []}, "I 2024")

        self.assertEqual(len(result.discrepancies), 1)
        self.assertEqual(result.discrepancies[0].discrepancy_type, MISSING_IN_TAX)
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)

    def test_transfer_is_not_internal_when_different_items_only_cancel_in_total(self) -> None:
        document = "Передача товаров филиалу ДЖ3 от 23.03.2026 12:00:00"
        outgoing = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        incoming_other_item = replace(
            outgoing,
            nomenclature="Товар 2",
            nomenclature_code="N2",
            warehouse="Секция 2",
            warehouse_code="T2",
            turnover_quantity=Decimal("1"),
        )

        result = reconcile([outgoing, incoming_other_item], {"БУХ": []}, "I 2024")

        self.assertEqual(len(result.discrepancies), 2)
        self.assertTrue(all(item.discrepancy_type == MISSING_IN_TAX for item in result.discrepancies))
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)

    def test_excluded_document_types_do_not_create_actions(self) -> None:
        documents = [
            "Комплектация номенклатуры ДЖ1 от 23.03.2026 10:00:00",
            "Требование-накладная ДЖ2 от 23.03.2026 10:00:00",
            "Поступление дополнительных расходов ДЖ3 от 23.03.2026 10:00:00",
            "Заказ покупателя ДЖ4 от 23.03.2026 10:00:00",
        ]
        trade_rows = [row(f"Товар {index}", document, turnover_quantity="1") for index, document in enumerate(documents)]

        result = reconcile(trade_rows, {"БУХ": []}, "I 2024")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].trade_documents, 0)
        self.assertEqual(result.summaries[0].pending_trade_documents, 2)
        self.assertEqual(result.summaries[0].excluded_trade_documents, 2)

    def test_snapshots_from_physical_warehouses_are_aggregated(self) -> None:
        first = replace(
            row("Товар 1", opening_quantity="2", opening_amount="20", closing_quantity="2", closing_amount="20"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        second = replace(
            first,
            opening_quantity=Decimal("3"),
            opening_amount=Decimal("30"),
            closing_quantity=Decimal("3"),
            closing_amount=Decimal("30"),
            warehouse="Секция 2",
            warehouse_code="T2",
        )
        tax = replace(
            first,
            opening_quantity=Decimal("5"),
            opening_amount=Decimal("50"),
            closing_quantity=Decimal("5"),
            closing_amount=Decimal("50"),
            warehouse="Основной склад",
            warehouse_code="B1",
        )

        self.assertEqual(reconcile([first, second], {"БУХ": [tax]}, "I 2024").discrepancies, [])


class ReportingTests(TestCase):
    def test_builds_xlsx_bytes(self) -> None:
        content = build_excel_report([])

        self.assertTrue(content.startswith(b"PK"))
        self.assertIn(b"[Content_Types].xml", content)

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            styles = workbook.read("xl/styles.xml").decode("utf-8")
        self.assertIn('state="frozen"', sheet)
        self.assertIn('<autoFilter ref="A1:A1"/>', sheet)
        self.assertIn('<cols>', sheet)
        self.assertIn('fillId="2"', styles)

    def test_report_has_summary_and_side_by_side_details(self) -> None:
        trade = row("Товар 1", closing_quantity="10")
        tax = row("Товар 1", closing_quantity="11")
        discrepancy = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")
        content = build_excel_report(result)

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            summary = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            details = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        self.assertIn('name="Итоги"', workbook_xml)
        self.assertIn('name="Расхождения"', workbook_xml)
        self.assertIn('name="Документы"', workbook_xml)
        self.assertIn('name="Количество"', workbook_xml)
        self.assertIn('name="Остатки"', workbook_xml)
        self.assertIn("Всего строк с расхождениями", summary)
        self.assertIn("Статистика сопоставления документов", summary)
        self.assertIn("Документ УТ", details)
        self.assertIn("Документ БУХ", details)

    def test_action_report_groups_all_item_rows_into_one_document(self) -> None:
        document = "Перемещение товаров ДЖ000001 от 05.03.2026 14:22:02"
        first = replace(
            row("Товар 1", document),
            nomenclature_code="N1",
            warehouse="Склад УТ",
            warehouse_mapping_id="main",
        )
        second = replace(first, nomenclature="Товар 2", nomenclature_code="N2")

        content = build_action_excel_report(reconcile([first, second], {"БУХ2019": []}, "Март 2026"))

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            add_sheet = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
            ns = {"s": "http://internal.internal.example.invalid/spreadsheetml/2006/main"}
            rows = ET.fromstring(add_sheet).findall("s:sheetData/s:row", ns)
        self.assertIn('name="Довнести в БУХ"', workbook_xml)
        self.assertIn('name="Лишние в БУХ"', workbook_xml)
        self.assertIn('name="Проверить вручную"', workbook_xml)
        self.assertEqual(len(rows), 2)
        self.assertEqual(add_sheet.count(document), 1)
        self.assertIn("Довнести в БУХ2019", add_sheet)
        self.assertNotIn("Товар 1", add_sheet)
        self.assertNotIn("Товар 2", add_sheet)

    def test_action_report_separates_extra_and_accounting_only_documents(self) -> None:
        extra = row(
            "Товар 1",
            "Перемещение товаров, материалов БД-1 от 06.03.2026 10:00:00",
        )
        accounting_only = row(
            "Товар 2",
            "Списание товаров, материалов БД-2 от 07.03.2026 11:00:00",
        )

        content = build_action_excel_report(
            reconcile([], {"БУХ2019": [extra, accounting_only]}, "Март 2026")
        )

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            extra_sheet = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")
            review_sheet = workbook.read("xl/worksheets/sheet5.xml").decode("utf-8")
        self.assertIn("БД-1", extra_sheet)
        self.assertNotIn("БД-2", extra_sheet)
        self.assertIn("БД-2", review_sheet)
        self.assertIn("Проверить допустимость в БУХ2019", review_sheet)

    def test_action_report_excludes_document_value_differences_from_action_sheets(self) -> None:
        trade = row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ-1 от 08.03.2026 12:00:00",
            turnover_amount="100",
        )
        tax = replace(trade, turnover_amount=Decimal("101"))

        content = build_action_excel_report(reconcile([trade], {"БУХ2019": [tax]}, "Март 2026"))

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            action_sheets = [
                workbook.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
                for index in range(3, 6)
            ]
        self.assertTrue(all(sheet.count("<row ") == 1 for sheet in action_sheets))
        self.assertTrue(all("Различие стоимости" not in sheet for sheet in action_sheets))

    def test_action_report_includes_balance_differences_with_dates_values_and_missing_reasons(self) -> None:
        trade_start = replace(
            row("Товар только УТ", period="01.03.2026 0:00:00", opening_quantity="7", opening_amount="70"),
            nomenclature_code="N-TRADE",
            article="ART-TRADE",
            warehouse="Склад УТ",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        tax_end = replace(
            row("Товар только БУХ", period="11.03.2026 23:59:59", closing_quantity="4", closing_amount="40"),
            nomenclature_code="N-TAX",
            article="ART-TAX",
            warehouse="Склад БУХ",
            warehouse_code="B1",
            warehouse_mapping_id="main",
        )
        checked = reconcile([trade_start], {"БУХ2019": [tax_end]}, "01.03.2026–11.03.2026")
        result = ReconciliationResult(
            discrepancies=checked.discrepancies,
            summaries=checked.summaries,
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 11),
        )

        content = build_action_excel_report(result)
        sheets = workbook_rows(content)
        balances = sheets["Расхождения остатков"]

        self.assertEqual(
            list(sheets)[:5],
            ["Итоги", "Расхождения остатков", "Довнести в БУХ", "Лишние в БУХ", "Проверить вручную"],
        )
        self.assertEqual(len(balances), 3)
        self.assertEqual(
            balances[0][:6],
            ["Дата расхождения", "Артикул", "Наименование", "Остаток в УТ", "Остаток в НБ", "Расхождение"],
        )
        by_reason = {excel_row[8]: excel_row for excel_row in balances[1:]}
        missing_tax = by_reason["Нет остатка в БУХ"]
        self.assertEqual(missing_tax[:9], [
            "01.03.2026", "ART-TRADE", "Товар только УТ", "7", "0", "-7",
            "БУХ2019", "Начальный остаток", "Нет остатка в БУХ",
        ])
        self.assertEqual(missing_tax[10:13], ["N-TRADE", "Склад УТ", ""])
        missing_trade = by_reason["Нет остатка в УТ"]
        self.assertEqual(missing_trade[:9], [
            "11.03.2026", "ART-TAX", "Товар только БУХ", "0", "4", "4",
            "БУХ2019", "Конечный остаток", "Нет остатка в УТ",
        ])
        self.assertEqual(missing_trade[10:13], ["N-TAX", "", "Склад БУХ"])

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            balance_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn('state="frozen"', balance_xml)
        self.assertIn('<autoFilter ref="A1:M3"/>', balance_xml)
        self.assertIn('<col min="12" max="13" width="42"', balance_xml)
