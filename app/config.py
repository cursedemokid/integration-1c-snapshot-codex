from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OneCBaseConfig:
    name: str
    url: str
    base_id: str = ""
    warehouses_url: str | None = None
    username: str | None = None
    REDACTED: str | None = None
    request_method: str = "GET"
    date_format: str = "iso"
    period_begin_key: str = "start_date"
    period_end_key: str = "end_date"
    request_payload: str = "auto"
    supports_warehouse_filter: bool = False


@dataclass(frozen=True)
class WarehouseMappingConfig:
    mapping_id: str
    label: str
    codes: dict[str, tuple[str, ...] | list[str] | str]

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for base_id, raw_codes in self.codes.items():
            if isinstance(raw_codes, str):
                values = [raw_codes]
            elif isinstance(raw_codes, (list, tuple)):
                values = list(raw_codes)
            else:
                raise ConfigurationError(
                    f"Коды складов базы {base_id} в соответствии {self.mapping_id} должны быть строкой или списком"
                )
            codes = tuple(dict.fromkeys(
                " ".join(str(code).strip().split())
                for code in values
                if code is not None and str(code).strip()
            ))
            if not codes:
                raise ConfigurationError(f"Не заданы склады базы {base_id} для соответствия {self.mapping_id}")
            normalized[base_id] = codes
        object.__setattr__(self, "codes", normalized)

    def codes_for(self, base_id: str) -> tuple[str, ...]:
        return self.codes[base_id]  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    trade_base: OneCBaseConfig
    tax_bases: list[OneCBaseConfig]
    timeout_seconds: float = 120.0
    amount_tolerance: Decimal = Decimal("0.01")
    quantity_tolerance: Decimal = Decimal("0")
    warehouse_mappings: list[WarehouseMappingConfig] = field(default_factory=list)
    retry_max_attempts: int = 3
    retry_backoff_seconds: float = 0.2
    max_concurrent_reconciliations: int = 2


def load_settings(env_file: str = ".env") -> Settings:
    values = _load_env_file(env_file) | dict(os.environ)

    trade_base = OneCBaseConfig(
        name=values.get("ONEC_TRADE_NAME", "Управление торговлей"),
        url=_required(values, "ONEC_TRADE_URL"),
        base_id=values.get("ONEC_TRADE_BASE_ID", "trade"),
        warehouses_url=values.get("ONEC_TRADE_WAREHOUSES_URL"),
        username=values.get("ONEC_TRADE_USERNAME"),
        REDACTED=values.get("ONEC_TRADE_PASSWORD"),
        request_method=values.get("ONEC_TRADE_REQUEST_METHOD", "GET"),
        date_format=values.get("ONEC_TRADE_DATE_FORMAT", "iso"),
        period_begin_key=values.get("ONEC_TRADE_PERIOD_BEGIN_KEY", "start_date"),
        period_end_key=values.get("ONEC_TRADE_PERIOD_END_KEY", "end_date"),
        request_payload=values.get("ONEC_TRADE_REQUEST_PAYLOAD", "auto"),
        supports_warehouse_filter=_parse_bool(values.get("ONEC_TRADE_SUPPORTS_WAREHOUSE_FILTER", "false")),
    )

    tax_bases_raw = _required(values, "ONEC_TAX_BASES_JSON")
    try:
        tax_bases_payload = json.loads(tax_bases_raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("ONEC_TAX_BASES_JSON должен быть валидным JSON") from exc

    if not isinstance(tax_bases_payload, list) or not tax_bases_payload:
        raise ConfigurationError("ONEC_TAX_BASES_JSON должен содержать непустой список баз")

    tax_bases = [_parse_base_config(item, index, values) for index, item in enumerate(tax_bases_payload, start=1)]
    if len({base.name for base in tax_bases}) != len(tax_bases):
        raise ConfigurationError("Названия бухгалтерских баз должны быть уникальными")
    bases = [trade_base, *tax_bases]
    if any(not base.base_id.strip() for base in bases):
        raise ConfigurationError("Идентификаторы баз не должны быть пустыми")
    if len({base.base_id for base in bases}) != len(bases):
        raise ConfigurationError("Идентификаторы баз должны быть уникальными")

    warehouse_mappings = _parse_warehouse_mappings(
        values.get("ONEC_WAREHOUSE_MAPPINGS_JSON", "[]"),
        {base.base_id for base in bases},
    )

    return Settings(
        trade_base=trade_base,
        tax_bases=tax_bases,
        timeout_seconds=float(values.get("ONEC_HTTP_TIMEOUT_SECONDS", "120")),
        amount_tolerance=Decimal(values.get("ONEC_AMOUNT_TOLERANCE", "0.01")),
        quantity_tolerance=Decimal(values.get("ONEC_QUANTITY_TOLERANCE", "0")),
        warehouse_mappings=warehouse_mappings,
        retry_max_attempts=_positive_int(values, "ONEC_RETRY_MAX_ATTEMPTS", 3),
        retry_backoff_seconds=_non_negative_float(values, "ONEC_RETRY_BACKOFF_SECONDS", 0.2),
        max_concurrent_reconciliations=_positive_int(
            values, "ONEC_MAX_CONCURRENT_RECONCILIATIONS", 2
        ),
    )


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} должен быть целым числом, получено: {raw}") from exc
    if parsed < 1:
        raise ConfigurationError(f"{key} должен быть не меньше 1, получено: {raw}")
    return parsed


def _non_negative_float(values: dict[str, str], key: str, default: float) -> float:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} должен быть числом, получено: {raw}") from exc
    if parsed < 0:
        raise ConfigurationError(f"{key} не может быть отрицательным, получено: {raw}")
    return parsed


def _load_env_file(env_file: str) -> dict[str, str]:
    path = Path(env_file)
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _required(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise ConfigurationError(f"Не задан обязательный параметр конфигурации {key}")
    return value


def _parse_base_config(item: Any, index: int, values: dict[str, str] | None = None) -> OneCBaseConfig:
    if not isinstance(item, dict):
        raise ConfigurationError(f"Налоговая база #{index} должна быть объектом")
    name = item.get("name") or f"Налоговая база {index}"
    url = item.get("url")
    if not url:
        raise ConfigurationError(f"Для налоговой базы {name} не задан url")
    return OneCBaseConfig(
        name=str(name),
        url=str(url),
        base_id=str(item.get("base_id", f"tax{index}")),
        warehouses_url=item.get("warehouses_url"),
        username=_required(values or {}, item["username_env"]) if item.get("username_env") else item.get("username"),
        REDACTED=_required(values or {}, item["REDACTED_env"]) if item.get("REDACTED_env") else item.get("REDACTED"),
        request_method=str(item.get("request_method", "GET")),
        date_format=str(item.get("date_format", "iso")),
        period_begin_key=str(item.get("period_begin_key", "start_date")),
        period_end_key=str(item.get("period_end_key", "end_date")),
        request_payload=str(item.get("request_payload", "auto")),
        supports_warehouse_filter=_parse_bool(item.get("supports_warehouse_filter", False)),
    )


def _parse_warehouse_mappings(raw: str, base_ids: set[str]) -> list[WarehouseMappingConfig]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("ONEC_WAREHOUSE_MAPPINGS_JSON должен быть валидным JSON") from exc
    if not isinstance(payload, list):
        raise ConfigurationError("ONEC_WAREHOUSE_MAPPINGS_JSON должен содержать список")

    result: list[WarehouseMappingConfig] = []
    seen_ids: set[str] = set()
    seen_codes: dict[str, set[str]] = {base_id: set() for base_id in base_ids}
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"Соответствие складов #{index} должно быть объектом")
        mapping_id = str(item.get("id", "")).strip()
        if not mapping_id:
            raise ConfigurationError(f"Для соответствия складов #{index} не задан id")
        if mapping_id in seen_ids:
            raise ConfigurationError(f"Дублирующийся id соответствия складов: {mapping_id}")
        seen_ids.add(mapping_id)

        raw_codes = item.get("codes")
        if not isinstance(raw_codes, dict):
            raise ConfigurationError(f"Для соответствия складов {mapping_id} не задан объект codes")
        unknown_bases = set(raw_codes) - base_ids
        missing_bases = base_ids - set(raw_codes)
        if unknown_bases or missing_bases:
            details = []
            if missing_bases:
                details.append(f"нет баз: {', '.join(sorted(missing_bases))}")
            if unknown_bases:
                details.append(f"неизвестные базы: {', '.join(sorted(unknown_bases))}")
            raise ConfigurationError(f"Некорректные codes для {mapping_id}: {'; '.join(details)}")

        codes: dict[str, tuple[str, ...]] = {}
        for base_id in sorted(base_ids):
            raw_base_codes = raw_codes[base_id]
            if isinstance(raw_base_codes, str):
                values = [raw_base_codes]
            elif isinstance(raw_base_codes, list):
                values = raw_base_codes
            else:
                raise ConfigurationError(
                    f"Коды складов базы {base_id} в соответствии {mapping_id} должны быть строкой или списком"
                )
            normalized_codes = tuple(dict.fromkeys(
                " ".join(str(code).strip().split())
                for code in values
                if code is not None and str(code).strip()
            ))
            if not normalized_codes:
                raise ConfigurationError(f"Пустой код склада {base_id} в соответствии {mapping_id}")
            repeated = [code for code in normalized_codes if code in seen_codes[base_id]]
            if repeated:
                raise ConfigurationError(
                    f"Код склада {repeated[0]} базы {base_id} используется в нескольких соответствиях"
                )
            seen_codes[base_id].update(normalized_codes)
            codes[base_id] = normalized_codes

        label = " ".join(str(item.get("label") or mapping_id).strip().split())
        result.append(WarehouseMappingConfig(mapping_id=mapping_id, label=label, codes=codes))
    return result


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ConfigurationError("supports_warehouse_filter должен быть true или false")
