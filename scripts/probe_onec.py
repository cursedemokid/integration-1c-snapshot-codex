"""Portable statistics for already-loaded rows; does not access 1C."""
from decimal import Decimal


def document_stats(rows):
    empty = [row for row in rows if not row.document.strip()]
    return {
        "empty_document_rows": len(empty),
        "movements_without_document": sum(
            row.turnover_quantity != Decimal(0) or row.turnover_amount != Decimal(0)
            for row in empty
        ),
        "unique_document_representations": len({row.document for row in rows if row.document.strip()}),
    }
