from decimal import Decimal
from unittest import TestCase

from app.normalization import normalize_rows
from scripts.verify_onec import period_balances, terminal_balance


def movement(start, change, period="01.04.2026 23:59:59", document="Document"):
    return normalize_rows([{
        "period": period, "document": document, "nomenclature": "Item", "nomenclature_code": "N1",
        "warehouse_code": "W1", "opening_quantity": "1", "turnover_quantity": "0", "closing_quantity": "1",
        "opening_amount": str(start), "turnover_amount": str(change), "closing_amount": str(start + change),
    }], "period")[0]


class BalanceDiagnosticsTests(TestCase):
    def test_unordered_documents_at_end_timestamp(self):
        rows = [movement(20, 5), movement(10, 10), movement(10, 0, document="")]
        self.assertEqual(terminal_balance(rows, "closing"), (Decimal(1), Decimal(25)))
        self.assertEqual(terminal_balance(rows, "opening"), (Decimal(1), Decimal(10)))

    def test_zero_movement_snapshot(self):
        self.assertEqual(terminal_balance([movement(10, 0)], "closing"), (Decimal(1), Decimal(10)))

    def test_disconnected_balances_are_ambiguous(self):
        self.assertIsNone(terminal_balance([movement(10, 1), movement(30, 1)], "closing"))

    def test_cycle_has_no_unambiguous_terminal(self):
        self.assertIsNone(terminal_balance([movement(10, 5), movement(15, -5)], "closing"))

    def test_negative_adjustment_is_not_replaced_by_maximum_amount(self):
        rows = [movement(10, 5), movement(15, -2)]
        self.assertEqual(terminal_balance(rows, "closing"), (Decimal(1), Decimal(13)))

    def test_boundary_can_be_document_instead_of_empty_snapshot(self):
        rows = [movement(10, 0, "01.03.2026 0:00:00", ""), movement(20, 5), movement(10, 10)]
        balances, ambiguous = period_balances(rows)
        self.assertFalse(ambiguous)
        self.assertEqual(balances[0]["closing_amount"], Decimal(25))
        self.assertEqual(balances[0]["amount_gap"], Decimal(0))

    def test_discontinuous_period_is_reported(self):
        rows = [movement(10, 0, "01.03.2026 0:00:00", ""), movement(20, 5)]
        balances, _ = period_balances(rows)
        self.assertEqual(balances[0]["amount_gap"], Decimal(10))
