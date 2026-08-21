from __future__ import annotations

from decimal import Decimal

from platform_core.backtest import DBBacktestRunner
from platform_core.data import (
    build_quality_report,
    upsert_equity_bars,
    upsert_option_bars,
    upsert_option_chain,
    upsert_universe,
)
from platform_core.data.fixtures import build_fixture_dataset
from platform_core.db import Base, get_engine, get_session_factory
from tests.support import BuyOnceTestStrategy


def test_fixture_ingest_quality_and_db_backtest(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'starter.db'}"
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    session = get_session_factory(db_url)()
    fixture = build_fixture_dataset(["SPY"])
    try:
        upsert_universe(session, symbols=fixture.symbols)
        upsert_equity_bars(session, fixture.equity_bars)
        upsert_option_chain(session, trade_date=fixture.start.date(), contracts=fixture.option_contracts)
        upsert_option_bars(session, fixture.option_quotes)

        report = build_quality_report(
            session,
            run_key="test",
            symbols=fixture.symbols,
            start=fixture.start,
            end=fixture.end,
            option_spread_pct_max=Decimal("0.10"),
        )
        assert report.ok

        result = DBBacktestRunner(
            session=session,
            strategy=BuyOnceTestStrategy(),
        ).run(
            start=fixture.start,
            end=fixture.end,
            symbols=fixture.symbols,
            track="dual",
        )
        session.commit()
        assert result.status == "COMPLETED"
        assert result.decision_count >= 1
        assert result.trade_count >= 1
        assert result.gross_notional > 0
    finally:
        session.close()
