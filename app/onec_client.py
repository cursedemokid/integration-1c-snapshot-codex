from __future__ import annotations

import base64
import time
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, Callable

import requests

from app.config import OneCBaseConfig
from app.models import LedgerRow, Warehouse
from app.normalization import NormalizationError, normalize_rows


class OneCClientError(RuntimeError):
    pass


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.2
MAX_BACKOFF_SECONDS = 5.0
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# ChunkedEncodingError не наследуется от ConnectionError: обрыв соединения
# внутри тела ответа приходит отдельным классом. Без него самый частый сбой
# 1С за IIS под нагрузкой — усечённое тело — не повторялся бы.
RETRYABLE_EXCEPTIONS = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class OneCClient:
    def __init__(
        self,
        timeout_seconds: float = 120.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def _send(self, send: "Callable[[], requests.Response]") -> requests.Response:
        """Выполнить запрос, повторяя его при транзитных сбоях 1С.

        Повторяются таймауты, обрывы соединения (включая обрыв внутри тела
        ответа) и статусы 408/429/500/502/503/504. Остальные 4xx и ошибки
        конфигурации детерминированы: повтор лишь задержал бы понятное
        сообщение оператору, поэтому они проходят наружу сразу.

        Последняя попытка выполняется за циклом, поэтому её исключение или
        ответ уходят вызывающему коду без дополнительной обработки.

        Повтор применяется и к ветке POST. Сервис обращается к 1С только за
        выборками и использует POST лишь как способ передать период, поэтому
        повторная отправка безопасна. Для endpoint, изменяющего данные, это
        предположение неверно, и повторы отключаются `ONEC_RETRY_MAX_ATTEMPTS=1`.

        Худшее время запроса — `max_attempts` × `timeout_seconds` плюс сумма
        задержек. При значениях по умолчанию и таймауте 120 с это около 361 с,
        поэтому таймаут обратного прокси должен быть не меньше.
        """
        for attempt in range(self.max_attempts - 1):
            try:
                response = send()
            except RETRYABLE_EXCEPTIONS:
                pass
            else:
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    return response
            time.sleep(min(self.backoff_seconds * (2 ** attempt), MAX_BACKOFF_SECONDS))
        return send()

    def fetch_rows(
        self,
        base: OneCBaseConfig,
        start_date: date,
        end_date: date,
        warehouse: str | None = None,
        warehouse_mapping_id: str = "",
    ) -> list[LedgerRow]:
        try:
            response = self._send(
                lambda: _request_reconciliation_rows(
                    base=base,
                    start_date=start_date,
                    end_date=end_date,
                    warehouse=warehouse,
                    timeout_seconds=self.timeout_seconds,
                )
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            method = base.request_method.strip().upper() or "GET"
            raise OneCClientError(_request_error_message(base, "получить данные", method, exc)) from exc

        try:
            payload: Any = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise OneCClientError(f"База «{base.name}» вернула некорректный JSON") from exc

        try:
            rows = normalize_rows(payload, default_period=_format_period(start_date, end_date))
        except NormalizationError as exc:
            raise OneCClientError(f"Некорректные данные из базы «{base.name}»: {exc}") from exc

        normalized_warehouse = _normalize_text(warehouse or "")
        if normalized_warehouse:
            wrong_codes = sorted({row.warehouse_code for row in rows if row.warehouse_code != normalized_warehouse})
            if wrong_codes:
                rendered = ", ".join(code or "<пустой>" for code in wrong_codes[:5])
                raise OneCClientError(
                    f"База «{base.name}» вернула строки не по складу {normalized_warehouse}: {rendered}"
                )
        if warehouse_mapping_id:
            rows = [replace(row, warehouse_mapping_id=warehouse_mapping_id) for row in rows]
        return rows

    def fetch_warehouses(self, base: OneCBaseConfig) -> list[Warehouse]:
        if not base.warehouses_url:
            raise OneCClientError(f"Для базы «{base.name}» не задан endpoint списка складов")

        try:
            response = self._send(
                lambda: requests.get(
                    base.warehouses_url,
                    headers=_build_auth_headers(base),
                    timeout=self.timeout_seconds,
                )
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise OneCClientError(_request_error_message(base, "получить список складов", "GET", exc)) from exc

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise OneCClientError(f"База «{base.name}» вернула некорректный JSON со списком складов") from exc

        try:
            return normalize_warehouses(payload)
        except ValueError as exc:
            raise OneCClientError(f"Некорректный список складов из базы «{base.name}»: {exc}") from exc


def normalize_warehouses(payload: Any) -> list[Warehouse]:
    records = _extract_warehouse_records(payload)
    result: list[Warehouse] = []
    by_code: dict[str, Warehouse] = {}

    for record in records:
        if not isinstance(record, dict):
            raise ValueError("строка справочника склада должна быть объектом")
        code = _extract_warehouse_field(record, ("code", "id", "Код", "СкладКод"), "код")
        name = _extract_warehouse_field(
            record,
            ("name", "Название", "название", "Склад", "склад", "warehouse"),
            "название",
        )
        group = _extract_optional_warehouse_field(record, ("group", "Группа"))
        accounting_name = _extract_optional_warehouse_field(
            record,
            ("accounting_name", "accountingWarehouse", "СкладБухгалтерия"),
        )
        warehouse = Warehouse(code=code, name=name, group=group, accounting_name=accounting_name)
        existing = by_code.get(code)
        if existing == warehouse:
            continue
        if existing is not None:
            raise ValueError(f"конфликтующие записи с кодом склада: {code}")
        by_code[code] = warehouse
        result.append(warehouse)

    return result


def _extract_warehouse_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "code" in payload and str(payload["code"]) != "200":
            raise ValueError(f"1С вернула код ошибки {payload['code']}")
        for key in ("warehouses", "data", "items", "rows", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("ответ должен быть списком складов или объектом с warehouses/data/items/rows/result")


def _extract_warehouse_field(record: dict[str, Any], aliases: tuple[str, ...], field_name: str) -> str:
    value = _extract_optional_warehouse_field(record, aliases)
    if not value:
        raise ValueError(f"не задано обязательное поле склада: {field_name}")
    return value


def _extract_optional_warehouse_field(record: dict[str, Any], aliases: tuple[str, ...]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for alias in aliases:
        value = record.get(alias)
        if value is None:
            value = lowered.get(alias.lower())
        if value is not None:
            return _normalize_text(value)
    return ""


def _build_reconciliation_params(
    base: OneCBaseConfig,
    start_date: date,
    end_date: date,
    warehouse: str | None = None,
) -> list[tuple[str, str]]:
    params = [
        (base.period_begin_key, _format_request_date(start_date, base.date_format)),
        (base.period_end_key, _format_request_date(end_date, base.date_format)),
    ]
    normalized = _normalize_text(warehouse or "")
    if normalized:
        params.append(("warehouse", normalized))
    return params


def _request_reconciliation_rows(
    base: OneCBaseConfig,
    start_date: date,
    end_date: date,
    warehouse: str | None,
    timeout_seconds: float,
) -> requests.Response:
    if end_date < start_date:
        raise OneCClientError("end_date должен быть больше или равен start_date")
    headers = _build_auth_headers(base)
    params = _build_reconciliation_params(base, start_date, end_date, warehouse)
    selected = next((value for key, value in params[2:] if key == "warehouse"), "")
    if selected and not base.supports_warehouse_filter:
        raise OneCClientError(f"База «{base.name}» не поддерживает фильтр складов")
    method = base.request_method.strip().upper()
    if method not in ("GET", "POST"):
        raise OneCClientError(f"Unsupported request method for base {base.name}: {base.request_method}")
    payload_mode = base.request_payload.strip().lower()
    if payload_mode == "auto":
        payload_mode = "json" if method == "POST" else "query"
    if payload_mode not in ("json", "query"):
        raise OneCClientError(f"Unsupported request payload for base {base.name}: {base.request_payload}")
    request = requests.get if method == "GET" else requests.post
    return request(
        base.url,
        headers=headers,
        timeout=timeout_seconds,
        **({"json": dict(params)} if payload_mode == "json" else {"params": params}),
    )


def _build_auth_headers(base: OneCBaseConfig) -> dict[str, str] | None:
    if not base.username and not base.REDACTED:
        return None
    username = base.username or ""
    REDACTED = base.REDACTED or ""
    token = base64.b64encode(f"{username}:{REDACTED}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _request_error_message(
    base: OneCBaseConfig,
    action: str,
    method: str,
    error: requests.RequestException,
) -> str:
    response = error.response
    if response is not None and response.status_code == 401:
        return (
            f"Не удалось {action} из базы «{base.name}»: 1С отклонила авторизацию. "
            "Проверьте имя пользователя и пароль."
        )
    if response is not None and response.status_code == 403:
        return (
            f"Не удалось {action} из базы «{base.name}»: у пользователя 1С нет права "
            f"на HTTP-метод {method} для этого ресурса. Проверьте роль пользователя и публикацию HTTP-сервиса."
        )
    return f"Не удалось {action} из базы «{base.name}»: {error}"


def _format_request_date(value: date, date_format: str) -> str:
    if date_format == "iso":
        return value.isoformat()
    return value.strftime(date_format)


def _format_period(start_date: date, end_date: date) -> str:
    return f"{start_date:%d.%m.%Y}–{end_date:%d.%m.%Y}"


def _normalize_text(value: Any) -> str:
    return " ".join(str(value).strip().split())
