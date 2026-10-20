from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from app.models import LedgerRow
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report, build_excel_report
from scripts.probe_onec import document_stats
from scripts.verify_onec import verify_action_excel, verify_diagnostic_excel


def row(nomenclature, document):
    return LedgerRow(
        period="01.03.2026 10:00:00",
        document=document,
        nomenclature=nomenclature,
        nomenclature_code=nomenclature,
        warehouse="Склад",
        warehouse_code="W1",
        warehouse_mapping_id="main",
        opening_quantity=Decimal("0"),
        opening_amount=Decimal("0"),
        turnover_quantity=Decimal("1"),
        turnover_amount=Decimal("10"),
        closing_quantity=Decimal("1"),
        closing_amount=Decimal("10"),
    )


class VerificationToolTests(TestCase):
    def test_document_stats_distinguish_balances_from_invalid_movements(self):
        movement = row("N1", "")
        balance = replace(
            movement,
            nomenclature="N2",
            nomenclature_code="N2",
            turnover_quantity=Decimal("0"),
            turnover_amount=Decimal("0"),
        )

        documented = replace(movement, document="Реализация 1 от 01.03.2026 10:00:00")
        stats = document_stats([movement, balance, documented])

        self.assertEqual(stats["empty_document_rows"], 2)
        self.assertEqual(stats["movements_without_document"], 1)
        self.assertEqual(stats["unique_document_representations"], 1)

    def test_diagnostic_and_action_reports_are_verified_separately(self):
        document = "Перемещение товаров ДЖ-1 от 01.03.2026 10:00:00"
        first = row("N1", document)
        second = replace(first, nomenclature="N2", nomenclature_code="N2")
        result = reconcile([first, second], {"БУХ2019": []}, "Март 2026")

        diagnostic = verify_diagnostic_excel(build_excel_report(result), result)
        actions = verify_action_excel(build_action_excel_report(result), result)

        self.assertEqual(diagnostic["rows"], 2)
        self.assertEqual(actions["action_rows"], 1)
        self.assertTrue(actions["summary_matches_action_sheets"])

        with self.assertRaises((AssertionError, KeyError)):
            verify_diagnostic_excel(build_action_excel_report(result), result)
