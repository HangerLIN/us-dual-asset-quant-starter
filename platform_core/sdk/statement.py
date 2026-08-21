from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from platform_core.schemas import BrokerPosition

from .ledger import SQLAlchemyOrderLedger


class StatementExecution(BaseModel):
    execution_id: str
    commission: Decimal | None = None


class BrokerStatementSnapshot(BaseModel):
    account: str
    provider: str = "IBKR_FLEX"
    period_start: datetime
    period_end: datetime
    executions: list[StatementExecution] = Field(default_factory=list)
    positions: list[BrokerPosition] = Field(default_factory=list)
    net_liquidation: Decimal | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_period(self) -> "BrokerStatementSnapshot":
        if self.period_end <= self.period_start:
            raise ValueError("statement period_end must be after period_start")
        if any(position.account != self.account for position in self.positions):
            raise ValueError("statement positions must belong to the statement account")
        return self


class StatementReconciliationIssue(BaseModel):
    code: str
    detail: str
    payload: dict = Field(default_factory=dict)


class StatementReconciliationReport(BaseModel):
    reconciliation_id: str
    account: str
    provider: str
    period_start: datetime
    period_end: datetime
    reconciled_at: datetime
    issues: list[StatementReconciliationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


class StatementProvider(Protocol):
    def fetch(
        self, *, account: str, period_start: datetime, period_end: datetime
    ) -> BrokerStatementSnapshot: ...


class EndOfDayReconciliationSDK:
    """比较独立经纪商对账单与本地执行账本。"""

    def __init__(
        self,
        ledger: SQLAlchemyOrderLedger,
        *,
        money_tolerance: Decimal = Decimal("0.01"),
        block_on_difference: bool = True,
    ) -> None:
        if money_tolerance < 0:
            raise ValueError("statement reconciliation tolerance cannot be negative")
        self.ledger = ledger
        self.money_tolerance = money_tolerance
        self.block_on_difference = block_on_difference

    def reconcile(
        self,
        statement: BrokerStatementSnapshot,
        *,
        actor: str,
    ) -> StatementReconciliationReport:
        normalized_actor = actor.strip()
        if not normalized_actor:
            raise ValueError("statement reconciliation actor is required")
        issues: list[StatementReconciliationIssue] = []
        local_executions = self.ledger.execution_records(
            statement.account,
            period_start=statement.period_start,
            period_end=statement.period_end,
        )
        statement_executions = {
            execution.execution_id: execution for execution in statement.executions
        }
        local_by_id = {execution.execution_id: execution for execution in local_executions}
        for execution_id in sorted(statement_executions.keys() - local_by_id.keys()):
            issues.append(
                StatementReconciliationIssue(
                    code="STATEMENT_EXECUTION_MISSING_LOCALLY",
                    detail=f"statement execution {execution_id} is absent from the ledger",
                )
            )
        for execution_id in sorted(local_by_id.keys() - statement_executions.keys()):
            issues.append(
                StatementReconciliationIssue(
                    code="LOCAL_EXECUTION_MISSING_FROM_STATEMENT",
                    detail=f"local execution {execution_id} is absent from the statement",
                )
            )
        for execution_id in sorted(local_by_id.keys() & statement_executions.keys()):
            local_commission = local_by_id[execution_id].commission
            statement_commission = statement_executions[execution_id].commission
            if (
                local_commission is not None
                and statement_commission is not None
                and abs(local_commission - statement_commission) > self.money_tolerance
            ):
                issues.append(
                    StatementReconciliationIssue(
                        code="COMMISSION_MISMATCH",
                        detail=f"commission differs for execution {execution_id}",
                        payload={
                            "local": str(local_commission),
                            "statement": str(statement_commission),
                        },
                    )
                )

        local_positions = {
            row.position_key: Decimal(str(row.quantity))
            for row in self.ledger.current_positions(statement.account)
        }
        statement_positions = {
            _position_key(position): position.quantity for position in statement.positions
        }
        for key in sorted(local_positions.keys() | statement_positions.keys()):
            local_quantity = local_positions.get(key, Decimal("0"))
            statement_quantity = statement_positions.get(key, Decimal("0"))
            if local_quantity != statement_quantity:
                issues.append(
                    StatementReconciliationIssue(
                        code="STATEMENT_POSITION_MISMATCH",
                        detail=f"position differs for {key}",
                        payload={
                            "local": str(local_quantity),
                            "statement": str(statement_quantity),
                        },
                    )
                )

        latest = self.ledger.latest_account_snapshot(statement.account)
        if statement.net_liquidation is not None:
            if latest is None:
                issues.append(
                    StatementReconciliationIssue(
                        code="LOCAL_ACCOUNT_SNAPSHOT_MISSING",
                        detail="cannot compare statement net liquidation without a local snapshot",
                    )
                )
            elif (
                abs(latest.net_liquidation - statement.net_liquidation)
                > self.money_tolerance
            ):
                issues.append(
                    StatementReconciliationIssue(
                        code="NET_LIQUIDATION_MISMATCH",
                        detail="statement and local net liquidation differ",
                        payload={
                            "local": str(latest.net_liquidation),
                            "statement": str(statement.net_liquidation),
                        },
                    )
                )

        report = StatementReconciliationReport(
            reconciliation_id=str(uuid4()),
            account=statement.account,
            provider=statement.provider,
            period_start=_as_utc(statement.period_start),
            period_end=_as_utc(statement.period_end),
            reconciled_at=datetime.now(UTC),
            issues=issues,
        )
        self.ledger.record_statement_reconciliation(
            reconciliation_id=report.reconciliation_id,
            account=report.account,
            provider=report.provider,
            period_start=report.period_start,
            period_end=report.period_end,
            ok=report.ok,
            issues=[issue.model_dump(mode="json") for issue in report.issues],
            statement_payload=statement.model_dump(mode="json"),
            actor=normalized_actor,
            reconciled_at=report.reconciled_at,
        )
        if issues and self.block_on_difference:
            self.ledger.set_kill_switch(
                f"account:{statement.account}",
                reason=(
                    f"statement reconciliation {report.reconciliation_id} found "
                    f"{len(issues)} difference(s)"
                ),
                changed_by=normalized_actor,
            )
        return report

    def reconcile_from_provider(
        self,
        provider: StatementProvider,
        *,
        account: str,
        period_start: datetime,
        period_end: datetime,
        actor: str,
    ) -> StatementReconciliationReport:
        statement = provider.fetch(
            account=account,
            period_start=period_start,
            period_end=period_end,
        )
        return self.reconcile(statement, actor=actor)


def _position_key(position: BrokerPosition) -> str:
    instrument = position.instrument
    identity = instrument.conid or ":".join(
        str(value or "")
        for value in (
            instrument.asset_type.value,
            instrument.symbol,
            instrument.currency,
            instrument.venue,
            instrument.expiry,
            instrument.option_right,
            instrument.strike,
        )
    )
    return f"{position.account}:{identity}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
