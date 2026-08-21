from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from _bootstrap import ROOT  # noqa: F401
from _common import json_print
from platform_core.core import get_settings
from platform_core.db import Base
from platform_core.infra.ibkr import IBKRAdapter, IBKRAdapterConfig
from platform_core.schemas import AssetType, BrokerOrderRequest, InstrumentRef
from platform_core.sdk import (
    BracketOrderIntent,
    ComboLegRef,
    DefinedRiskComboSDK,
    DefinedRiskOptionComboIntent,
    EndOfDayReconciliationSDK,
    ExecutionSDK,
    IBKRFlexConfig,
    IBKRFlexStatementProvider,
    IBKRReconciliationSDK,
    LiveOrderIntent,
    LiveRiskGateway,
    LiveRiskPolicy,
    OCAOrderIntentGroup,
    OrderCancelCommand,
    OrderLifecycleState,
    OrderReplaceCommand,
    ReconciliationBlockedError,
    SQLAlchemyOrderLedger,
    SessionSupervisorSDK,
    TradingMode,
    TradingSafetyConfig,
    TradingSafetyController,
)

STRATEGY_CODE = "ibkr-paper-sdk-test"
class PaperAcceptanceSuite:
    """真实 IBKR 模拟盘生命周期验收，订单修改和清理仅通过 SDK 完成。"""

    def __init__(
        self,
        *,
        adapter: IBKRAdapter,
        execution: ExecutionSDK,
        ledger: SQLAlchemyOrderLedger,
        account: str,
        instrument: InstrumentRef,
        min_tick: Decimal,
        reference_price: Decimal,
        quantity: Decimal,
    ) -> None:
        self.adapter = adapter
        self.execution = execution
        self.ledger = ledger
        self.account = account
        self.instrument = instrument
        self.min_tick = min_tick
        self.reference_price = reference_price
        self.quantity = quantity
        self.tracked_order_ids: list[str] = []
        self.results: dict[str, Any] = {}

    def run_what_if(self) -> None:
        intent = self._intent(
            "whatif",
            limit_price=self._price(Decimal("0.50")),
            what_if=True,
        )
        self.results["what_if"] = self.execution.submit(intent).model_dump(mode="json")

    def run_submit_confirm_cancel(self) -> None:
        intent = self._intent("submit-cancel", limit_price=self._price(Decimal("0.50")))
        submitted = self._submit(intent)
        confirmed = self._wait_for(
            intent.client_order_id,
            lambda row: row.broker_order_id is not None
            and row.state
            in {
                OrderLifecycleState.ACKNOWLEDGED.value,
                OrderLifecycleState.PARTIAL_FILL.value,
                OrderLifecycleState.FILLED.value,
            },
        )
        cancelled = self._cancel(intent.client_order_id)
        self.results["submit_confirm_cancel"] = {
            "submitted": submitted.model_dump(mode="json"),
            "confirmed_state": confirmed.state,
            "cancelled": cancelled.model_dump(mode="json"),
        }

    def run_replace(self) -> None:
        intent = self._intent("replace", limit_price=self._price(Decimal("0.50")))
        submitted = self._submit(intent)
        self._wait_for(intent.client_order_id, lambda item: item.broker_order_id is not None)
        replaced = None
        for attempt in range(3):
            row = self.ledger.get(intent.client_order_id)
            assert row is not None
            replacement = BrokerOrderRequest.model_validate(row.request_payload).model_copy(
                update={"limit_price": self._price(Decimal("0.48"))}
            )
            try:
                replaced = self.execution.replace(
                    OrderReplaceCommand(
                        client_order_id=intent.client_order_id,
                        expected_revision=row.revision,
                        request=replacement,
                    )
                )
                break
            except ValueError as exc:
                if attempt == 2 or not any(
                    marker in str(exc)
                    for marker in ("revision conflict", "changed before replacement claim")
                ):
                    raise
                sleep(0.25)
        assert replaced is not None
        cancelled = self._cancel(intent.client_order_id)
        self.results["replace"] = {
            "submitted": submitted.model_dump(mode="json"),
            "replaced": replaced.model_dump(mode="json"),
            "cancelled": cancelled.model_dump(mode="json"),
        }

    def run_bracket(self) -> None:
        entry = self._intent("bracket-entry", limit_price=self._price(Decimal("0.50")))
        take_profit = self._intent(
            "bracket-profit",
            side="SELL",
            limit_price=self._price(Decimal("0.65")),
        )
        stop_loss = self._intent(
            "bracket-stop",
            side="SELL",
            order_type="STP",
            stop_price=self._price(Decimal("0.40")),
        )
        self._track(entry, take_profit, stop_loss)
        submitted = self.execution.submit_bracket(
            BracketOrderIntent(
                entry=entry,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
        )
        cancelled = [
            self._cancel(intent.client_order_id)
            for intent in reversed((entry, take_profit, stop_loss))
        ]
        self.results["bracket"] = {
            "submitted": [item.model_dump(mode="json") for item in submitted],
            "cancelled": [item.model_dump(mode="json") for item in cancelled],
        }

    def run_oca(self) -> None:
        first = self._intent("oca-a", limit_price=self._price(Decimal("0.50")))
        second = self._intent("oca-b", limit_price=self._price(Decimal("0.45")))
        self._track(first, second)
        group = OCAOrderIntentGroup(
            group_id=self._id("oca-group"),
            orders=[first, second],
        )
        submitted = self.execution.submit_oca(group)
        cancelled = [self._cancel(intent.client_order_id) for intent in (first, second)]
        self.results["oca"] = {
            "submitted": [item.model_dump(mode="json") for item in submitted],
            "cancelled": [item.model_dump(mode="json") for item in cancelled],
        }

    def run_combo(self) -> None:
        contracts = self.adapter.option_chain(
            self.instrument.symbol,
            as_of=datetime.now(UTC).date(),
            dte_min=7,
            dte_max=45,
            max_per_side=1,
            max_expiries=1,
            include_quotes=False,
        )
        calls = sorted(
            [item for item in contracts if item.option_right == "CALL"],
            key=lambda item: item.strike or Decimal(0),
        )
        pair = next(
            (
                (left, right)
                for left, right in zip(calls, calls[1:])
                if left.expiry == right.expiry and left.strike != right.strike
            ),
            None,
        )
        if pair is None:
            raise RuntimeError("IBKR option chain did not provide a call vertical pair")
        lower, upper = pair
        assert lower.strike is not None and upper.strike is not None
        width = upper.strike - lower.strike
        combo_id = self._id("combo")
        preliminary = DefinedRiskOptionComboIntent(
            client_order_id=combo_id,
            strategy_code=STRATEGY_CODE,
            legs=[
                ComboLegRef(instrument=lower, action="BUY"),
                ComboLegRef(instrument=upper, action="SELL"),
            ],
            quantity=Decimal(1),
            limit_price=min(Decimal("0.10"), width / Decimal(10)),
            account=self.account,
            transmit=False,
        )
        prepared, _ = DefinedRiskComboSDK(self.execution).prepare(preliminary)
        quote = self.adapter.snapshot_quote(prepared.request.instrument)
        reference = quote.ask or quote.mid or quote.last or quote.bid
        if reference is None or reference <= 0:
            raise RuntimeError(
                "IBKR did not return a positive BAG quote for the combo check: "
                f"source={quote.source} bid={quote.bid} ask={quote.ask} mid={quote.mid}"
            )
        combo_rules = self.adapter.qualify_contract(prepared.request.instrument)
        combo_tick = combo_rules.min_tick
        combo_limit = min(
            max(
                combo_tick,
                (
                    (abs(reference) * Decimal("0.50")) / combo_tick
                ).to_integral_value(rounding=ROUND_FLOOR)
                * combo_tick,
            ),
            width - combo_tick,
        )
        combo = preliminary.model_copy(
            update={"limit_price": combo_limit, "transmit": True}
        )
        self.tracked_order_ids.append(combo_id)
        submitted = DefinedRiskComboSDK(self.execution).submit(combo)
        if submitted.broker_status is None or submitted.state not in {
            OrderLifecycleState.ACKNOWLEDGED,
            OrderLifecycleState.PARTIAL_FILL,
            OrderLifecycleState.FILLED,
        }:
            raise RuntimeError(
                f"IBKR did not acknowledge the PAPER combo order: {submitted.detail}"
            )
        cancelled = self._cancel(combo_id)
        self.results["combo"] = {
            "legs": [lower.model_dump(mode="json"), upper.model_dump(mode="json")],
            "submitted": submitted.model_dump(mode="json"),
            "cancelled": cancelled.model_dump(mode="json"),
        }

    def run_fill_and_commission(self, *, timeout_seconds: float) -> None:
        entry = self._intent("fill-entry", order_type="MKT")
        submitted = self._submit(entry)
        expected_order_refs = {entry.client_order_id}
        row = self._wait_for(
            entry.client_order_id,
            lambda item: item.state == OrderLifecycleState.FILLED.value,
            timeout_seconds=timeout_seconds,
            required=False,
        )
        if row.state != OrderLifecycleState.FILLED.value:
            self._cancel(entry.client_order_id)
        filled = Decimal(str(row.filled))
        exit_result = None
        if filled > 0:
            exit_intent = self._intent(
                "fill-exit",
                side="SELL",
                quantity=filled,
                order_type="MKT",
                reduce_only=True,
            )
            expected_order_refs.add(exit_intent.client_order_id)
            exit_result = self._submit(exit_intent)
            exit_row = self._wait_for(
                exit_intent.client_order_id,
                lambda item: item.state == OrderLifecycleState.FILLED.value,
                timeout_seconds=timeout_seconds,
                required=False,
            )
            if exit_row.state != OrderLifecycleState.FILLED.value:
                self._cancel(exit_intent.client_order_id)
                raise RuntimeError(
                    "paper fill exit did not complete; inspect the paper account immediately"
                )
        commission_ready = self._wait_for_commission(
            expected_order_refs=expected_order_refs,
            timeout_seconds=timeout_seconds,
        )
        self.results["fill_and_commission"] = {
            "submitted": submitted.model_dump(mode="json"),
            "filled_quantity": str(filled),
            "exit": exit_result.model_dump(mode="json") if exit_result else None,
            "commission_callback_received": commission_ready,
        }
        if filled <= 0:
            raise RuntimeError("paper fill did not complete before timeout; rerun during market hours")
        if not commission_ready:
            raise RuntimeError("paper fill completed but commission callback was not observed")

    def run_reconnect(self) -> None:
        observed: list[dict[str, Any]] = []

        def capture(event: Any) -> None:
            if getattr(event.event_type, "value", None) == "CONNECTION":
                observed.append(event.model_dump(mode="json"))

        self.adapter.add_event_handler(capture)
        self.adapter.disconnect()
        supervisor = SessionSupervisorSDK(
            execution=self.execution,
            heartbeat_interval_seconds=2,
            account_snapshot_refresh_seconds=2,
            reconciliation_interval_seconds=60,
        )
        report = supervisor.check_once(account=self.account)
        self.results["reconnect"] = {
            "reconciliation": report.model_dump(mode="json") if report else None,
            "readiness": self.execution.readiness(),
            "connection_events": observed,
        }

    def observe_ibkr_connection_codes(self, *, seconds: float) -> None:
        observed: list[dict[str, Any]] = []

        def capture(event: Any) -> None:
            code = event.payload.get("code")
            if code in {1100, 1101, 1102}:
                observed.append(event.model_dump(mode="json"))

        self.adapter.add_event_handler(capture)
        deadline = monotonic() + seconds
        while monotonic() < deadline and {event["payload"]["code"] for event in observed} != {
            1100,
            1101,
            1102,
        }:
            sleep(min(0.5, deadline - monotonic()))
        self.results["ibkr_110x"] = {
            "observed": observed,
            "complete": {event["payload"]["code"] for event in observed}
            >= {1100, 1101, 1102},
            "note": "1100/1101/1102 require an actual Gateway/network interruption during this window",
        }

    def run_flex(self, *, token: str, query_id: str, timeout: int, poll_attempts: int) -> None:
        if not token or not query_id:
            raise RuntimeError("IB_FLEX_TOKEN and IB_FLEX_QUERY_ID are required for Flex acceptance")
        now = datetime.now(UTC)
        period_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        provider = IBKRFlexStatementProvider(
            IBKRFlexConfig(
                token=token,
                query_id=query_id,
                timeout_seconds=timeout,
                max_poll_attempts=poll_attempts,
            )
        )
        report = EndOfDayReconciliationSDK(
            self.ledger,
            block_on_difference=False,
        ).reconcile_from_provider(
            provider,
            account=self.account,
            period_start=period_start,
            period_end=now + timedelta(seconds=1),
            actor=STRATEGY_CODE,
        )
        self.results["flex_reconciliation"] = report.model_dump(mode="json")

    def cleanup(self) -> None:
        warnings: list[dict[str, str]] = []
        # 清理也必须穿过 ExecutionSDK；绝不绕过 capability 直接调用 Adapter 写入口。
        for client_order_id in reversed(self.tracked_order_ids):
            try:
                self._cancel(client_order_id)
            except Exception as exc:  # noqa: BLE001 - 必须继续撤销其余已跟踪订单。
                warnings.append(
                    {
                        "client_order_id": client_order_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        tracked = set(self.tracked_order_ids)
        remaining = [
            order
            for order in self.adapter.open_orders(all_clients=True)
            if order.account == self.account and order.order_ref in tracked
        ]
        if remaining:
            # 独立执行客户端的兜底清理仍经 ExecutionSDK 的私有能力令牌完成。
            self.execution.kill(
                account=self.account,
                reason="paper acceptance cleanup fallback",
                actor=STRATEGY_CODE,
                include_other_clients=False,
            )
            remaining = [
                order
                for order in self.adapter.open_orders(all_clients=True)
                if order.account == self.account and order.order_ref in tracked
            ]
        if warnings:
            self.results["cleanup_warnings"] = warnings
        if remaining:
            raise RuntimeError(
                "PAPER CLEANUP FAILED; remaining tracked IBKR orders: "
                + ", ".join(str(order.order_id) for order in remaining)
            )

    def _intent(
        self,
        label: str,
        *,
        side: str = "BUY",
        quantity: Decimal | None = None,
        order_type: str = "LMT",
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        what_if: bool = False,
        reduce_only: bool = False,
    ) -> LiveOrderIntent:
        client_order_id = self._id(label)
        return LiveOrderIntent(
            client_order_id=client_order_id,
            strategy_code=STRATEGY_CODE,
            request=BrokerOrderRequest(
                instrument=self.instrument,
                side=side,
                quantity=quantity or self.quantity,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                account=self.account,
                what_if=what_if,
                reduce_only=reduce_only,
            ),
        )

    def _submit(self, intent: LiveOrderIntent):
        self._track(intent)
        return self.execution.submit(intent)

    def _track(self, *intents: LiveOrderIntent) -> None:
        for intent in intents:
            if not intent.request.what_if and intent.client_order_id not in self.tracked_order_ids:
                self.tracked_order_ids.append(intent.client_order_id)

    def _cancel(self, client_order_id: str):
        for attempt in range(3):
            row = self.ledger.get(client_order_id)
            if row is None:
                raise LookupError(f"unknown tracked client order {client_order_id}")
            try:
                return self.execution.cancel(
                    OrderCancelCommand(
                        client_order_id=client_order_id,
                        expected_revision=row.revision,
                    )
                )
            except ValueError as exc:
                if attempt == 2 or "revision conflict" not in str(exc):
                    raise
                sleep(0.1)
        raise RuntimeError("unreachable cancellation retry state")

    def _wait_for(
        self,
        client_order_id: str,
        predicate: Callable[[Any], bool],
        *,
        timeout_seconds: float = 20,
        required: bool = True,
    ):
        deadline = monotonic() + timeout_seconds
        row = self.ledger.get(client_order_id)
        while row is not None and not predicate(row) and monotonic() < deadline:
            sleep(0.1)
            row = self.ledger.get(client_order_id)
        if row is None:
            raise LookupError(client_order_id)
        if required and not predicate(row):
            raise TimeoutError(f"timed out waiting for PAPER order {client_order_id}")
        return row

    def _wait_for_commission(
        self,
        *,
        expected_order_refs: set[str],
        timeout_seconds: float,
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            now = datetime.now(UTC)
            rows = self.ledger.execution_records(
                self.account,
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(minutes=1),
            )
            matched_refs = {
                row.order_ref
                for row in rows
                if row.order_ref in expected_order_refs and row.commission is not None
            }
            if matched_refs == expected_order_refs:
                return True
            sleep(0.2)
        return False

    def _price(self, ratio: Decimal) -> Decimal:
        raw = self.reference_price * ratio
        aligned = (raw / self.min_tick).to_integral_value(rounding=ROUND_FLOOR) * self.min_tick
        return max(aligned, self.min_tick)

    @staticmethod
    def _id(label: str) -> str:
        return f"paper-{label[:20]}-{uuid4().hex[:20]}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run durable ExecutionSDK acceptance checks against an IBKR paper account."
    )
    parser.add_argument("--confirm-paper", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--adopt-existing-positions", action="store_true")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--quantity", type=Decimal, default=Decimal("1"))
    parser.add_argument("--real-time-data", action="store_true")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run replace, Bracket, OCA, Combo, and reconnect checks.",
    )
    parser.add_argument(
        "--combo-only",
        action="store_true",
        help="Run only the real defined-risk Combo submission and cleanup check.",
    )
    parser.add_argument(
        "--fill-only",
        action="store_true",
        help="Run only the real PAPER fill, close, and commission callback check.",
    )
    parser.add_argument(
        "--reconnect-only",
        action="store_true",
        help="Run only the real adapter disconnect, reconnect, and reconciliation check.",
    )
    parser.add_argument(
        "--flex-only",
        action="store_true",
        help="Run only Flex reconciliation against an existing persistent ledger.",
    )
    parser.add_argument(
        "--include-fill",
        action="store_true",
        help="Create and close a real PAPER fill; use only during a liquid session.",
    )
    parser.add_argument("--fill-timeout-seconds", type=float, default=45)
    parser.add_argument(
        "--flex",
        action="store_true",
        help="Fetch and reconcile the configured IBKR Flex statement.",
    )
    parser.add_argument(
        "--observe-110x-seconds",
        type=float,
        default=0,
        help="Wait while an operator interrupts/restores Gateway connectivity.",
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=None,
        help="Optional persistent SQLite ledger for delayed Flex reconciliation.",
    )
    args = parser.parse_args()
    isolated_checks = (
        args.combo_only,
        args.fill_only,
        args.flex_only,
        args.reconnect_only,
    )
    if sum(isolated_checks) > 1:
        raise SystemExit(
            "choose at most one of --combo-only, --fill-only, --flex-only, "
            "--reconnect-only"
        )
    if not args.read_only and not args.confirm_paper:
        raise SystemExit("Refusing to submit: pass --confirm-paper after checking TWS paper mode")
    if args.quantity <= 0:
        raise SystemExit("--quantity must be positive")

    settings = get_settings()
    adapter = IBKRAdapter(
        IBKRAdapterConfig(
            host=settings.ib_host,
            port=settings.ib_port,
            client_id=settings.ib_exec_client_id,
            account=settings.ib_account if settings.ib_account != "DU0000000" else None,
            market_data_type=1 if args.real_time_data else 4,
            request_timeout_seconds=settings.ib_request_timeout_seconds,
            pacing_sleep_seconds=settings.ib_pacing_sleep_seconds,
            minimum_server_version=settings.ib_min_server_version,
        )
    )
    temporary = (
        TemporaryDirectory(prefix="ibkr-execution-sdk-")
        if args.ledger_path is None
        else None
    )
    context = temporary if temporary is not None else nullcontext()
    try:
        adapter.connect()
        account = adapter.require_paper_account()
        safety = TradingSafetyController(
            TradingSafetyConfig(
                mode=TradingMode.READ_ONLY if args.read_only else TradingMode.PAPER,
                allowed_accounts=frozenset({account}),
            )
        )
        with context:
            database_path = args.ledger_path or Path(temporary.name) / "paper-ledger.db"
            database_path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{database_path}",
                connect_args={"check_same_thread": False},
            )
            Base.metadata.create_all(engine)
            ledger = SQLAlchemyOrderLedger(sessionmaker(bind=engine, expire_on_commit=False))
            risk = LiveRiskGateway(
                LiveRiskPolicy(
                    max_order_notional=Decimal("1000000000"),
                    max_symbol_notional=Decimal("1000000000"),
                    max_gross_notional=Decimal("1000000000000"),
                    daily_loss_limit=Decimal("1000000000"),
                    max_daily_traded_notional=Decimal("1000000000000"),
                    max_price_deviation_pct=Decimal("0.60"),
                    max_option_spread_pct=Decimal("0.80"),
                    max_quote_age_seconds=172800,
                    max_account_snapshot_age_seconds=30,
                )
            )
            execution = ExecutionSDK(
                broker=adapter,
                ledger=ledger,
                risk=risk,
                safety=safety,
                reconciliation=IBKRReconciliationSDK(broker=adapter, ledger=ledger),
                # 真实期权链探测可能超过生产默认时限；单进程验收期间仍由同一数据库租约持有者执行。
                execution_lease_ttl_seconds=600,
            )
            try:
                startup = execution.start(account=account)
            except ReconciliationBlockedError as exc:
                codes = {issue.code for issue in exc.report.issues if issue.blocking}
                if codes != {"POSITION_BASELINE_REQUIRED"} or not args.adopt_existing_positions:
                    raise
                startup = execution.adopt_positions(
                    account=account,
                    actor=STRATEGY_CODE,
                    confirmation=f"ADOPT-POSITIONS:{account}",
                )
            if args.read_only:
                json_print(
                    {
                        "account": account,
                        "capabilities": adapter.capabilities(),
                        "startup_reconciliation": startup.model_dump(mode="json"),
                        "readiness": execution.readiness(),
                    }
                )
                execution.release_execution_lease()
                return

            asset_type = (
                AssetType.ETF
                if args.symbol.upper() in {"SPY", "QQQ", "IWM"}
                else AssetType.EQUITY
            )
            qualified = adapter.qualify_contract(
                InstrumentRef(asset_type=asset_type, symbol=args.symbol.upper())
            )
            quote = adapter.snapshot_quote(qualified.instrument)
            reference = quote.bid or quote.last or quote.ask
            if reference is None or reference <= 0:
                raise RuntimeError("IBKR did not return a usable quote for the paper order")
            suite = PaperAcceptanceSuite(
                adapter=adapter,
                execution=execution,
                ledger=ledger,
                account=account,
                instrument=qualified.instrument,
                min_tick=qualified.min_tick,
                reference_price=reference,
                quantity=args.quantity,
            )
            try:
                if args.combo_only:
                    suite.run_combo()
                elif args.fill_only:
                    suite.run_fill_and_commission(timeout_seconds=args.fill_timeout_seconds)
                elif args.flex_only:
                    suite.run_flex(
                        token=settings.ib_flex_token.strip(),
                        query_id=settings.ib_flex_query_id.strip(),
                        timeout=settings.ib_flex_timeout_seconds,
                        poll_attempts=settings.ib_flex_max_poll_attempts,
                    )
                elif args.reconnect_only:
                    suite.run_reconnect()
                else:
                    suite.run_what_if()
                    suite.run_submit_confirm_cancel()
                if args.full and not any(isolated_checks):
                    suite.run_replace()
                    suite.run_bracket()
                    suite.run_oca()
                    suite.run_combo()
                    suite.run_reconnect()
                if args.observe_110x_seconds > 0:
                    suite.observe_ibkr_connection_codes(seconds=args.observe_110x_seconds)
                if args.include_fill and not args.fill_only:
                    suite.run_fill_and_commission(timeout_seconds=args.fill_timeout_seconds)
                if args.flex and not args.flex_only:
                    suite.run_flex(
                        token=settings.ib_flex_token.strip(),
                        query_id=settings.ib_flex_query_id.strip(),
                        timeout=settings.ib_flex_timeout_seconds,
                        poll_attempts=settings.ib_flex_max_poll_attempts,
                    )
            finally:
                try:
                    suite.cleanup()
                    execution.refresh_account_snapshot(account)
                    json_print(
                        {
                            "account": account,
                            "market_data_type": quote.market_data_type,
                            "startup_reconciliation": startup.model_dump(mode="json"),
                            "qualified_contract": qualified.model_dump(mode="json"),
                            "checks": suite.results,
                            "final_readiness": execution.readiness(),
                        }
                    )
                finally:
                    execution.release_execution_lease()
    finally:
        # 此处禁止直接调用 Adapter 修改订单；所有撤单统一由 suite.cleanup 负责。
        adapter.disconnect()


if __name__ == "__main__":
    main()
