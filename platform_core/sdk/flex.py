from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Any, ClassVar
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from platform_core.schemas import AssetType, BrokerPosition, InstrumentRef

from .statement import BrokerStatementSnapshot, StatementExecution


class FlexStatementError(RuntimeError):
    pass


class FlexStatementFormatError(FlexStatementError):
    pass


@dataclass(frozen=True, slots=True)
class IBKRFlexConfig:
    token: str = field(repr=False)
    query_id: str
    base_url: str = (
        "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
    )
    version: int = 3
    timeout_seconds: int = 30
    poll_interval_seconds: float = 1.0
    max_poll_attempts: int = 30
    max_response_bytes: int = 20 * 1024 * 1024
    user_agent: str = "us-dual-asset-quant-starter/0.1"
    require_net_liquidation: bool = True

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("IBKR Flex token is required")
        if not self.query_id.strip().isdigit():
            raise ValueError("IBKR Flex query_id must be numeric")
        if self.version != 3:
            raise ValueError("this SDK requires IBKR Flex Web Service version 3")
        if self.timeout_seconds <= 0 or self.max_poll_attempts <= 0:
            raise ValueError("IBKR Flex timeouts and poll attempts must be positive")
        if self.poll_interval_seconds < 1:
            raise ValueError("IBKR Flex polling must respect the one-request-per-second limit")
        if self.max_response_bytes <= 0:
            raise ValueError("IBKR Flex response limit must be positive")
        if not self.base_url.startswith("https://"):
            raise ValueError("IBKR Flex base_url must use HTTPS")


FlexTransport = Callable[[str, dict[str, str], int, int], bytes]


class IBKRFlexStatementProvider:
    """获取并规范化独立的 Activity Flex XML 对账单。"""

    _TRANSIENT_CODES: ClassVar[frozenset[str]] = frozenset(
        {
            "1001",
            "1003",
            "1004",
            "1005",
            "1006",
            "1007",
            "1008",
            "1009",
            "1019",
            "1021",
        }
    )

    def __init__(
        self,
        config: IBKRFlexConfig,
        *,
        transport: FlexTransport | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.config = config
        self._transport = transport or _https_get
        self._sleeper = sleeper

    def fetch(
        self, *, account: str, period_start: datetime, period_end: datetime
    ) -> BrokerStatementSnapshot:
        normalized_account = account.strip()
        if not normalized_account:
            raise ValueError("IBKR Flex account is required")
        start = _as_utc(period_start)
        end = _as_utc(period_end)
        if end <= start:
            raise ValueError("IBKR Flex period_end must be after period_start")
        if end - start > timedelta(days=365):
            raise ValueError("IBKR Flex date override cannot exceed 365 calendar days")
        inclusive_end = (end - timedelta(microseconds=1)).date()
        send_payload = self._request(
            "/SendRequest",
            {
                "t": self.config.token,
                "q": self.config.query_id,
                "v": str(self.config.version),
                "fd": start.date().strftime("%Y%m%d"),
                "td": inclusive_end.strftime("%Y%m%d"),
            },
        )
        reference_code = _successful_reference_code(send_payload)
        self._sleeper(self.config.poll_interval_seconds)
        statement_payload = self._poll_statement(reference_code)
        return parse_flex_statement_xml(
            statement_payload,
            account=normalized_account,
            period_start=start,
            period_end=end,
            require_net_liquidation=self.config.require_net_liquidation,
        )

    def _poll_statement(self, reference_code: str) -> bytes:
        for attempt in range(self.config.max_poll_attempts):
            payload = self._request(
                "/GetStatement",
                {
                    "t": self.config.token,
                    "q": reference_code,
                    "v": str(self.config.version),
                },
            )
            service_error = _service_error(payload)
            if service_error is None:
                return payload
            code, message = service_error
            if code not in self._TRANSIENT_CODES:
                raise FlexStatementError(f"IBKR Flex failed with code {code}: {message}")
            if attempt + 1 >= self.config.max_poll_attempts:
                break
            self._sleeper(self.config.poll_interval_seconds)
        raise FlexStatementError("IBKR Flex statement was not ready before the polling limit")

    def _request(self, path: str, params: dict[str, str]) -> bytes:
        base = self.config.base_url.rstrip("/")
        url = f"{base}{path}?{urlencode(params)}"
        try:
            payload = self._transport(
                url,
                {"User-Agent": self.config.user_agent, "Accept": "application/xml"},
                self.config.timeout_seconds,
                self.config.max_response_bytes,
            )
        except FlexStatementError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不得泄露包含令牌的 URL。
            raise FlexStatementError(
                f"IBKR Flex HTTPS request failed: {type(exc).__name__}"
            ) from None
        if not payload:
            raise FlexStatementError("IBKR Flex returned an empty response")
        if len(payload) > self.config.max_response_bytes:
            raise FlexStatementError("IBKR Flex response exceeded the configured size limit")
        return payload


def parse_flex_statement_xml(
    payload: bytes,
    *,
    account: str,
    period_start: datetime,
    period_end: datetime,
    require_net_liquidation: bool = True,
) -> BrokerStatementSnapshot:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise FlexStatementFormatError("IBKR Flex XML declarations are not permitted")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise FlexStatementFormatError("IBKR Flex response is not valid XML") from exc
    error = _service_error_from_root(root)
    if error is not None:
        raise FlexStatementError(f"IBKR Flex failed with code {error[0]}: {error[1]}")
    statements = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "FlexStatement"
        and (_attribute(element, "accountId", "account") or "") == account
    ]
    if len(statements) != 1:
        raise FlexStatementFormatError(
            f"expected exactly one FlexStatement for account {account}, got {len(statements)}"
        )
    statement = statements[0]
    if not _has_section(statement, "Trades"):
        raise FlexStatementFormatError("Flex query must include the Trades section")
    if not _has_section(statement, "OpenPositions"):
        raise FlexStatementFormatError("Flex query must include the OpenPositions section")
    executions = _parse_executions(statement)
    positions = _parse_positions(statement, account=account)
    net_liquidation = _parse_net_liquidation(statement)
    if require_net_liquidation and net_liquidation is None:
        raise FlexStatementFormatError(
            "Flex query must include base-currency net liquidation"
        )
    return BrokerStatementSnapshot(
        account=account,
        provider="IBKR_FLEX",
        period_start=_as_utc(period_start),
        period_end=_as_utc(period_end),
        executions=executions,
        positions=positions,
        net_liquidation=net_liquidation,
        metadata={
            "query_name": _attribute(root, "queryName"),
            "statement_from_date": _attribute(statement, "fromDate"),
            "statement_to_date": _attribute(statement, "toDate"),
        },
    )


def _parse_executions(statement: ElementTree.Element) -> list[StatementExecution]:
    commissions: dict[str, Decimal | None] = {}
    for trade in statement.iter():
        if _local_name(trade.tag) != "Trade":
            continue
        execution_id = _attribute(trade, "ibExecID", "execID", "executionID")
        if not execution_id:
            raise FlexStatementFormatError(
                "each Flex Trade must include IB ExecID for durable reconciliation"
            )
        raw_commission = _attribute(trade, "ibCommission", "commission")
        commission = (
            abs(_decimal(raw_commission, field="trade commission"))
            if raw_commission
            else None
        )
        if execution_id not in commissions:
            commissions[execution_id] = commission
        elif commission is not None:
            commissions[execution_id] = (commissions[execution_id] or Decimal(0)) + commission
    return [
        StatementExecution(execution_id=execution_id, commission=commission)
        for execution_id, commission in sorted(commissions.items())
    ]


def _parse_positions(
    statement: ElementTree.Element, *, account: str
) -> list[BrokerPosition]:
    aggregate: dict[str, tuple[InstrumentRef, Decimal, Decimal]] = {}
    for row in statement.iter():
        if _local_name(row.tag) != "OpenPosition":
            continue
        row_account = _attribute(row, "accountId", "account") or account
        if row_account != account:
            continue
        quantity = _decimal(
            _required_attribute(row, "position", "quantity"), field="position quantity"
        )
        if quantity == 0:
            continue
        category = (_attribute(row, "assetCategory", "assetClass") or "").upper()
        instrument = _position_instrument(row, category=category)
        avg_cost_raw = _attribute(row, "costBasisPrice", "markPrice")
        avg_cost = (
            _decimal(avg_cost_raw, field="position average cost")
            if avg_cost_raw
            else Decimal(0)
        )
        key = str(instrument.conid) if instrument.conid else instrument.model_dump_json()
        existing = aggregate.get(key)
        if existing is None:
            aggregate[key] = (instrument, quantity, avg_cost)
            continue
        _, previous_quantity, previous_cost = existing
        combined_quantity = previous_quantity + quantity
        if combined_quantity == 0:
            aggregate.pop(key)
            continue
        weighted_cost = (
            (abs(previous_quantity) * previous_cost) + (abs(quantity) * avg_cost)
        ) / (abs(previous_quantity) + abs(quantity))
        aggregate[key] = (instrument, combined_quantity, weighted_cost)
    return [
        BrokerPosition(
            account=account,
            instrument=instrument,
            quantity=quantity,
            avg_cost=avg_cost,
        )
        for _, (instrument, quantity, avg_cost) in sorted(aggregate.items())
    ]


def _position_instrument(
    row: ElementTree.Element, *, category: str
) -> InstrumentRef:
    symbol = _required_attribute(row, "symbol", "underlyingSymbol").upper()
    conid_raw = _required_attribute(row, "conid", "conId")
    try:
        conid = int(conid_raw)
    except ValueError as exc:
        raise FlexStatementFormatError("Flex position conid must be an integer") from exc
    common: dict[str, Any] = {
        "symbol": symbol,
        "conid": conid,
        "currency": _attribute(row, "currency") or "USD",
        "venue": _attribute(row, "listingExchange", "exchange"),
        "metadata": {
            "multiplier": _attribute(row, "multiplier")
            or ("100" if category == "OPT" else "1")
        },
    }
    if category in {"STK", "ETF"}:
        return InstrumentRef(
            asset_type=AssetType.ETF if category == "ETF" else AssetType.EQUITY,
            **common,
        )
    if category == "OPT":
        right = (_required_attribute(row, "putCall", "right")).upper()
        normalized_right = {
            "C": "CALL",
            "P": "PUT",
            "CALL": "CALL",
            "PUT": "PUT",
        }.get(right)
        if normalized_right is None:
            raise FlexStatementFormatError("invalid option right in Flex statement")
        expiry = _flex_date(_required_attribute(row, "expiry", "expiration"))
        return InstrumentRef(
            asset_type=AssetType.OPTION,
            option_right=normalized_right,
            strike=_decimal(_required_attribute(row, "strike"), field="option strike"),
            expiry=expiry,
            **common,
        )
    raise FlexStatementFormatError(
        f"unsupported nonzero Flex position asset category {category or '<missing>'}"
    )


def _parse_net_liquidation(statement: ElementTree.Element) -> Decimal | None:
    preferred: list[Decimal] = []
    fallback: list[Decimal] = []
    for row in statement.iter():
        raw = _attribute(
            row,
            "netLiquidationValue",
            "netLiquidation",
            "endingValue",
            "total",
        )
        if raw is None:
            continue
        name = _local_name(row.tag).lower()
        if "netassetvalue" not in name and "accountinformation" not in name:
            continue
        value = _decimal(raw, field="net liquidation")
        currency = (_attribute(row, "currency", "reportingCurrency") or "").upper()
        if currency in {"BASE", "BASE_SUMMARY", "BASE CURRENCY", ""}:
            preferred.append(value)
        else:
            fallback.append(value)
    if preferred:
        return preferred[-1]
    if len(fallback) == 1:
        return fallback[0]
    return None


def _successful_reference_code(payload: bytes) -> str:
    root = _parse_xml(payload)
    error = _service_error_from_root(root)
    if error is not None:
        raise FlexStatementError(f"IBKR Flex failed with code {error[0]}: {error[1]}")
    status = _child_text(root, "Status")
    reference = _child_text(root, "ReferenceCode")
    if status != "Success" or not reference or not reference.isdigit():
        raise FlexStatementFormatError("IBKR Flex SendRequest response is incomplete")
    return reference


def _service_error(payload: bytes) -> tuple[str, str] | None:
    root = _parse_xml(payload)
    return _service_error_from_root(root)


def _service_error_from_root(
    root: ElementTree.Element,
) -> tuple[str, str] | None:
    if _local_name(root.tag) != "FlexStatementResponse":
        return None
    if _child_text(root, "Status") != "Fail":
        return None
    return (
        _child_text(root, "ErrorCode") or "UNKNOWN",
        _child_text(root, "ErrorMessage") or "unknown Flex service error",
    )


def _parse_xml(payload: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise FlexStatementFormatError("IBKR Flex XML declarations are not permitted")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise FlexStatementFormatError("IBKR Flex response is not valid XML") from exc


def _has_section(statement: ElementTree.Element, name: str) -> bool:
    return any(_local_name(element.tag) == name for element in statement.iter())


def _child_text(root: ElementTree.Element, name: str) -> str | None:
    for child in root.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _attribute(element: ElementTree.Element, *names: str) -> str | None:
    attributes = {key.lower(): value for key, value in element.attrib.items()}
    for name in names:
        value = attributes.get(name.lower())
        if value is not None and value.strip():
            return value.strip()
    return None


def _required_attribute(element: ElementTree.Element, *names: str) -> str:
    value = _attribute(element, *names)
    if value is None:
        raise FlexStatementFormatError(
            f"Flex {_local_name(element.tag)} is missing required field {names[0]}"
        )
    return value


def _decimal(value: str, *, field: str) -> Decimal:
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise FlexStatementFormatError(f"invalid {field} in Flex statement") from exc


def _flex_date(value: str) -> date:
    normalized = value.strip().replace("-", "")
    try:
        return date(
            int(normalized[:4]),
            int(normalized[4:6]),
            int(normalized[6:8]),
        )
    except ValueError as exc:
        raise FlexStatementFormatError("invalid option expiry in Flex statement") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _https_get(
    url: str, headers: dict[str, str], timeout_seconds: int, max_response_bytes: int
) -> bytes:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(max_response_bytes + 1)
    if len(payload) > max_response_bytes:
        raise FlexStatementError("IBKR Flex response exceeded the configured size limit")
    return payload
