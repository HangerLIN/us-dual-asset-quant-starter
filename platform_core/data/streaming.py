from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from time import sleep

from platform_core.infra import IBKRAdapter
from platform_core.schemas import BarEvent, InstrumentRef, MarketQuote


class IBKRPollingQuoteFeed:
    """轻量实时报价源，可替换为流式行情适配器。"""

    def __init__(
        self,
        adapter: IBKRAdapter,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.adapter = adapter
        self.poll_interval_seconds = poll_interval_seconds

    def stream(
        self,
        instruments: Sequence[InstrumentRef],
        *,
        stop_event: Event,
        once: bool = False,
    ) -> Iterator[MarketQuote]:
        while not stop_event.is_set():
            for instrument in instruments:
                if stop_event.is_set():
                    break
                yield self.adapter.snapshot_quote(instrument)
            if once:
                return
            sleep(self.poll_interval_seconds)


@dataclass(slots=True)
class _BarState:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class QuoteBarAggregator:
    """把规范化报价聚合为确定性的自然时间 K 线。"""

    def __init__(self, *, timeframe_seconds: int = 60) -> None:
        if timeframe_seconds <= 0:
            raise ValueError("timeframe_seconds must be positive")
        self.timeframe_seconds = timeframe_seconds
        self._states: dict[str, _BarState] = {}
        self._instruments: dict[str, InstrumentRef] = {}

    def update(self, quote: MarketQuote) -> BarEvent | None:
        price = quote.last or quote.mid or quote.bid or quote.ask
        if price is None or price <= 0:
            return None
        key = quote.instrument.key
        bucket_start = _bucket_start(quote.quote_ts, self.timeframe_seconds)
        bucket_end = bucket_start + timedelta(seconds=self.timeframe_seconds)
        state = self._states.get(key)
        completed = None
        if state is not None and bucket_start >= state.end:
            completed = self._to_bar(self._instruments[key], state)
            state = None
        if state is None:
            state = _BarState(
                start=bucket_start,
                end=bucket_end,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=quote.volume or 0,
            )
            self._states[key] = state
            self._instruments[key] = quote.instrument
        else:
            state.high = max(state.high, price)
            state.low = min(state.low, price)
            state.close = price
            state.volume = max(state.volume, quote.volume or 0)
        return completed

    def flush(self) -> list[BarEvent]:
        bars = [
            self._to_bar(self._instruments[key], state)
            for key, state in self._states.items()
        ]
        self._states.clear()
        self._instruments.clear()
        return bars

    def _to_bar(self, instrument: InstrumentRef, state: _BarState) -> BarEvent:
        return BarEvent(
            instrument=instrument,
            bar_start=state.start,
            bar_end=state.end,
            timeframe=f"{self.timeframe_seconds}s",
            open=state.open,
            high=state.high,
            low=state.low,
            close=state.close,
            volume=state.volume,
        )


def _bucket_start(value: datetime, seconds: int) -> datetime:
    normalized = value.astimezone(UTC)
    epoch = int(normalized.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)
