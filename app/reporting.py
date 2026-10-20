from __future__ import annotations

import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from xml.sax.saxutils import escape

from app.document_matching import parse_document
from app.models import Discrepancy, ReconciliationResult, ReconciliationSummary
from app.reconciliation import (
    AMBIGUOUS_MATCH,
    BALANCE_AMOUNT_MISMATCH,
    BALANCE_QUANTITY_MISMATCH,
    BUH_ONLY_DOCUMENT,
    DOCUMENT_AMOUNT_MISMATCH,
    DOCUMENT_QUANTITY_MISMATCH,
    DUPLICATE_IN_TAX,
    EXTRA_BALANCE_IN_TAX,
    EXTRA_IN_TAX,
    MISSING_IN_TAX,
    MISSING_BALANCE_IN_TAX,
)


HEADERS = [
    "База",
    "Тип расхождения",
    "Способ сопоставления",
    "Описание расхождения",
    "Рекомендация",
    "Период УТ",
    "Документ УТ",
    "Период БУХ",
    "Документ БУХ",
    "Код номенклатуры",
    "Артикул",
    "Номенклатура",
    "Соответствие склада",
    "Склад УТ",
    "Код склада УТ",
    "Склад БУХ",
    "Код склада БУХ",
    "Начальный остаток УТ, кол-во",
    "Начальный остаток БУХ, кол-во",
    "Разница начального остатка, кол-во",
    "Оборот УТ, кол-во",
    "Оборот БУХ, кол-во",
    "Разница оборота, кол-во",
    "Конечный остаток УТ, кол-во",
    "Конечный остаток БУХ, кол-во",
    "Разница конечного остатка, кол-во",
    "Начальный остаток УТ, сумма",
    "Начальный остаток БУХ, сумма",
    "Разница начального остатка, сумма",
    "Оборот УТ, сумма",
    "Оборот БУХ, сумма",
    "Разница оборота, сумма",
    "Конечный остаток УТ, сумма",
    "Конечный остаток БУХ, сумма",
    "Разница конечного остатка, сумма",
    "Склад-отправитель УТ",
    "Склад-получатель УТ",
    "Организация БУХ",
    "ИНН организации БУХ",
]


def build_excel_report(source: ReconciliationResult | list[Discrepancy]) -> bytes:
    if isinstance(source, ReconciliationResult):
        discrepancies = source.discrepancies
        summaries = source.summaries or []
    else:
        discrepancies = source
        summaries = []

    if discrepancies:
        detail_rows = [HEADERS] + [
            _report_row(item)
            for item in discrepancies
        ]
        sheets = [
            ("Итоги", _summary_rows(discrepancies, summaries)),
            ("Расхождения", detail_rows),
        ]
        balance_types = {
            BALANCE_QUANTITY_MISMATCH,
            BALANCE_AMOUNT_MISMATCH,
            MISSING_BALANCE_IN_TAX,
            EXTRA_BALANCE_IN_TAX,
        }
        categories = (
            (
                "Документы",
                [item for item in discrepancies if item.discrepancy_type not in {
                    DOCUMENT_QUANTITY_MISMATCH,
                    DOCUMENT_AMOUNT_MISMATCH,
                    *balance_types,
                }],
            ),
            ("Количество", [item for item in discrepancies if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH]),
            ("Остатки", [item for item in discrepancies if item.discrepancy_type in balance_types]),
        )
        sheets.extend((name, [HEADERS] + [_report_row(item) for item in items]) for name, items in categories)
    elif summaries:
        sheets = [("Итоги", _summary_rows([], summaries))]
    else:
        sheets = [("Итоги", [["Расхождения не обнаружены"]])]

    return _build_xlsx(sheets)


def _summary_rows(
    discrepancies: list[Discrepancy],
    summaries: list[ReconciliationSummary],
) -> list[list[object]]:
    grouped: dict[tuple[str, str, str], list[Discrepancy]] = defaultdict(list)
    for item in discrepancies:
        grouped[(item.base, item.discrepancy_type, item.match_method or "Не сопоставлен")].append(item)

    rows: list[list[object]] = [
        ["Итоги сверки"],
        ["Всего строк с расхождениями", len(discrepancies)],
        [],
        ["База", "Тип расхождения", "Способ сопоставления", "Строк", "Документов"],
    ]
    for (base, discrepancy_type, match_method), items in sorted(grouped.items()):
        documents = {
            (item.trade_row.document if item.trade_row else "")
            or (item.tax_row.document if item.tax_row else "")
            or f"остаток:{item.period}:{item.nomenclature_code or item.nomenclature}"
            for item in items
        }
        rows.append([base, discrepancy_type, match_method, len(items), len(documents)])
    if summaries:
        metrics = (
            ("Документов УТ", "trade_documents"),
            ("Документов БУХ", "tax_documents"),
            ("Сопоставлено", "matched_documents"),
            ("По номеру и дате", "matched_by_number_date"),
            ("По составу", "matched_by_content"),
            ("Неоднозначных", "ambiguous_documents"),
            ("Возможных дублей", "duplicate_documents"),
            ("Не найдено в БУХ", "missing_documents"),
            ("Лишних в БУХ", "extra_documents"),
            ("Только в БУХ", "buh_only_documents"),
            ("Исключено внутренних перемещений", "ignored_internal_transfers"),
            ("Исключено документов УТ по типу", "excluded_trade_documents"),
            ("Исключено документов БУХ по типу", "excluded_tax_documents"),
            ("Ожидают правила типа в УТ", "pending_trade_documents"),
            ("Ожидают правила типа в БУХ", "pending_tax_documents"),
            ("Различий количества движения", "document_quantity_differences"),
            ("Различий количества остатков", "balance_quantity_differences"),
        )
        rows.extend(
            [
                [],
                ["Статистика сопоставления документов"],
                ["Показатель", *(summary.base for summary in summaries)],
            ]
        )
        rows.extend(
            [label, *(getattr(summary, field) for summary in summaries)]
            for label, field in metrics
        )
    return rows


def _report_row(item: Discrepancy) -> list[object]:
    trade = item.trade_row
    tax = item.tax_row
    row = tax or trade
    if row is None:
        raise ValueError("Расхождение не содержит исходную строку")
    return [
        item.base,
        item.discrepancy_type,
        item.match_method,
        item.difference_summary,
        item.recommendation,
        _value(trade, "period"),
        _value(trade, "document"),
        _value(tax, "period"),
        _value(tax, "document"),
        row.nomenclature_code,
        row.article,
        row.nomenclature,
        row.warehouse_mapping_id,
        _value(trade, "warehouse"),
        _value(trade, "warehouse_code"),
        _value(tax, "warehouse"),
        _value(tax, "warehouse_code"),
        _value(trade, "opening_quantity"),
        _value(tax, "opening_quantity"),
        _delta(trade, tax, "opening_quantity"),
        _value(trade, "turnover_quantity"),
        _value(tax, "turnover_quantity"),
        _delta(trade, tax, "turnover_quantity"),
        _value(trade, "closing_quantity"),
        _value(tax, "closing_quantity"),
        _delta(trade, tax, "closing_quantity"),
        _value(trade, "opening_amount"),
        _value(tax, "opening_amount"),
        _delta(trade, tax, "opening_amount"),
        _value(trade, "turnover_amount"),
        _value(tax, "turnover_amount"),
        _delta(trade, tax, "turnover_amount"),
        _value(trade, "closing_amount"),
        _value(tax, "closing_amount"),
        _delta(trade, tax, "closing_amount"),
        _value(trade, "sender_warehouse"),
        _value(trade, "receiver_warehouse"),
        _value(tax, "organization"),
        _value(tax, "organization_inn"),
    ]


def _value(row, field: str) -> object:
    return getattr(row, field) if row is not None else ""


def _delta(trade, tax, field: str) -> object:
    if trade is None or tax is None:
        return ""
    return getattr(tax, field) - getattr(trade, field)


ACTION_HEADERS = [
    "Действие",
    "База",
    "Причина",
    "Тип документа",
    "Номер",
    "Дата и время",
    "Документ УТ",
    "Документ БУХ",
    "Склад УТ",
    "Склад БУХ",
    "Найдено вариантов",
]

BALANCE_HEADERS = [
    "Дата расхождения",
    "Артикул",
    "Наименование",
    "Остаток в УТ",
    "Остаток в НБ",
    "Расхождение",
    "База",
    "Тип остатка",
    "Причина",
    "Логический склад",
    "Код номенклатуры",
    "Склад УТ",
    "Склад НБ",
]

ADD_TO_TAX = "Довнести в БУХ"
EXTRA_IN_TAX_SHEET = "Лишние в БУХ"
MANUAL_REVIEW = "Проверить вручную"
UNCHECKED_BASES = "Базы не проверены"
BALANCE_DIFFERENCES = "Расхождения остатков"


@dataclass(frozen=True)
class _DocumentAction:
    section: str
    action: str
    base: str
    reason: str
    document_type: str
    number: str
    timestamp: str
    trade_document: str
    tax_document: str
    trade_warehouse: str
    tax_warehouse: str
    variants: int


@dataclass(frozen=True)
class _BalanceDifference:
    base: str
    control_date: str
    balance_type: str
    reason: str
    logical_warehouse: str
    nomenclature_code: str
    article: str
    nomenclature: str
    trade_warehouse: str
    tax_warehouse: str
    trade_quantity: Decimal
    tax_quantity: Decimal
    quantity_difference: Decimal


def build_action_excel_report(source: ReconciliationResult | list[Discrepancy]) -> bytes:
    discrepancies = source.discrepancies if isinstance(source, ReconciliationResult) else source
    summaries = (source.summaries or []) if isinstance(source, ReconciliationResult) else []
    source_errors = source.source_errors if isinstance(source, ReconciliationResult) else {}
    start_date = source.start_date if isinstance(source, ReconciliationResult) else None
    end_date = source.end_date if isinstance(source, ReconciliationResult) else None
    actions = _document_actions(discrepancies)
    balance_differences = _balance_differences(discrepancies, start_date, end_date)
    direct_actions = sum(action.section != MANUAL_REVIEW for action in actions)
    manual_actions = len(actions) - direct_actions

    grouped: dict[tuple[str, str], int] = defaultdict(int)
    for action in actions:
        grouped[(action.section, action.base)] += 1
    balance_counts: dict[str, int] = defaultdict(int)
    for difference in balance_differences:
        balance_counts[difference.base] += 1

    summary_rows: list[list[object]] = [
        ["Список документов для исправления"],
        ["Требуют исправления", direct_actions],
        ["Требуют ручной проверки", manual_actions],
        ["Баз проверено", len(summaries)],
        ["Баз не проверено", len(source_errors)],
        ["Расхождений остатков", len(balance_differences)],
        ["Раздел", "База", "Документов"],
    ]
    summary_rows.extend(
        [section, base, count]
        for (section, base), count in sorted(grouped.items())
    )
    summary_rows.extend([
        [],
        ["Статистика по базам"],
        [
            "База",
            "Документов УТ",
            "Документов БУХ",
            "Сопоставлено",
            "Не найдено в БУХ",
            "Лишних в БУХ",
            "Дублей",
            "Неоднозначных",
            "Внутренних перемещений исключено",
            "Исключено по типу УТ",
            "Исключено по типу БУХ",
            "Ожидают правила УТ",
            "Ожидают правила БУХ",
            "Расхождений остатков",
        ],
    ])
    summary_rows.extend(
        [
            summary.base,
            summary.trade_documents,
            summary.tax_documents,
            summary.matched_documents,
            summary.missing_documents,
            summary.extra_documents,
            summary.duplicate_documents,
            summary.ambiguous_documents,
            summary.ignored_internal_transfers,
            summary.excluded_trade_documents,
            summary.excluded_tax_documents,
            summary.pending_trade_documents,
            summary.pending_tax_documents,
            balance_counts[summary.base],
        ]
        for summary in summaries
    )
    summary_rows.extend([
        [],
        [UNCHECKED_BASES],
        ["База", "Статус", "Причина"],
    ])
    summary_rows.extend(
        [base, "Не проверена", reason]
        for base, reason in source_errors.items()
    )
    summary_rows.extend([
        [],
        ["Инструкция"],
        [ADD_TO_TAX, "Довнести указанный документ из УТ в соответствующую базу БУХ."],
        [EXTRA_IN_TAX_SHEET, "Проверить документ в БУХ и при подтверждении удалить или сторнировать."],
        [MANUAL_REVIEW, "Не изменять автоматически; проверить соответствие документов вручную."],
    ])

    balance_rows = [BALANCE_HEADERS]
    balance_rows.extend(_balance_row(item) for item in balance_differences)
    sheets: list[tuple[str, list[list[object]]]] = [
        ("Итоги", summary_rows),
        (BALANCE_DIFFERENCES, balance_rows),
    ]
    for section in (ADD_TO_TAX, EXTRA_IN_TAX_SHEET, MANUAL_REVIEW):
        section_rows = [ACTION_HEADERS]
        section_rows.extend(_action_row(action) for action in actions if action.section == section)
        sheets.append((section, section_rows))
    if source_errors:
        unchecked_rows: list[list[object]] = [["База", "Статус", "Причина", "Что сделать"]]
        unchecked_rows.extend(
            [
                base,
                "Не проверена",
                reason,
                "Исправить или проверить эндпоинт 1С и повторить сверку.",
            ]
            for base, reason in source_errors.items()
        )
        sheets.append((UNCHECKED_BASES, unchecked_rows))
    return _build_xlsx(sheets, action_report=True)


def _balance_differences(
    discrepancies: list[Discrepancy],
    start_date: date | None,
    end_date: date | None,
) -> list[_BalanceDifference]:
    balance_types = {
        BALANCE_QUANTITY_MISMATCH,
        MISSING_BALANCE_IN_TAX,
        EXTRA_BALANCE_IN_TAX,
    }
    source = [item for item in discrepancies if item.discrepancy_type in balance_types]
    parsed_dates = [
        parsed.date()
        for item in source
        if (parsed := _parse_period(item.period)) is not None
    ]
    inferred_start = start_date or (min(parsed_dates) if parsed_dates else None)
    inferred_end = end_date or (max(parsed_dates) if parsed_dates else None)
    grouped: dict[tuple[object, ...], _BalanceDifference] = {}

    for item in source:
        trade = item.trade_row
        tax = item.tax_row
        row = trade or tax
        if row is None:
            continue
        field_prefix, balance_type, control_date = _control_point(
            row.period,
            inferred_start,
            inferred_end,
        )
        quantity_field = f"{field_prefix}_quantity"
        trade_quantity = getattr(trade, quantity_field) if trade is not None else Decimal("0")
        tax_quantity = getattr(tax, quantity_field) if tax is not None else Decimal("0")
        reason = _balance_reason(trade, tax)
        logical_warehouse = (
            _first_value(trade, tax, "warehouse_mapping_id")
            or _first_value(trade, tax, "warehouse_code")
            or _first_value(trade, tax, "warehouse")
        )
        difference = _BalanceDifference(
            base=item.base,
            control_date=control_date,
            balance_type=balance_type,
            reason=reason,
            logical_warehouse=logical_warehouse,
            nomenclature_code=_first_value(trade, tax, "nomenclature_code"),
            article=_first_value(trade, tax, "article"),
            nomenclature=_first_value(trade, tax, "nomenclature"),
            trade_warehouse=_first_value(trade, None, "warehouse"),
            tax_warehouse=_first_value(tax, None, "warehouse"),
            trade_quantity=trade_quantity,
            tax_quantity=tax_quantity,
            quantity_difference=tax_quantity - trade_quantity,
        )
        item_identity = (
            difference.nomenclature_code
            or difference.article
            or difference.nomenclature.casefold()
        )
        key = (
            difference.base,
            difference.logical_warehouse,
            item_identity,
            difference.control_date,
            difference.balance_type,
        )
        grouped[key] = difference

    return sorted(
        grouped.values(),
        key=lambda item: (
            item.base.casefold(),
            _display_date_sort_key(item.control_date),
            item.balance_type,
            item.logical_warehouse.casefold(),
            (item.article or item.nomenclature_code or item.nomenclature).casefold(),
        ),
    )


def _control_point(
    period: str,
    start_date: date | None,
    end_date: date | None,
) -> tuple[str, str, str]:
    parsed = _parse_period(period)
    control_date = parsed.date() if parsed is not None else None
    if control_date is not None and start_date is not None and end_date is not None:
        if control_date == start_date != end_date:
            return "opening", "Начальный остаток", control_date.strftime("%d.%m.%Y")
        if control_date == end_date != start_date:
            return "closing", "Конечный остаток", control_date.strftime("%d.%m.%Y")
        if control_date == start_date == end_date:
            is_opening = parsed.time() <= time(12)
            return (
                "opening" if is_opening else "closing",
                "Начальный остаток" if is_opening else "Конечный остаток",
                control_date.strftime("%d.%m.%Y"),
            )
    if parsed is not None:
        is_opening = parsed.time() <= time(12)
        return (
            "opening" if is_opening else "closing",
            "Начальный остаток" if is_opening else "Конечный остаток",
            parsed.strftime("%d.%m.%Y"),
        )
    return "opening", "Начальный остаток", period


def _parse_period(value: str) -> datetime | None:
    normalized = " ".join(value.strip().split())
    for format_string in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(normalized, format_string)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _balance_reason(
    trade,
    tax,
) -> str:
    if trade is None:
        return "Нет остатка в УТ"
    if tax is None:
        return "Нет остатка в БУХ"
    return "Не совпадает количество"


def _first_value(primary, secondary, field: str) -> str:
    for row in (primary, secondary):
        if row is not None:
            value = getattr(row, field)
            if value:
                return str(value)
    return ""


def _display_date_sort_key(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return value


def _balance_row(item: _BalanceDifference) -> list[object]:
    return [
        item.control_date,
        item.article,
        item.nomenclature,
        item.trade_quantity,
        item.tax_quantity,
        item.quantity_difference,
        item.base,
        item.balance_type,
        item.reason,
        item.logical_warehouse,
        item.nomenclature_code,
        item.trade_warehouse,
        item.tax_warehouse,
    ]


def _document_actions(discrepancies: list[Discrepancy]) -> list[_DocumentAction]:
    grouped: dict[tuple[object, ...], list[Discrepancy]] = defaultdict(list)
    for item in discrepancies:
        section = _action_section(item.discrepancy_type)
        if section is None:
            continue
        row = item.trade_row or item.tax_row
        if row is None or not row.document:
            continue
        identity = parse_document(row.document, row.period)
        side = "trade" if item.trade_row is not None else "tax"
        grouped[(
            section,
            item.base,
            item.discrepancy_type,
            side,
            row.warehouse_mapping_id,
            row.organization_inn,
            identity.family,
            identity.number.casefold(),
            identity.timestamp,
        )].append(item)

    actions = [_document_action(items) for items in grouped.values()]
    return sorted(
        actions,
        key=lambda action: (
            (ADD_TO_TAX, EXTRA_IN_TAX_SHEET, MANUAL_REVIEW).index(action.section),
            action.base.casefold(),
            _timestamp_sort_key(action.timestamp),
            action.document_type.casefold(),
            action.number.casefold(),
        ),
    )


def _action_section(discrepancy_type: str) -> str | None:
    if discrepancy_type == MISSING_IN_TAX:
        return ADD_TO_TAX
    if discrepancy_type in {EXTRA_IN_TAX, DUPLICATE_IN_TAX}:
        return EXTRA_IN_TAX_SHEET
    if discrepancy_type in {BUH_ONLY_DOCUMENT, AMBIGUOUS_MATCH}:
        return MANUAL_REVIEW
    return None


def _document_action(items: list[Discrepancy]) -> _DocumentAction:
    first = items[0]
    rows = [row for item in items for row in (item.trade_row, item.tax_row) if row is not None]
    trade_rows = [item.trade_row for item in items if item.trade_row is not None]
    tax_rows = [item.tax_row for item in items if item.tax_row is not None]
    identity_row = trade_rows[0] if trade_rows else tax_rows[0]
    identity = parse_document(identity_row.document, identity_row.period)
    section = _action_section(first.discrepancy_type)
    if section is None:
        raise ValueError(f"Неподдерживаемый тип действия: {first.discrepancy_type}")

    if first.discrepancy_type == MISSING_IN_TAX:
        action = f"Довнести в {first.base}"
        reason = f"Документ есть в УТ и не найден в {first.base}"
    elif first.discrepancy_type == EXTRA_IN_TAX:
        action = f"Проверить и удалить или сторнировать в {first.base}"
        reason = f"Документ есть в {first.base} и не найден в УТ"
    elif first.discrepancy_type == DUPLICATE_IN_TAX:
        action = f"Проверить дубль в {first.base}"
        reason = f"В {first.base} найдено несколько документов с одинаковыми типом, номером и датой"
    elif first.discrepancy_type == BUH_ONLY_DOCUMENT:
        action = f"Проверить допустимость в {first.base}"
        reason = f"Тип документа может создаваться только в {first.base}"
    else:
        action = "Сопоставить вручную"
        reason = first.difference_summary or "Найдено несколько возможных соответствий"

    variants = {row.document for row in rows if row.document}
    return _DocumentAction(
        section=section,
        action=action,
        base=first.base,
        reason=reason,
        document_type=identity.document_type or "Не распознан",
        number=identity.number,
        timestamp=_display_timestamp(identity.timestamp),
        trade_document="; ".join(dict.fromkeys(row.document for row in trade_rows)),
        tax_document="; ".join(dict.fromkeys(row.document for row in tax_rows)),
        trade_warehouse="; ".join(sorted({row.warehouse for row in trade_rows if row.warehouse})),
        tax_warehouse="; ".join(sorted({row.warehouse for row in tax_rows if row.warehouse})),
        variants=max(len(variants), 1),
    )


def _display_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return value


def _timestamp_sort_key(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y %H:%M:%S").isoformat()
    except ValueError:
        return value


def _action_row(action: _DocumentAction) -> list[object]:
    return [
        action.action,
        action.base,
        action.reason,
        action.document_type,
        action.number,
        action.timestamp,
        action.trade_document,
        action.tax_document,
        action.trade_warehouse,
        action.tax_warehouse,
        action.variants,
    ]


def _build_xlsx(sheets: list[tuple[str, list[list[object]]]], action_report: bool = False) -> bytes:
    output = io.BytesIO()
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("docProps/core.xml", _core_xml(timestamp))
        archive.writestr("docProps/app.xml", _app_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml([name for name, _ in sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, (sheet_name, rows) in enumerate(sheets, start=1):
            is_summary = index == 1 and len(sheets) > 1
            is_balance_report = sheet_name == BALANCE_DIFFERENCES
            primary_header_row = next(
                (row_index for row_index, row in enumerate(rows, start=1) if row and row[0] in {"База", "Раздел"}),
                4,
            ) if is_summary else 1
            header_rows = {1, primary_header_row} if is_summary else {1}
            if is_summary:
                header_rows.update(
                    row_index
                    for row_index, row in enumerate(rows, start=1)
                    if row and row[0] in {
                        "Статистика сопоставления документов",
                        "Показатель",
                        "Инструкция",
                        "Статистика по базам",
                        "База",
                        UNCHECKED_BASES,
                    }
                )
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(
                    rows,
                    header_rows=header_rows,
                    freeze_rows=primary_header_row if is_summary else 1,
                    filter_row=primary_header_row if is_summary else 1,
                    summary=is_summary,
                    action_report=action_report,
                    balance_report=is_balance_report,
                ),
            )

    return output.getvalue()


def _sheet_xml(
    rows: list[list[object]],
    header_rows: set[int] | None = None,
    freeze_rows: int = 1,
    filter_row: int = 1,
    summary: bool = False,
    action_report: bool = False,
    balance_report: bool = False,
) -> str:
    header_rows = header_rows or {1}
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(column_index)}{row_index}"
            cells.append(_cell_xml(
                cell_ref,
                value,
                header=row_index in header_rows,
                action_body=action_report and not summary and row_index not in header_rows,
            ))
        if row_index in header_rows:
            row_attributes = ' ht="32" customHeight="1"'
        elif action_report and not summary:
            row_attributes = ' ht="36" customHeight="1"'
        else:
            row_attributes = ""
        row_xml.append(f'<row r="{row_index}"{row_attributes}>{"".join(cells)}</row>')

    last_row = max(len(rows), 1)
    last_column = _column_name(max((len(row) for row in rows), default=1))
    columns = (
        """
    <col min="1" max="1" width="42" customWidth="1"/>
    <col min="2" max="2" width="34" customWidth="1"/>
    <col min="3" max="3" width="38" customWidth="1"/>
    <col min="4" max="5" width="18" customWidth="1"/>"""
        if summary
        else """
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="2" width="20" customWidth="1"/>
    <col min="3" max="3" width="48" customWidth="1"/>
    <col min="4" max="6" width="18" customWidth="1"/>
    <col min="7" max="7" width="16" customWidth="1"/>
    <col min="8" max="8" width="22" customWidth="1"/>
    <col min="9" max="9" width="28" customWidth="1"/>
    <col min="10" max="11" width="24" customWidth="1"/>
    <col min="12" max="13" width="42" customWidth="1"/>"""
        if balance_report
        else """
    <col min="1" max="1" width="46" customWidth="1"/>
    <col min="2" max="2" width="18" customWidth="1"/>
    <col min="3" max="3" width="48" customWidth="1"/>
    <col min="4" max="4" width="36" customWidth="1"/>
    <col min="5" max="6" width="22" customWidth="1"/>
    <col min="7" max="8" width="52" customWidth="1"/>
    <col min="9" max="10" width="42" customWidth="1"/>
    <col min="11" max="11" width="20" customWidth="1"/>"""
        if action_report
        else """
    <col min="1" max="2" width="24" customWidth="1"/>
    <col min="3" max="3" width="30" customWidth="1"/>
    <col min="4" max="5" width="60" customWidth="1"/>
    <col min="6" max="9" width="42" customWidth="1"/>
    <col min="10" max="11" width="22" customWidth="1"/>
    <col min="12" max="12" width="48" customWidth="1"/>
    <col min="13" max="17" width="28" customWidth="1"/>
    <col min="18" max="35" width="18" customWidth="1"/>
    <col min="36" max="38" width="32" customWidth="1"/>
    <col min="39" max="39" width="20" customWidth="1"/>"""
    )

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://internal.internal.example.invalid/spreadsheetml/2006/main">
  <dimension ref="A1:{last_column}{last_row}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>{columns}
  </cols>
  <sheetData>{"".join(row_xml)}</sheetData>
  <autoFilter ref="A{filter_row}:{last_column}{last_row}"/>
</worksheet>'''


def _cell_xml(cell_ref: str, value: object, header: bool = False, action_body: bool = False) -> str:
    style = ' s="2"' if header else (' s="3"' if action_body else "")
    if isinstance(value, Decimal):
        number_style = ' s="3"' if action_body else ' s="1"'
        return f'<c r="{cell_ref}"{number_style}><v>{value}</v></c>'
    if isinstance(value, int | float):
        number_style = ' s="3"' if action_body else ' s="1"'
        return f'<c r="{cell_ref}"{number_style}><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{cell_ref}"{style} t="inlineStr"><is><t>{text}</t></is></c>'


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types_xml(sheet_count: int) -> str:
    worksheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://internal.internal.example.invalid/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  {worksheets}
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''


def _root_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://internal.internal.example.invalid/package/2006/relationships">
  <Relationship Id="rId1" Type="http://internal.internal.example.invalid/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://internal.internal.example.invalid/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://internal.internal.example.invalid/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def _core_xml(timestamp: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://internal.internal.example.invalid/package/2006/metadata/core-properties" xmlns:dc="http://internal.example.invalid/dc/elements/1.1/" xmlns:dcterms="http://internal.example.invalid/dc/terms/" xmlns:xsi="http://internal.example.invalid/2001/XMLSchema-instance">
  <dc:creator>IntegrationOneS</dc:creator>
  <cp:lastModifiedBy>IntegrationOneS</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>'''


def _app_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://internal.internal.example.invalid/officeDocument/2006/extended-properties" xmlns:vt="http://internal.internal.example.invalid/officeDocument/2006/docPropsVTypes">
  <Application>IntegrationOneS</Application>
</Properties>'''


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://internal.internal.example.invalid/spreadsheetml/2006/main" xmlns:r="http://internal.internal.example.invalid/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>'''


def _workbook_rels_xml(sheet_count: int) -> str:
    worksheets = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://internal.internal.example.invalid/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    styles_id = sheet_count + 1
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://internal.internal.example.invalid/package/2006/relationships">
  {worksheets}
  <Relationship Id="rId{styles_id}" Type="http://internal.internal.example.invalid/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://internal.internal.example.invalid/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00########"/></numFmts>
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF245B78"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFD9E2E8"/></left><right style="thin"><color rgb="FFD9E2E8"/></right><top style="thin"><color rgb="FFD9E2E8"/></top><bottom style="thin"><color rgb="FFD9E2E8"/></bottom><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
