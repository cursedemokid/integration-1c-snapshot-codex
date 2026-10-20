from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


_DOCUMENT_PATTERN = re.compile(
    r"^(?P<document_type>.+?)\s+(?P<number>\S+)\s+от\s+(?P<timestamp>.+?)$",
    re.IGNORECASE,
)

_DOCUMENT_FAMILIES = {
    "передача товаров филиалу": "transfer",
    "передача товаров филиалу (птф)": "transfer",
    "передача товаров": "transfer",
    "перемещение": "transfer",
    "перемещение товаров": "transfer",
    "перемещение товаров, материалов": "transfer",
    "реализация товаров и услуг": "sale",
    "реализация (акт, накладная, упд)": "sale",
    "поступление товаров": "goods_receipt",
    "поступление товаров и услуг": "goods_receipt",
    "поступление (акты, накладные, упд)": "goods_receipt",
    "оприходование": "receipt",
    "оприходование товаров": "receipt",
    "списание": "write_off",
    "списание товаров": "write_off",
    "списание товаров, материалов": "write_off",
    "требование-накладная": "requirement_pending",
    "безвозмездная передача": "free_transfer_pending",
    "комплектация номенклатуры": "assembly",
    "поступление дополнительных расходов": "excluded",
    "заказ покупателя": "excluded",
    "корректировка реализации": "sales_adjustment",
    "корректировка поступления": "receipt_adjustment",
    "таможенная декларация (импорт)": "customs",
    "операция": "operation",
}

# Эти семейства подтверждены заказчиком как изменяющие количество товара.
# Комплектации и требования-накладные намеренно не включены: для них 1С должна
# передать разновидность документа, по которой можно определить правила выгрузки.
RECONCILABLE_DOCUMENT_FAMILIES = frozenset({
    "transfer",
    "sale",
    "goods_receipt",
    "receipt",
    "write_off",
})
PENDING_DOCUMENT_FAMILIES = frozenset({
    "assembly",
    "requirement_pending",
    "free_transfer_pending",
})


def document_reconciliation_status(document: str, period: str = "") -> str:
    identity = parse_document(document, period)
    if identity.parsed and identity.family in RECONCILABLE_DOCUMENT_FAMILIES:
        return "included"
    if identity.parsed and identity.family in PENDING_DOCUMENT_FAMILIES:
        return "pending"
    return "excluded"


def is_reconcilable_document(document: str, period: str = "") -> bool:
    return document_reconciliation_status(document, period) == "included"


@dataclass(frozen=True)
class DocumentIdentity:
    original: str
    document_type: str
    family: str
    number: str
    timestamp: str
    parsed: bool

    @property
    def strong_key(self) -> tuple[str, str, str]:
        return self.family, self.number.casefold(), self.timestamp


def parse_document(document: str, period: str = "") -> DocumentIdentity:
    original = _normalize_text(document)
    if not original:
        return DocumentIdentity("", "", "snapshot", "", _normalize_timestamp(period), True)

    match = _DOCUMENT_PATTERN.match(original)
    if not match:
        return DocumentIdentity(
            original=original,
            document_type="",
            family="unparsed",
            number=original,
            timestamp=_normalize_timestamp(period),
            parsed=False,
        )

    document_type = _normalize_text(match.group("document_type"))
    return DocumentIdentity(
        original=original,
        document_type=document_type,
        family=_DOCUMENT_FAMILIES.get(document_type.casefold(), document_type.casefold()),
        number=_normalize_text(match.group("number")),
        timestamp=_normalize_timestamp(match.group("timestamp")),
        parsed=True,
    )


def _normalize_timestamp(value: str) -> str:
    normalized = _normalize_text(value)
    for date_format in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(normalized, date_format).isoformat(timespec="seconds")
        except ValueError:
            continue
    return normalized


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())
