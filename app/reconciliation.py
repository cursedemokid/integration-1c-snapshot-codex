from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from app.document_matching import (
    DocumentIdentity,
    document_reconciliation_status,
    is_reconcilable_document,
    parse_document,
)
from app.models import Discrepancy, LedgerRow, ReconciliationResult, ReconciliationSummary


MISSING_IN_TAX = "Отсутствует в бухгалтерии"
EXTRA_IN_TAX = "Лишняя строка в бухгалтерии"
DUPLICATE_IN_TAX = "Дублирующая строка в бухгалтерии"
MISSING_ITEM_IN_TAX = "Строка товара отсутствует в документе БУХ"
EXTRA_ITEM_IN_TAX = "Лишняя строка товара в документе БУХ"
MISSING_BALANCE_IN_TAX = "Остаток отсутствует в бухгалтерии"
EXTRA_BALANCE_IN_TAX = "Лишний остаток в бухгалтерии"
DOCUMENT_QUANTITY_MISMATCH = "Различие движения количества"
DOCUMENT_AMOUNT_MISMATCH = "Различие стоимости движения"
BALANCE_QUANTITY_MISMATCH = "Различие остатков количества"
BALANCE_AMOUNT_MISMATCH = "Различие стоимости остатков"
BUH_ONLY_DOCUMENT = "Документ только в бухгалтерии"
# Compatibility for callers which imported the previous broad category.
DATA_MISMATCH = DOCUMENT_QUANTITY_MISMATCH
AMBIGUOUS_MATCH = "Невозможность однозначного сопоставления"

RELOAD_DOCUMENT = "Проверить загрузку документа и правила сопоставления"
REMOVE_DOCUMENT = "Проверить соответствие и уникальность строки бухгалтерии"
CHECK_QUANTITY = "Проверить движение количества"
CHECK_AMOUNT = "Проверить правила учета стоимости"
CHECK_BALANCE = "Проверить остаток на границе периода"
CHECK_BUH_ONLY = "Проверить, должен ли тип документа существовать только в бухгалтерии"
MANUAL_CHECK = "Выполнить ручную проверку"

NUMBER_DATE_MATCH = "Тип, номер и дата документа"
SOURCE_ID_MATCH = "Идентификатор исходного документа"
DOCUMENT_CONTENT_MATCH = "Дата, товары и количество движения"
SNAPSHOT_MATCH = "Товар и дата остатка"

_BUH_ONLY_FAMILIES = {
    "assembly",
    "customs",
    "operation",
    "receipt",
    "receipt_adjustment",
    "sales_adjustment",
    "write_off",
}


@dataclass(frozen=True)
class _DocumentGroup:
    key: tuple[str, ...]
    warehouse_key: tuple[str, str]
    organization_key: tuple[str, str]
    identity: DocumentIdentity
    rows: tuple[LedgerRow, ...]


@dataclass(frozen=True)
class _DocumentReconciliation:
    discrepancies: list[Discrepancy]
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]]
    ambiguous_documents: int
    duplicate_documents: int
    missing_documents: int
    extra_documents: int
    buh_only_documents: int


def reconcile(
    trade_rows: list[LedgerRow],
    tax_exports: dict[str, list[LedgerRow]],
    period: str,
    amount_tolerance: Decimal = Decimal("0.01"),
    quantity_tolerance: Decimal = Decimal("0"),
) -> ReconciliationResult:
    discrepancies: list[Discrepancy] = []
    summaries: list[ReconciliationSummary] = []

    if not tax_exports:
        discrepancies.append(
            Discrepancy(
                period=period,
                base="",
                document="",
                nomenclature="",
                opening_quantity=Decimal("0.00"),
                opening_amount=Decimal("0.00"),
                turnover_quantity=Decimal("0.00"),
                turnover_amount=Decimal("0.00"),
                closing_quantity=Decimal("0.00"),
                closing_amount=Decimal("0.00"),
                discrepancy_type=AMBIGUOUS_MATCH,
                recommendation="Не настроены базы бухгалтерии для сверки",
            )
        )
        return ReconciliationResult(discrepancies=discrepancies, summaries=[])

    trade_policy_counts = _document_policy_counts(trade_rows)
    all_trade_documents = _group_documents(
        row for row in trade_rows
        if row.document and is_reconcilable_document(row.document, row.period)
    )
    internal_trade_keys = _internal_transfer_keys(all_trade_documents, trade_rows)
    trade_documents = {
        key: group for key, group in all_trade_documents.items() if key not in internal_trade_keys
    }
    trade_snapshots = _aggregate_snapshot_rows([row for row in trade_rows if not row.document])

    for tax_base_name, tax_rows in tax_exports.items():
        tax_policy_counts = _document_policy_counts(tax_rows)
        tax_documents = _group_documents(
            row for row in tax_rows
            if row.document and is_reconcilable_document(row.document, row.period)
        )
        document_result = _reconcile_documents(
            trade_documents,
            tax_documents,
            tax_base_name,
            amount_tolerance,
            quantity_tolerance,
        )
        snapshot_discrepancies = _reconcile_snapshot_rows(
            trade_snapshots,
            _aggregate_snapshot_rows([row for row in tax_rows if not row.document]),
            tax_base_name,
            amount_tolerance,
            quantity_tolerance,
        )
        base_discrepancies = [*document_result.discrepancies, *snapshot_discrepancies]
        discrepancies.extend(base_discrepancies)
        match_counts = Counter(method for _, _, method in document_result.pairs)
        discrepancy_counts = Counter(item.discrepancy_type for item in base_discrepancies)
        summaries.append(
            ReconciliationSummary(
                base=tax_base_name,
                trade_documents=len(trade_documents),
                tax_documents=len(tax_documents),
                matched_documents=len(document_result.pairs),
                matched_by_number_date=match_counts[NUMBER_DATE_MATCH],
                matched_by_content=match_counts[DOCUMENT_CONTENT_MATCH],
                ambiguous_documents=document_result.ambiguous_documents,
                duplicate_documents=document_result.duplicate_documents,
                missing_documents=document_result.missing_documents,
                extra_documents=document_result.extra_documents,
                buh_only_documents=document_result.buh_only_documents,
                ignored_internal_transfers=len(internal_trade_keys),
                excluded_trade_documents=trade_policy_counts["excluded"],
                excluded_tax_documents=tax_policy_counts["excluded"],
                pending_trade_documents=trade_policy_counts["pending"],
                pending_tax_documents=tax_policy_counts["pending"],
                document_quantity_differences=discrepancy_counts[DOCUMENT_QUANTITY_MISMATCH],
                document_amount_differences=discrepancy_counts[DOCUMENT_AMOUNT_MISMATCH],
                balance_quantity_differences=discrepancy_counts[BALANCE_QUANTITY_MISMATCH],
                balance_amount_differences=discrepancy_counts[BALANCE_AMOUNT_MISMATCH],
            )
        )

    return ReconciliationResult(discrepancies=discrepancies, summaries=summaries)


def _reconcile_documents(
    trade_source: dict[tuple[str, ...], _DocumentGroup],
    tax_source: dict[tuple[str, ...], _DocumentGroup],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
) -> _DocumentReconciliation:
    trade_groups = dict(trade_source)
    tax_groups = dict(tax_source)
    discrepancies: list[Discrepancy] = []
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]] = []
    ambiguous_documents = 0
    duplicate_documents = 0

    for key_function, match_method, ambiguous_duplicates in (
        (lambda group, side: _source_id_key(group, side), SOURCE_ID_MATCH, False),
        (lambda group, side: (*group.warehouse_key, *group.identity.strong_key), NUMBER_DATE_MATCH, False),
        (lambda group, side: _content_key(group), DOCUMENT_CONTENT_MATCH, True),
    ):
        matched, ambiguous, duplicates = _consume_group_matches(
            trade_groups,
            tax_groups,
            key_function,
            match_method,
            tax_base_name,
            discrepancies,
            ambiguous_duplicates=ambiguous_duplicates,
        )
        pairs.extend(matched)
        ambiguous_documents += ambiguous
        duplicate_documents += duplicates

    for trade_group, tax_group, match_method in pairs:
        discrepancies.extend(
            _compare_document_rows(
                trade_group,
                tax_group,
                tax_base_name,
                amount_tolerance,
                quantity_tolerance,
                match_method,
            )
        )

    for group in trade_groups.values():
        discrepancies.extend(
            _from_rows(row, None, tax_base_name, MISSING_IN_TAX, RELOAD_DOCUMENT, "")
            for row in group.rows
        )
    extra_documents = 0
    buh_only_documents = 0
    for group in tax_groups.values():
        is_buh_only = group.identity.family in _BUH_ONLY_FAMILIES
        discrepancy_type = BUH_ONLY_DOCUMENT if is_buh_only else EXTRA_IN_TAX
        recommendation = CHECK_BUH_ONLY if is_buh_only else REMOVE_DOCUMENT
        if is_buh_only:
            buh_only_documents += 1
        else:
            extra_documents += 1
        discrepancies.extend(
            _from_rows(None, row, tax_base_name, discrepancy_type, recommendation, "")
            for row in group.rows
        )

    return _DocumentReconciliation(
        discrepancies=discrepancies,
        pairs=pairs,
        ambiguous_documents=ambiguous_documents,
        duplicate_documents=duplicate_documents,
        missing_documents=len(trade_groups),
        extra_documents=extra_documents,
        buh_only_documents=buh_only_documents,
    )


def _consume_group_matches(
    trade_groups: dict[tuple[str, ...], _DocumentGroup],
    tax_groups: dict[tuple[str, ...], _DocumentGroup],
    key_function: Callable[[_DocumentGroup, str], tuple[object, ...] | None],
    match_method: str,
    tax_base_name: str,
    discrepancies: list[Discrepancy],
    ambiguous_duplicates: bool = False,
) -> tuple[list[tuple[_DocumentGroup, _DocumentGroup, str]], int, int]:
    trade_index = _index_groups(trade_groups, lambda group: key_function(group, "trade"))
    tax_index = _index_groups(tax_groups, lambda group: key_function(group, "tax"))
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]] = []
    ambiguous_documents = 0
    duplicate_documents = 0

    for match_key in sorted(set(trade_index) & set(tax_index), key=str):
        trade_keys = trade_index[match_key]
        tax_keys = tax_index[match_key]
        if len(trade_keys) == len(tax_keys) == 1:
            trade_group = trade_groups.pop(trade_keys[0])
            tax_group = tax_groups.pop(tax_keys[0])
            pairs.append((trade_group, tax_group, match_method))
            continue

        trade_ambiguous = [trade_groups.pop(key) for key in trade_keys]
        tax_ambiguous = [tax_groups.pop(key) for key in tax_keys]
        if ambiguous_duplicates:
            ambiguous_documents += len(trade_ambiguous) + len(tax_ambiguous)
            for group in trade_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            row,
                            None,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Несколько документов имеют одинаковые дату, товары и количество движения",
                        )
                    )
            for group in tax_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            None,
                            row,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Несколько документов имеют одинаковые дату, товары и количество движения",
                        )
                    )
        else:
            tax_is_duplicate = (
                len(tax_ambiguous) > 1
                and len({group.organization_key for group in tax_ambiguous}) == 1
            )
            ambiguous_documents += len(trade_ambiguous)
            duplicate_documents += len(tax_ambiguous) if tax_is_duplicate else 0
            if not tax_is_duplicate:
                ambiguous_documents += len(tax_ambiguous)

            for group in trade_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            row,
                            None,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Документ УТ имеет несколько кандидатов сопоставления",
                        )
                    )
            for group in tax_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            None,
                            row,
                            tax_base_name,
                            DUPLICATE_IN_TAX if tax_is_duplicate else AMBIGUOUS_MATCH,
                            REMOVE_DOCUMENT if tax_is_duplicate else MANUAL_CHECK,
                            match_method,
                            (
                                "Повторяющийся идентификатор документа в бухгалтерии"
                                if tax_is_duplicate
                                else "Документ бухгалтерии имеет несколько кандидатов сопоставления"
                            ),
                        )
                    )
    return pairs, ambiguous_documents, duplicate_documents


def _compare_document_rows(
    trade_group: _DocumentGroup,
    tax_group: _DocumentGroup,
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    match_method: str,
) -> list[Discrepancy]:
    return _reconcile_rows(
        list(trade_group.rows),
        list(tax_group.rows),
        tax_base_name,
        amount_tolerance,
        quantity_tolerance,
        match_method,
        key_function=_item_key,
        comparison_scope="document",
    )


def _reconcile_snapshot_rows(
    trade_rows: list[LedgerRow],
    tax_rows: list[LedgerRow],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
) -> list[Discrepancy]:
    return _reconcile_rows(
        trade_rows,
        tax_rows,
        tax_base_name,
        amount_tolerance,
        quantity_tolerance,
        SNAPSHOT_MATCH,
        comparison_scope="snapshot",
    )


def _reconcile_rows(
    trade_rows: list[LedgerRow],
    tax_rows: list[LedgerRow],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    match_method: str,
    key_function: Callable[[LedgerRow], tuple[str, ...]] | None = None,
    comparison_scope: str = "snapshot",
) -> list[Discrepancy]:
    discrepancies: list[Discrepancy] = []
    key_function = key_function or (lambda row: row.match_key)
    raw_trade_index = _index_rows(trade_rows, key_function)
    raw_tax_index = _index_rows(tax_rows, key_function)
    duplicate_trade_keys = {key for key, rows in raw_trade_index.items() if len(rows) > 1}
    duplicate_tax_keys = {key for key, rows in raw_tax_index.items() if len(rows) > 1}

    entity = "товарная строка в документе" if comparison_scope == "document" else "строка остатка"
    for key in sorted(duplicate_trade_keys):
        for row in raw_trade_index[key]:
            discrepancies.append(
                _from_rows(
                    row,
                    None,
                    tax_base_name,
                    AMBIGUOUS_MATCH,
                    MANUAL_CHECK,
                    match_method,
                    f"Повторяющаяся {entity} УТ",
                )
            )
    for key in sorted(duplicate_tax_keys):
        for row in raw_tax_index[key]:
            discrepancies.append(
                _from_rows(
                    None,
                    row,
                    tax_base_name,
                    DUPLICATE_IN_TAX,
                    REMOVE_DOCUMENT,
                    match_method,
                    f"Повторяющаяся {entity} бухгалтерии",
                )
            )

    trade_index = {key: rows[0] for key, rows in raw_trade_index.items() if key not in duplicate_trade_keys}
    tax_index = {key: rows[0] for key, rows in raw_tax_index.items() if key not in duplicate_tax_keys}

    for key, trade_row in trade_index.items():
        if key in duplicate_tax_keys:
            continue
        tax_row = tax_index.get(key)
        if tax_row is None:
            discrepancy_type = MISSING_ITEM_IN_TAX if comparison_scope == "document" else MISSING_BALANCE_IN_TAX
            description = (
                "В соответствующем документе нет товарной строки"
                if comparison_scope == "document"
                else "В бухгалтерии нет остатка по товару на контрольную дату"
            )
            discrepancies.append(
                _from_rows(
                    trade_row,
                    None,
                    tax_base_name,
                    discrepancy_type,
                    RELOAD_DOCUMENT if comparison_scope == "document" else CHECK_BALANCE,
                    match_method,
                    description,
                )
            )
            continue
        if (
            trade_row.organization_inn
            and tax_row.organization_inn
            and trade_row.organization_inn != tax_row.organization_inn
        ):
            discrepancies.append(
                _from_rows(
                    trade_row,
                    tax_row,
                    tax_base_name,
                    AMBIGUOUS_MATCH,
                    MANUAL_CHECK,
                    match_method,
                    "Строки относятся к разным организациям",
                )
            )
            continue
        discrepancies.extend(
            _value_discrepancies(
                trade_row,
                tax_row,
                tax_base_name,
                match_method,
                comparison_scope,
                amount_tolerance,
                quantity_tolerance,
            )
        )

    for key, tax_row in tax_index.items():
        if key in duplicate_trade_keys or key in duplicate_tax_keys:
            continue
        if key not in trade_index:
            discrepancy_type = EXTRA_ITEM_IN_TAX if comparison_scope == "document" else EXTRA_BALANCE_IN_TAX
            description = (
                "В соответствующем документе УТ нет товарной строки"
                if comparison_scope == "document"
                else "В УТ нет остатка по товару на контрольную дату"
            )
            discrepancies.append(
                _from_rows(
                    None,
                    tax_row,
                    tax_base_name,
                    discrepancy_type,
                    REMOVE_DOCUMENT if comparison_scope == "document" else CHECK_BALANCE,
                    match_method,
                    description,
                )
            )
    return discrepancies


def _group_documents(rows) -> dict[tuple[str, ...], _DocumentGroup]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        warehouse_key = _warehouse_key(row)
        organization_key = _organization_key(row)
        grouped[(*warehouse_key, *organization_key, row.document)].append(row)

    return {
        key: _DocumentGroup(
            key=key,
            warehouse_key=_warehouse_key(group_rows[0]),
            organization_key=_organization_key(group_rows[0]),
            identity=parse_document(group_rows[0].document, group_rows[0].period),
            rows=tuple(_aggregate_document_rows(group_rows)),
        )
        for key, group_rows in grouped.items()
    }


def _document_policy_counts(rows: list[LedgerRow]) -> Counter[str]:
    unique_documents = {
        (*_warehouse_key(row), *_organization_key(row), row.document, row.period)
        for row in rows
        if row.document
    }
    counts: Counter[str] = Counter()
    for *_, document, period in unique_documents:
        counts[document_reconciliation_status(document, period)] += 1
    return counts


def _aggregate_document_rows(rows: list[LedgerRow]) -> list[LedgerRow]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        grouped[_item_key(row)].append(row)

    result: list[LedgerRow] = []
    for group_rows in grouped.values():
        first = group_rows[0]
        if len(group_rows) == 1:
            result.append(first)
            continue
        result.append(
            replace(
                first,
                turnover_quantity=sum((row.turnover_quantity for row in group_rows), Decimal("0")),
                turnover_amount=sum((row.turnover_amount for row in group_rows), Decimal("0")),
            )
        )
    return result


def _index_groups(
    groups: dict[tuple[str, ...], _DocumentGroup],
    key_function: Callable[[_DocumentGroup], tuple[object, ...] | None],
) -> dict[tuple[object, ...], list[tuple[str, ...]]]:
    result: dict[tuple[object, ...], list[tuple[str, ...]]] = defaultdict(list)
    for group_key, group in groups.items():
        match_key = key_function(group)
        if match_key is not None:
            result[match_key].append(group_key)
    return result


def _source_id_key(group: _DocumentGroup, side: str) -> tuple[object, ...] | None:
    if side == "trade":
        identifiers = {row.document_id for row in group.rows if row.document_id}
    else:
        source_identifiers = {row.source_document_id for row in group.rows if row.source_document_id}
        identifiers = source_identifiers or {row.document_id for row in group.rows if row.document_id}
    if len(identifiers) != 1:
        return None
    return (*group.warehouse_key, next(iter(identifiers)).casefold())


def _content_key(group: _DocumentGroup, side: str = "") -> tuple[object, ...] | None:
    if group.identity.family != "sale":
        return None
    content = tuple(sorted((_item_key(row), row.turnover_quantity) for row in group.rows))
    return (*group.warehouse_key, group.identity.family, group.identity.timestamp, content)


def _internal_transfer_keys(
    groups: dict[tuple[str, ...], _DocumentGroup],
    all_trade_rows: list[LedgerRow],
) -> set[tuple[str, ...]]:
    names_by_mapping: dict[str, set[str]] = defaultdict(set)
    for row in all_trade_rows:
        if row.warehouse_mapping_id and row.warehouse:
            names_by_mapping[row.warehouse_mapping_id].add(_warehouse_name_key(row.warehouse))

    raw_rows_by_group: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in all_trade_rows:
        if not row.document:
            continue
        raw_rows_by_group[
            (*_warehouse_key(row), *_organization_key(row), row.document)
        ].append(row)

    result: set[tuple[str, ...]] = set()
    for key, group in groups.items():
        if group.identity.family != "transfer" or group.warehouse_key[0] != "mapping":
            continue
        mapped_names = names_by_mapping[group.warehouse_key[1]]
        raw_rows = raw_rows_by_group.get(key, list(group.rows))
        endpoints = [
            (_warehouse_name_key(row.sender_warehouse), _warehouse_name_key(row.receiver_warehouse))
            for row in raw_rows
        ]
        if endpoints and all(sender and receiver and sender in mapped_names and receiver in mapped_names
                             for sender, receiver in endpoints):
            result.add(key)
            continue

        # В фактической выгрузке ПТФ конечные склады часто пустые. Тогда обе
        # стороны внутренней передачи уже присутствуют в выбранной логической
        # группе: разные физические склады и нулевое движение каждого товара.
        physical_warehouses = {
            row.warehouse_code or _warehouse_name_key(row.warehouse)
            for row in raw_rows
            if row.warehouse_code or row.warehouse
        }
        quantities_by_item: dict[tuple[str, ...], list[Decimal]] = defaultdict(list)
        for row in raw_rows:
            quantities_by_item[_item_key(row)].append(row.turnover_quantity)
        if (
            len(physical_warehouses) > 1
            and quantities_by_item
            and all(
                sum(values, Decimal("0")) == 0
                and any(value > 0 for value in values)
                and any(value < 0 for value in values)
                for values in quantities_by_item.values()
            )
        ):
            result.add(key)
    return result


def _aggregate_snapshot_rows(rows: list[LedgerRow]) -> list[LedgerRow]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        grouped[row.match_key].append(row)

    result: list[LedgerRow] = []
    for group_rows in grouped.values():
        physical_warehouses = {
            row.warehouse_code or _warehouse_name_key(row.warehouse)
            for row in group_rows
        }
        if (
            len(group_rows) == 1
            or not group_rows[0].warehouse_mapping_id
            or len(physical_warehouses) != len(group_rows)
        ):
            result.extend(group_rows)
            continue
        first = group_rows[0]
        result.append(
            replace(
                first,
                opening_quantity=sum((row.opening_quantity for row in group_rows), Decimal("0")),
                opening_amount=sum((row.opening_amount for row in group_rows), Decimal("0")),
                turnover_quantity=sum((row.turnover_quantity for row in group_rows), Decimal("0")),
                turnover_amount=sum((row.turnover_amount for row in group_rows), Decimal("0")),
                closing_quantity=sum((row.closing_quantity for row in group_rows), Decimal("0")),
                closing_amount=sum((row.closing_amount for row in group_rows), Decimal("0")),
                warehouse="; ".join(dict.fromkeys(row.warehouse for row in group_rows if row.warehouse)),
                warehouse_code="; ".join(dict.fromkeys(row.warehouse_code for row in group_rows if row.warehouse_code)),
            )
        )
    return result


def _index_rows(
    rows: list[LedgerRow],
    key_function: Callable[[LedgerRow], tuple[str, ...]],
) -> dict[tuple[str, ...], list[LedgerRow]]:
    result: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        result[key_function(row)].append(row)
    return result


def _item_key(row: LedgerRow) -> tuple[str, ...]:
    return (
        "code" if row.nomenclature_code else ("article" if row.article else "name"),
        row.nomenclature_code or row.article or row.nomenclature,
    )


def _warehouse_key(row: LedgerRow) -> tuple[str, str]:
    if row.warehouse_mapping_id:
        return "mapping", row.warehouse_mapping_id
    if row.warehouse_code:
        return "code", row.warehouse_code
    return "name", _warehouse_name_key(row.warehouse)


def _warehouse_name_key(value: str) -> str:
    return " ".join(value.replace("–", "-").replace("—", "-").strip().casefold().split())


def _organization_key(row: LedgerRow) -> tuple[str, str]:
    if row.organization_inn:
        return "inn", row.organization_inn
    if row.organization:
        return "name", " ".join(row.organization.strip().casefold().split())
    return "none", ""


def _value_discrepancies(
    trade_row: LedgerRow,
    tax_row: LedgerRow,
    tax_base_name: str,
    match_method: str,
    comparison_scope: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
) -> list[Discrepancy]:
    # Суммы продолжают нормализоваться для совместимости с контрактом 1С, но по
    # требованиям встречи 11.09.2026 не участвуют в пользовательской сверке.
    _ = amount_tolerance
    if comparison_scope == "document":
        profiles = (
            (DOCUMENT_QUANTITY_MISMATCH, CHECK_QUANTITY, (("оборот количества", "turnover_quantity"),), quantity_tolerance),
        )
    else:
        profiles = (
            (
                BALANCE_QUANTITY_MISMATCH,
                CHECK_BALANCE,
                (("начальный остаток количества", "opening_quantity"), ("конечный остаток количества", "closing_quantity")),
                quantity_tolerance,
            ),
        )

    discrepancies: list[Discrepancy] = []
    for discrepancy_type, recommendation, fields, tolerance in profiles:
        differences = []
        for label, field in fields:
            trade_value = getattr(trade_row, field)
            tax_value = getattr(tax_row, field)
            if abs(trade_value - tax_value) > tolerance:
                differences.append(
                    f"{label}: УТ {trade_value}, БУХ {tax_value}, разница {tax_value - trade_value}"
                )
        if differences:
            discrepancies.append(
                _from_rows(
                    trade_row,
                    tax_row,
                    tax_base_name,
                    discrepancy_type,
                    recommendation,
                    match_method,
                    "; ".join(differences),
                )
            )
    return discrepancies


def _from_rows(
    trade_row: LedgerRow | None,
    tax_row: LedgerRow | None,
    base: str,
    discrepancy_type: str,
    recommendation: str,
    match_method: str,
    difference_summary: str = "",
) -> Discrepancy:
    row = tax_row or trade_row
    if row is None:
        raise ValueError("Для расхождения нужна хотя бы одна исходная строка")
    return Discrepancy(
        period=row.period,
        base=base,
        document=row.document,
        nomenclature=row.nomenclature,
        opening_quantity=row.opening_quantity,
        opening_amount=row.opening_amount,
        turnover_quantity=row.turnover_quantity,
        turnover_amount=row.turnover_amount,
        closing_quantity=row.closing_quantity,
        closing_amount=row.closing_amount,
        discrepancy_type=discrepancy_type,
        recommendation=recommendation,
        nomenclature_code=row.nomenclature_code,
        article=row.article,
        warehouse=row.warehouse,
        warehouse_code=row.warehouse_code,
        warehouse_mapping_id=row.warehouse_mapping_id,
        organization=row.organization,
        organization_inn=row.organization_inn,
        trade_row=trade_row,
        tax_row=tax_row,
        match_method=match_method,
        difference_summary=difference_summary,
    )
