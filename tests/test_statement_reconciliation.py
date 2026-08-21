from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from platform_core.sdk import (
    BrokerStatementSnapshot,
    EndOfDayReconciliationSDK,
    FlexStatementFormatError,
    IBKRFlexConfig,
    IBKRFlexStatementProvider,
    StatementExecution,
    parse_flex_statement_xml,
)
from tests.support.execution import (
    _ledger,
)


def test_end_of_day_statement_difference_persists_kill_switch() -> None:
    ledger, _ = _ledger()
    now = datetime.now(UTC)
    statement = BrokerStatementSnapshot(
        account="DU123456",
        period_start=now - timedelta(days=1),
        period_end=now + timedelta(seconds=1),
        executions=[StatementExecution(execution_id="missing-execution")],
    )

    report = EndOfDayReconciliationSDK(ledger).reconcile(
        statement,
        actor="eod-operator",
    )

    assert not report.ok
    assert report.issues[0].code == "STATEMENT_EXECUTION_MISSING_LOCALLY"
    assert "statement reconciliation" in ledger.kill_switch_reason("account:DU123456")


def test_ibkr_flex_provider_fetches_and_normalizes_independent_statement() -> None:
    send_response = b"""
    <FlexStatementResponse>
      <Status>Success</Status><ReferenceCode>123456789</ReferenceCode>
    </FlexStatementResponse>
    """
    statement_response = b"""
    <FlexQueryResponse queryName="eod-control">
      <FlexStatements count="1">
        <FlexStatement accountId="DU123456" fromDate="20260820" toDate="20260820">
          <Trades>
            <Trade ibExecID="exec.1" ibCommission="-1.25" />
          </Trades>
          <OpenPositions>
            <OpenPosition accountId="DU123456" assetCategory="STK" symbol="SPY"
              conid="756733" currency="USD" position="2" costBasisPrice="500" />
          </OpenPositions>
          <NetAssetValueByCurrency>
            <NetAssetValueByCurrency currency="BASE_SUMMARY" total="100000.50" />
          </NetAssetValueByCurrency>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """
    responses = iter([send_response, statement_response])
    requests = []
    sleeps = []

    def transport(url, headers, timeout_seconds, max_response_bytes):
        requests.append((url, headers, timeout_seconds, max_response_bytes))
        return next(responses)

    provider = IBKRFlexStatementProvider(
        IBKRFlexConfig(token="secret-token", query_id="12345"),
        transport=transport,
        sleeper=sleeps.append,
    )
    start = datetime(2026, 8, 20, tzinfo=UTC)
    statement = provider.fetch(
        account="DU123456",
        period_start=start,
        period_end=start + timedelta(days=1),
    )

    assert statement.executions[0].execution_id == "exec.1"
    assert statement.executions[0].commission == Decimal("1.25")
    assert statement.positions[0].instrument.conid == 756733
    assert statement.positions[0].quantity == Decimal(2)
    assert statement.net_liquidation == Decimal("100000.50")
    assert "/SendRequest?" in requests[0][0]
    assert "fd=20260820" in requests[0][0]
    assert "/GetStatement?" in requests[1][0]
    assert requests[0][1]["User-Agent"]
    assert sleeps == [1.0]


def test_ibkr_flex_parser_fails_closed_when_required_sections_are_missing() -> None:
    provider = IBKRFlexStatementProvider(
        IBKRFlexConfig(token="secret-token", query_id="12345"),
        transport=lambda *_: b"<not-used />",
        sleeper=lambda _: None,
    )

    with pytest.raises(FlexStatementFormatError, match="Trades"):
        parse_flex_statement_xml(
            b'<FlexQueryResponse><FlexStatement accountId="DU123456" /></FlexQueryResponse>',
            account="DU123456",
            period_start=datetime(2026, 8, 20, tzinfo=UTC),
            period_end=datetime(2026, 8, 21, tzinfo=UTC),
        )

    assert "secret-token" not in repr(provider.config)
