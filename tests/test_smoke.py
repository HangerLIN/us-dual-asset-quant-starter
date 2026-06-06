from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from examples.dual_asset_momentum import DualAssetMomentumStrategy
from examples.equity_momentum import sample_instrument
from examples.option_momentum import sample_option
from platform_core.execution import QuoteAwareExecutionSelector
from platform_core.risk import BasicRiskEngine
from platform_core.schemas import BarEvent, MarketQuote, RiskCheckRequest
from platform_core.schemas.assets import AssetType
from scripts.run_smoke import main as smoke_main


def test_dual_asset_smoke() -> None:
    now = datetime(2026, 5, 22, 13, 40, tzinfo=timezone.utc)
    bar = BarEvent(
        instrument=sample_instrument("SPY"),
        bar_start=now,
        bar_end=now,
        open=Decimal("500"),
        high=Decimal("503"),
        low=Decimal("499"),
        close=Decimal("502"),
        volume=1_000_000,
        vwap=Decimal("500.5"),
    )
    option = sample_option("SPY")
    strategy = DualAssetMomentumStrategy()
    signals = strategy.process_bar(
        bar,
        features={"vwap": Decimal("500.5"), "rvol": Decimal("2.0")},
        context={"selected_option": option, "option_spread_pct": Decimal("0.04")},
    )
    assert {signal.instrument.asset_type for signal in signals} == {AssetType.ETF, AssetType.OPTION}

    decisions = strategy.construct_portfolio(signals, prices={"SPY": Decimal("502")})
    assert decisions

    risk = BasicRiskEngine(notional_cap=Decimal("100000"))
    selector = QuoteAwareExecutionSelector()
    quote = MarketQuote(instrument=decisions[0].instrument, quote_ts=now, bid=Decimal("501"), ask=Decimal("502"))
    risk_result = risk.evaluate(
        RiskCheckRequest(
            strategy_code=decisions[0].strategy_code,
            instrument=decisions[0].instrument,
            side=decisions[0].side,
            quantity=decisions[0].quantity,
            notional=decisions[0].target_notional,
            timestamp=now,
            quote={"spread_pct": quote.spread_pct},
        )
    )
    assert risk_result.approved
    request = selector.build_request(decisions[0], quote=quote, trace_id="test")
    assert request.limit_price > 0


def test_offline_smoke_cli(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "smoke.db"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_smoke.py",
            "--mode",
            "offline",
            "--db-url",
            f"sqlite:///{db_path}",
            "--symbols",
            "SPY",
        ],
    )
    smoke_main()
    captured = capsys.readouterr()
    assert '"mode": "offline"' in captured.out
    assert '"status": "COMPLETED"' in captured.out
