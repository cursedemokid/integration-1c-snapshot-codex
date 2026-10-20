from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.document_matching import parse_document


@dataclass(frozen=True)
class Warehouse:
    code: str
    name: str
    group: str = ""
    accounting_name: str = ""


@dataclass(frozen=True)
class LedgerRow:
    period: str
    document: str
    nomenclature: str
    opening_quantity: Decimal
    opening_amount: Decimal
    turnover_quantity: Decimal
    turnover_amount: Decimal
    closing_quantity: Decimal
    closing_amount: Decimal
    nomenclature_code: str = ""
    article: str = ""
    warehouse: str = ""
    warehouse_code: str = ""
    warehouse_mapping_id: str = ""
    organization: str = ""
    organization_inn: str = ""
    sender_warehouse: str = ""
    receiver_warehouse: str = ""
    document_id: str = ""
    source_document_id: str = ""

    @property
    def match_key(self) -> tuple[str, ...]:
        document = parse_document(self.document, self.period)
        return (
            document.timestamp,
            document.family,
            document.number.casefold(),
            "code" if self.nomenclature_code else "name",
            self.nomenclature_code or self.nomenclature,
            "mapping" if self.warehouse_mapping_id else ("code" if self.warehouse_code else "name"),
            self.warehouse_mapping_id or self.warehouse_code or self.warehouse,
        )


@dataclass(frozen=True)
class Discrepancy:
    period: str
    base: str
    document: str
    nomenclature: str
    opening_quantity: Decimal
    opening_amount: Decimal
    turnover_quantity: Decimal
    turnover_amount: Decimal
    closing_quantity: Decimal
    closing_amount: Decimal
    discrepancy_type: str
    recommendation: str
    nomenclature_code: str = ""
    article: str = ""
    warehouse: str = ""
    warehouse_code: str = ""
    warehouse_mapping_id: str = ""
    organization: str = ""
    organization_inn: str = ""
    trade_row: LedgerRow | None = None
    tax_row: LedgerRow | None = None
    match_method: str = ""
    difference_summary: str = ""


@dataclass(frozen=True)
class ReconciliationSummary:
    base: str
    trade_documents: int = 0
    tax_documents: int = 0
    matched_documents: int = 0
    matched_by_number_date: int = 0
    matched_by_content: int = 0
    ambiguous_documents: int = 0
    duplicate_documents: int = 0
    missing_documents: int = 0
    extra_documents: int = 0
    buh_only_documents: int = 0
    ignored_internal_transfers: int = 0
    excluded_trade_documents: int = 0
    excluded_tax_documents: int = 0
    pending_trade_documents: int = 0
    pending_tax_documents: int = 0
    document_quantity_differences: int = 0
    document_amount_differences: int = 0
    balance_quantity_differences: int = 0
    balance_amount_differences: int = 0


@dataclass(frozen=True)
class ReconciliationResult:
    discrepancies: list[Discrepancy]
    summaries: list[ReconciliationSummary] | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    start_date: date | None = None
    end_date: date | None = None
