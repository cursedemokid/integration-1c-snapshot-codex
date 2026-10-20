from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.config import ConfigurationError, Settings, WarehouseMappingConfig
from app.models import Warehouse


@dataclass(frozen=True)
class WarehouseMappingResolution:
    mappings: tuple[WarehouseMappingConfig, ...]
    diagnostics: dict[str, object]


def resolve_warehouse_mappings(
    settings: Settings,
    directories: dict[str, list[Warehouse]],
) -> WarehouseMappingResolution:
    trade_id = settings.trade_base.base_id
    tax_bases = settings.tax_bases
    trade_warehouses = directories.get(trade_id, [])
    tax_name_indexes = {
        base.base_id: _warehouses_by_name(directories.get(base.base_id, []))
        for base in tax_bases
    }

    grouped_trade: dict[str, list[Warehouse]] = {}
    display_names: dict[str, str] = {}
    blank_trade_codes: set[str] = set()
    for warehouse in trade_warehouses:
        if not warehouse.accounting_name:
            blank_trade_codes.add(warehouse.code)
            continue
        key = _name_key(warehouse.accounting_name)
        grouped_trade.setdefault(key, []).append(warehouse)
        display_names.setdefault(key, warehouse.accounting_name)

    automatic: list[WarehouseMappingConfig] = []
    unresolved: list[dict[str, object]] = []
    for name_key, trade_group in grouped_trade.items():
        missing_bases: list[str] = []
        ambiguous_bases: list[str] = []
        accounting_warehouses: dict[str, Warehouse] = {}
        for base in tax_bases:
            matches = tax_name_indexes[base.base_id].get(name_key, [])
            if not matches:
                missing_bases.append(base.base_id)
            elif len(matches) > 1:
                ambiguous_bases.append(base.base_id)
            else:
                accounting_warehouses[base.base_id] = matches[0]

        if missing_bases or ambiguous_bases:
            unresolved.append(
                {
                    "accounting_name": display_names[name_key],
                    "trade_codes": [warehouse.code for warehouse in trade_group],
                    "trade_count": len(trade_group),
                    "missing_bases": missing_bases,
                    "ambiguous_bases": ambiguous_bases,
                }
            )
            continue

        first_tax = tax_bases[0]
        first_warehouse = accounting_warehouses[first_tax.base_id]
        codes: dict[str, tuple[str, ...]] = {
            trade_id: tuple(warehouse.code for warehouse in trade_group),
            **{
                base.base_id: (accounting_warehouses[base.base_id].code,)
                for base in tax_bases
            },
        }
        automatic.append(
            WarehouseMappingConfig(
                mapping_id=f"auto:{first_tax.base_id}:{first_warehouse.code}",
                label=first_warehouse.name,
                codes=codes,
            )
        )

    effective = _merge_manual_mappings(settings, automatic)
    mapped_codes = {
        base_id: {
            code
            for mapping in effective
            for code in mapping.codes_for(base_id)
        }
        for base_id in [trade_id, *(base.base_id for base in tax_bases)]
    }
    mapped_trade_codes = mapped_codes[trade_id]
    unresolved = [
        issue | {
            "trade_codes": [code for code in issue["trade_codes"] if code not in mapped_trade_codes],
            "trade_count": sum(code not in mapped_trade_codes for code in issue["trade_codes"]),
        }
        for issue in unresolved
    ]
    unresolved = [issue for issue in unresolved if issue["trade_count"]]
    unreferenced_accounting = [
        {
            "base_id": base.base_id,
            "code": warehouse.code,
            "name": warehouse.name,
        }
        for base in tax_bases
        for warehouse in directories.get(base.base_id, [])
        if warehouse.code not in mapped_codes[base.base_id]
    ]
    effective.sort(key=lambda mapping: (_name_key(mapping.label), mapping.mapping_id))
    diagnostics: dict[str, object] = {
        "mapped_logical_count": len(effective),
        "mapped_trade_count": len(mapped_trade_codes),
        "blank_trade_count": len(blank_trade_codes - mapped_trade_codes),
        "unresolved_trade_count": sum(int(issue["trade_count"]) for issue in unresolved),
        "unresolved_links": unresolved,
        "unreferenced_accounting": unreferenced_accounting,
    }
    return WarehouseMappingResolution(mappings=tuple(effective), diagnostics=diagnostics)


def _merge_manual_mappings(
    settings: Settings,
    automatic: list[WarehouseMappingConfig],
) -> list[WarehouseMappingConfig]:
    result = list(automatic)
    trade_id = settings.trade_base.base_id
    tax_ids = tuple(base.base_id for base in settings.tax_bases)

    for manual in settings.warehouse_mappings:
        manual_tax_codes = tuple(manual.codes_for(base_id) for base_id in tax_ids)
        target_indexes = [
            index
            for index, mapping in enumerate(result)
            if tuple(mapping.codes_for(base_id) for base_id in tax_ids) == manual_tax_codes
        ]
        conflicting_trade = {
            code: mapping.mapping_id
            for mapping in result
            for code in manual.codes_for(trade_id)
            if code in mapping.codes_for(trade_id)
            and tuple(mapping.codes_for(base_id) for base_id in tax_ids) != manual_tax_codes
        }
        if conflicting_trade:
            rendered = ", ".join(f"{code} ({mapping_id})" for code, mapping_id in conflicting_trade.items())
            raise ConfigurationError(
                f"Ручное соответствие {manual.mapping_id} противоречит СкладБухгалтерия: {rendered}"
            )

        if target_indexes:
            target_index = target_indexes[0]
            target = result[target_index]
            codes = dict(target.codes)
            codes[trade_id] = tuple(dict.fromkeys((*target.codes_for(trade_id), *manual.codes_for(trade_id))))
            result[target_index] = WarehouseMappingConfig(
                mapping_id=manual.mapping_id,
                label=target.label,
                codes=codes,
            )
            continue

        overlaps = [
            f"{base_id}:{code}"
            for mapping in result
            for base_id in manual.codes
            for code in manual.codes_for(base_id)
            if code in mapping.codes_for(base_id)
        ]
        if overlaps:
            raise ConfigurationError(
                f"Ручное соответствие {manual.mapping_id} частично пересекается с автоматическим: "
                f"{', '.join(overlaps)}"
            )
        result.append(manual)

    return result


def _warehouses_by_name(warehouses: list[Warehouse]) -> dict[str, list[Warehouse]]:
    result: dict[str, list[Warehouse]] = {}
    for warehouse in warehouses:
        result.setdefault(_name_key(warehouse.name), []).append(warehouse)
    return result


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.strip().split()).casefold()
