from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from platform_core.db.models import Base
from platform_core.schemas import (
    AssetType,
    BrokerExecution,
    BrokerOrderRequest,
    BrokerOrderStatus,
    InstrumentRef,
    MarketQuote,
)
from platform_core.sdk import (
    AccountRiskSnapshot,
    BrokerSessionState,
    ExecutionSDK,
    LiveOrderIntent,
    LiveRiskGateway,
    LiveRiskPolicy,
    SQLAlchemyOrderLedger,
    TradingMode,
    TradingSafetyConfig,
    TradingSafetyController,
)

# FakeBroker 只模拟内存状态，绝不创建 IBKR socket 或持有真实送单能力。


def _ledger() -> tuple[SQLAlchemyOrderLedger, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return SQLAlchemyOrderLedger(factory), factory


def _instrument() -> InstrumentRef:
    return InstrumentRef(asset_type=AssetType.ETF, symbol="SPY", conid=756733)


def _intent(
    client_order_id: str = "client-order-001",
    *,
    quantity: Decimal = Decimal(1),
    limit_price: Decimal = Decimal(100),
    expires_at: datetime | None = None,
) -> LiveOrderIntent:
    return LiveOrderIntent(
        client_order_id=client_order_id,
        strategy_code="test-strategy",
        request=BrokerOrderRequest(
            instrument=_instrument(),
            side="BUY",
            quantity=quantity,
            order_type="LMT",
            limit_price=limit_price,
            account="DU123456",
            order_ref=client_order_id,
        ),
        expires_at=expires_at,
    )


def _policy() -> LiveRiskPolicy:
    return LiveRiskPolicy(
        max_order_notional=Decimal(10000),
        max_symbol_notional=Decimal(20000),
        max_gross_notional=Decimal(50000),
        daily_loss_limit=Decimal(1000),
        max_price_deviation_pct=Decimal("0.20"),
    )


class FakeBroker:
    def __init__(self, *, fail_submission: bool = False) -> None:
        self.session_state = BrokerSessionState.READY
        self.fail_submission = fail_submission
        self.place_calls = 0
        self.bracket_calls = 0
        self.exercise_calls: list[dict] = []
        self.handlers = []
        self._open_orders: list[BrokerOrderStatus] = []
        self._completed_orders: list[BrokerOrderStatus] = []
        self._executions: list[BrokerExecution] = []
        self._positions = []

    def configure_safety_controller(self, safety) -> None:
        self.safety = safety

    def add_event_handler(self, handler) -> None:
        self.handlers.append(handler)

    def connect(self) -> None:
        self.session_state = BrokerSessionState.RECOVERING

    def resolve_account(self, account=None) -> str:
        return account or "DU123456"

    def snapshot_quote(self, instrument) -> MarketQuote:
        return MarketQuote(
            instrument=instrument,
            quote_ts=datetime.now(UTC),
            bid=Decimal(99),
            ask=Decimal(101),
            market_data_type=1,
            halted_status=0,
        )

    def account_risk_snapshot(self, *, account=None) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            account=account or "DU123456",
            captured_at=datetime.now(UTC),
            net_liquidation=Decimal(100000),
            available_funds=Decimal(50000),
            buying_power=Decimal(100000),
            daily_pnl=Decimal(0),
            gross_position_notional=Decimal(1000),
            market_data_type=1,
        )

    def place_order(self, request, *, order_id=None) -> BrokerOrderStatus:
        self.place_calls += 1
        if self.fail_submission:
            raise TimeoutError("simulated acknowledgement loss")
        status = self._status(request, order_id or 100 + self.place_calls)
        self._open_orders = [item for item in self._open_orders if item.order_id != status.order_id]
        self._open_orders.append(status)
        return status

    def replace_order(self, order_id, request, *, expected_permanent_id=None):
        current = next(item for item in self._open_orders if item.order_id == order_id)
        assert current.permanent_id == expected_permanent_id
        status = self._status(request, order_id)
        status = status.model_copy(update={"permanent_id": current.permanent_id})
        self._open_orders = [item for item in self._open_orders if item.order_id != order_id]
        self._open_orders.append(status)
        return status

    def place_bracket(self, *, entry, take_profit, stop_loss):
        self.bracket_calls += 1
        return [
            self._status(entry, 201),
            self._status(take_profit, 202, parent_id=201),
            self._status(stop_loss, 203, parent_id=201),
        ]

    def place_oca(self, requests, *, oca_group, oca_type=1):
        return [self._status(request, 300 + index) for index, request in enumerate(requests)]

    def cancel_order(self, order_id, **kwargs):
        existing = next((item for item in self._open_orders if item.order_id == order_id), None)
        if existing is None:
            raise LookupError(order_id)
        cancelled = existing.model_copy(
            update={"status": "Cancelled", "updated_at": datetime.now(UTC)}
        )
        self._open_orders = [item for item in self._open_orders if item.order_id != order_id]
        self._completed_orders.append(cancelled)
        return cancelled

    def open_orders(self, *, all_clients=False):
        return list(self._open_orders)

    def completed_orders(self, *, api_only=True):
        return list(self._completed_orders)

    def executions(self, **kwargs):
        return list(self._executions)

    def positions(self, *, account=None):
        return list(self._positions)

    def mark_reconciling(self):
        self.session_state = BrokerSessionState.RECONCILING

    def mark_reconciled(self):
        self.session_state = BrokerSessionState.READY

    def mark_degraded(self):
        self.session_state = BrokerSessionState.DEGRADED

    def mark_killed(self):
        self.session_state = BrokerSessionState.KILLED

    def cancel_all_orders(self, **kwargs):
        return []

    def exercise_option(self, **kwargs):
        self.exercise_calls.append(kwargs)
        return 7001

    def heartbeat(self):
        return datetime.now(UTC)

    @staticmethod
    def _status(request, order_id, parent_id=None):
        return BrokerOrderStatus(
            order_id=order_id,
            status="Submitted",
            instrument=request.instrument,
            account=request.account,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            tif=request.tif,
            remaining=request.quantity,
            permanent_id=9000 + order_id,
            client_id=11,
            parent_id=parent_id,
            order_ref=request.order_ref,
            updated_at=datetime.now(UTC),
        )


def _execution_sdk(broker: FakeBroker, ledger: SQLAlchemyOrderLedger) -> ExecutionSDK:
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.PAPER,
            allowed_accounts=frozenset({"DU123456"}),
        )
    )
    sdk = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=safety,
    )
    sdk.start(account="DU123456")
    return sdk
