from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from platform_core.schemas import BarEvent, MarketQuote
from platform_core.schemas.assets import AssetType, InstrumentRef


@dataclass(frozen=True, slots=True)
class FixtureDataset:
    symbols: list[str]
    start: datetime
    end: datetime
    equity_bars: list[BarEvent]
    option_contracts: list[InstrumentRef]
    option_quotes: list[MarketQuote]


def build_fixture_dataset(symbols: Sequence[str] | None = None, *, trade_date: date | None = None) -> FixtureDataset:
    selected_symbols = [symbol.upper() for symbol in (symbols or ["SPY"])]
    session_date = trade_date or date(2026, 5, 27)
    start = datetime(session_date.year, session_date.month, session_date.day, 13, 30, tzinfo=UTC)
    end = datetime(session_date.year, session_date.month, session_date.day, 20, 0, tzinfo=UTC)
    equity_bars: list[BarEvent] = []
    option_contracts: list[InstrumentRef] = []
    option_quotes: list[MarketQuote] = []
    for symbol_index, symbol in enumerate(selected_symbols):
        asset_type = AssetType.ETF if symbol in {"SPY", "QQQ", "IWM"} else AssetType.EQUITY
        instrument = InstrumentRef(asset_type=asset_type, symbol=symbol)
        base = Decimal(500) + Decimal(symbol_index * 20)
        running_vwap = Decimal(0)
        running_volume = Decimal(0)
        for minute in range(390):
            ts_end = start + timedelta(minutes=minute)
            open_price = base + Decimal(minute) * Decimal("0.015")
            close_price = open_price + Decimal("0.08")
            high = close_price + Decimal("0.04")
            low = open_price - Decimal("0.04")
            volume = 100_000 + minute * 100
            typical = (high + low + close_price) / Decimal(3)
            running_vwap += typical * Decimal(volume)
            running_volume += Decimal(volume)
            equity_bars.append(
                BarEvent(
                    instrument=instrument,
                    bar_start=ts_end - timedelta(minutes=1),
                    bar_end=ts_end,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=volume,
                    vwap=(running_vwap / running_volume).quantize(Decimal("0.000001")),
                )
            )
        expiry = session_date + timedelta(days=30)
        strike = (base / Decimal(5)).quantize(Decimal(1)) * Decimal(5)
        option = InstrumentRef(
            asset_type=AssetType.OPTION,
            symbol=symbol,
            conid=900000 + symbol_index,
            option_right="CALL",
            strike=strike,
            expiry=expiry,
            metadata={"dte": 30, "source": "fixture"},
        )
        option_contracts.append(option)
        for minute in range(390):
            ts_end = start + timedelta(minutes=minute)
            bid = Decimal("4.90") + Decimal(minute) * Decimal("0.002")
            ask = bid + Decimal("0.20")
            option_quotes.append(
                MarketQuote(
                    instrument=option,
                    quote_ts=ts_end,
                    bid=bid,
                    ask=ask,
                    last=(bid + ask) / Decimal(2),
                    volume=100 + minute,
                    open_interest=1000,
                    source="fixture",
                )
            )
    return FixtureDataset(
        symbols=selected_symbols,
        start=start,
        end=end,
        equity_bars=equity_bars,
        option_contracts=option_contracts,
        option_quotes=option_quotes,
    )
