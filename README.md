# Strategy-Agnostic US Quant Platform Starter

[English](README.md) | [简体中文](README.zh-CN.md)

A strategy-free foundation for researching, paper trading, and live trading US equities,
ETFs, and options. Strategies live in separate Python packages and use one runtime contract
across backtest, IBKR paper, and IBKR live modes.

This repository deliberately contains no alpha, entry rule, exit rule, or example strategy.

## Runtime Flow

```text
IBKR / historical database
  -> MarketQuote / BarEvent
  -> external StrategyPlugin
  -> PortfolioDecision
  -> platform risk gate
  -> ExecutionRequest
  -> durable OrderManager
  -> SimulatedBroker / IBKR Paper / IBKR Live
  -> BrokerOrderUpdate / ExecutionFill
  -> positions, account state, database, event stream
```

The same `TradingEngine`, `OrderManager`, schemas, risk gate, and event types are used in all
three modes. A strategy changes mode through deployment configuration, not strategy code.

## Included Platform Capabilities

- External strategy SDK and `package.module:attribute` loader
- Shared quote, bar, decision, risk, order, fill, account, and position schemas
- Stable option identity using conid or full expiry/right/strike/multiplier fields
- Strategy-agnostic DB replay through the production runtime path
- Deterministic simulated broker for backtest and contract tests
- IBKR market data, contract discovery, paper orders, live orders, cancellation, status,
  executions, commission, account, position, and open-order recovery boundaries
- Durable and idempotent OMS persistence through `client_order_id`
- In-memory event bus for monolith development and Redis Streams publishing for service splits
- Polling live quote feed and quote-to-bar aggregation
- Data ingestion, fixtures, data-quality checks, SQLite development, and Postgres schema
- Live-trading lock requiring both an enable flag and an exact confirmation value

## Repository Boundaries

The platform owns:

- normalized market and execution events
- strategy lifecycle and plugin loading
- account/position context supplied to strategies
- risk approval
- order construction, submission, persistence, recovery, and reconciliation
- backtest, paper, and live runtime wiring

An external strategy package owns:

- indicators and feature state specific to that strategy
- entry and exit decisions
- sizing intent expressed as `PortfolioDecision`
- strategy parameters and versioning

## Quick Start

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ibkr]"
pytest -q
python scripts/run_smoke.py --mode offline
```

The smoke command validates fixture ingestion and data-quality processing without loading or
executing a strategy.

## External Strategy Contract

A separate package can subclass `BaseStrategy` or implement `StrategyPlugin` directly:

```python
from platform_core.strategy import BaseStrategy, StrategyContext


class MyStrategy(BaseStrategy):
    strategy_code = "my-strategy"
    strategy_version = "0.1.0"

    def __init__(self, parameters=None):
        self.parameters = parameters or {}

    def on_bar(self, event, context: StrategyContext):
        # Return zero or more PortfolioDecision objects.
        return []
```

The complete lifecycle also exposes `on_start`, `on_quote`, `on_order_update`, `on_fill`, and `on_stop`.
`StrategyContext` contains the runtime mode, deployment parameters, latest quotes, broker
positions, and account snapshot.

Load paths use:

```text
my_strategy.package:MyStrategy
```

No strategy registration or source-code change is required in this repository.

Create a separate empty strategy package quickly:

```bash
python scripts/scaffold_strategy.py earnings-reversal --output-dir ../quant-strategies
```

The generated package implements the lifecycle contract but contains no trading rule. Install
the platform and generated package in the same environment, then use its import path with the
backtest or runtime commands.

## Backtest an External Strategy

First ingest or load historical data, then run:

```bash
python scripts/run_backtest.py \
  --strategy my_strategy.package:MyStrategy \
  --strategy-params '{"notional": 10000}' \
  --track dual \
  --symbols SPY \
  --start 2026-05-27 \
  --end 2026-05-27
```

The replay uses `TradingEngine -> risk -> OrderManager -> SimulatedBroker`, the same path used
by paper/live after broker selection. Orders, fills, positions, events, PnL, fees, gross
notional, and ending equity are persisted or reported.

## IBKR Paper Runtime

Run TWS or IB Gateway with API access enabled, then configure:

```dotenv
IB_HOST=127.0.0.1
IB_PAPER_PORT=7497
IB_PAPER_ACCOUNT=DU1234567
IB_CLIENT_ID=11
```

Start an external strategy:

```bash
python scripts/run_runtime.py \
  --mode paper \
  --strategy my_strategy.package:MyStrategy \
  --strategy-params '{"notional": 1000}' \
  --symbols SPY \
  --publish-redis
```

Use `--once` to process one snapshot per instrument as a connectivity/runtime probe.

For options or precise contract identity, provide an instrument list:

```bash
python scripts/run_runtime.py \
  --mode paper \
  --strategy my_strategy.package:MyStrategy \
  --instruments-file instruments.json
```

`instruments.json` is a JSON list matching the `InstrumentRef` schema, including option expiry,
right, strike, conid when available, and optional multiplier metadata.

## IBKR Live Runtime and Safety Lock

Live mode is disabled by default. It requires a separate account/port plus two explicit gates:

```dotenv
IB_LIVE_PORT=7496
IB_LIVE_ACCOUNT=U1234567
ALLOW_LIVE_TRADING=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_ARE_REAL
```

Then the command is structurally identical:

```bash
python scripts/run_runtime.py \
  --mode live \
  --strategy my_strategy.package:MyStrategy \
  --symbols SPY
```

Keep paper and live accounts, databases, Redis streams, client IDs, secrets, and deployment
credentials separate in real deployments.

## Data Flow and Services

Every runtime publishes normalized `PlatformEvent` objects such as:

- `BAR_RECEIVED`
- `QUOTE_RECEIVED`
- `PORTFOLIO_DECISION`
- `RISK_REJECTED`
- `ORDER_SUBMITTED`
- `ORDER_UPDATE`
- `FILL`

Local development uses `InMemoryEventBus`. `RedisStreamEventBus` publishes the same event
envelope to `quant:events`, allowing market-data, strategy, risk, execution, and PnL services to
be split later without changing strategy code.

The FastAPI packages under `platform_apps/` remain deployment entry-point shells. The working
single-process reference runtime is `platform_apps.runtime.main` / `scripts/run_runtime.py`.

## Database and Migrations

- `migrations/0001_baseline.sql` creates a fresh schema.
- `migrations/0002_strategy_agnostic_runtime.sql` upgrades the original starter execution tables.
- SQLite development uses SQLAlchemy `create_all`; production Postgres should apply migrations
  before starting paper/live services.

Execution persistence includes runtime mode, account, trace ID, client and broker order IDs,
full instrument identity, order state, fills, fees, and contract-aware positions.

## Development Gates

```bash
pytest -q
ruff check .
python -m compileall -q platform_core platform_apps scripts tests
```

Before promoting a strategy to live, add strategy-package tests for deterministic replay,
out-of-sample validation, paper-session reconciliation, restart recovery, risk rejection,
partial fills, and disconnect behavior. The platform safety lock prevents accidental live
startup; it does not prove a strategy is safe or profitable.

## Known Starter Boundaries

- IBKR integration requires TWS/Gateway and the appropriate market-data permissions.
- Live quotes currently use a replaceable polling feed; high-frequency use should supply a
  streaming adapter behind the same normalized event contracts.
- The backtest broker is intentionally deterministic and basic; advanced slippage, exchange
  queueing, assignment, exercise, margin, and corporate-action models belong in replaceable
  simulation components.
- Production deployment still needs organization-specific secrets management, alerting,
  dashboards, CI/CD, and infrastructure definitions.
