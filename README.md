# US Dual-Asset Quant Starter

Production-shaped starter for US equity, ETF, and option quantitative trading. This repository is intended to be a clean foundation you can copy into a new project and extend with your own strategies, execution policy, and deployment workflow.

It is designed around one idea: live, paper, and backtest paths should share the same core object flow and data contracts, instead of growing into three separate systems that drift apart over time.

## What This Starter Covers

- US asset support for `EQUITY`, `ETF`, and `OPTION`
- Shared schemas for signals, portfolio decisions, execution requests, fills, and order lifecycle events
- Local SQLite-friendly development path
- Real IBKR adapter boundary for bars, quotes, option chain discovery, and option L1 history
- SQLAlchemy models for market data, calibration, backtest runs, metrics, and execution artifacts
- Small but real data-quality checks for equity minute coverage and option quote sanity
- A DB-backed backtest runner that exercises signal -> portfolio -> risk -> execution style flows
- Example strategies for equity-only, option-only, and dual-asset smoke coverage
- Minimal service shells for `md_gw`, `signal_svc`, `risk_svc`, `exec_svc`, `pnl_svc`, and `backtest`

This is a starter platform, not a finished alpha engine. The included strategies are there to prove plumbing, not edge.

## Design Goals

1. Keep the platform boundary clean.
2. Make offline validation cheap and fast.
3. Make the IBKR path real enough to expose integration issues early.
4. Keep core workflows composable so strategy code can sit downstream from the platform.

## Architecture

The intended runtime flow is:

```text
SignalEnvelope
  -> PortfolioDecision
  -> RiskCheckRequest
  -> ExecutionRequest
  -> ExecutionFill
  -> BacktestOrderEvent / live execution state
```

At a high level:

- `platform_core.schemas` defines the shared domain objects
- `platform_core.data` handles ingestion, fixture loading, and quality checks
- `platform_core.features` builds derived features
- `platform_core.portfolio` turns candidates into allocations
- `platform_core.risk` applies basic guards
- `platform_core.execution` handles quote-aware request shaping
- `platform_core.backtest` runs DB-backed simulations
- `platform_core.reporting` summarizes outcomes
- `platform_apps.*` gives you service entry points to extend into a fuller platform

## Repository Layout

```text
.
├── examples/
│   ├── dual_asset_momentum/
│   ├── equity_momentum/
│   └── option_momentum/
├── infra/
│   ├── docker-compose.yml
│   └── prometheus/
├── migrations/
│   └── 0001_baseline.sql
├── platform_apps/
│   ├── backtest/
│   ├── exec_svc/
│   ├── md_gw/
│   ├── pnl_svc/
│   ├── risk_svc/
│   └── signal_svc/
├── platform_core/
│   ├── backtest/
│   ├── calibration/
│   ├── core/
│   ├── data/
│   ├── db/
│   ├── execution/
│   ├── features/
│   ├── infra/
│   ├── plugins/
│   ├── portfolio/
│   ├── reporting/
│   ├── risk/
│   └── schemas/
├── scripts/
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

## Quick Start

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Run the offline smoke

```bash
python scripts/run_smoke.py --mode offline
pytest -q
```

The offline smoke path:

- creates local tables in SQLite
- loads fixture equity and option data
- runs quality checks
- runs a dual-asset DB-backed backtest
- prints a JSON summary report

This is the fastest way to verify the starter is healthy before you wire in real market data or infrastructure.

## Environment Configuration

Copy the example file if you want to run with custom settings:

```bash
cp .env.example .env
```

Important settings include:

- `DATABASE_URL`
- `REDIS_URL`
- `IB_HOST`
- `IB_PORT`
- `IB_CLIENT_ID`
- `IB_ACCOUNT`
- `SMOKE_SYMBOLS`
- `SMOKE_DAYS`
- `OPTION_DTE_MIN`
- `OPTION_DTE_MAX`
- `RISK_NOTIONAL_CAP`
- `OPTION_SPREAD_PCT_MAX`
- `ORDER_TTL_SECONDS`

Defaults are intentionally lightweight for local development. If you do not provide a `.env`, the starter falls back to local defaults, including `sqlite:///./starter.db`.

## Running with IBKR

The IBKR path is meant to validate integration boundaries, not historical options research on expired contracts.

### Prerequisites

- TWS or IB Gateway running and logged in
- API access enabled
- Postgres or Timescale-compatible database available if you do not want SQLite
- Redis available if your extension work needs it

### Install IBKR and Postgres extras

```bash
pip install -e ".[dev,ibkr,postgres]"
```

### Start local infrastructure

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis
```

### Initialize the database

```bash
python scripts/init_db.py
```

### Probe connectivity

```bash
python scripts/probe_ibkr.py --symbol SPY
```

### Run IBKR smoke

```bash
python scripts/run_smoke.py --mode ibkr --symbols SPY --days 1 --track dual
```

In `ibkr` mode the smoke will:

- ingest equity bars
- probe for a recent non-expired option contract
- ingest option chain metadata
- attempt option L1 history
- run quality checks
- fall back to `equity` track if option data is not usable

That fallback behavior is intentional. It keeps the smoke useful even when option coverage is incomplete.

## Common Commands

### Initialize DB

```bash
python scripts/init_db.py
```

### Ingest equity bars

```bash
python scripts/ingest_equity.py --symbols SPY,QQQ --start 2026-05-27 --end 2026-05-27
```

### Ingest option chain

```bash
python scripts/ingest_option_chain.py --symbols SPY --as-of 2026-05-27 --dte-min 7 --dte-max 45
```

### Ingest option L1

```bash
python scripts/ingest_option_l1.py --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### Recompute features

```bash
python scripts/recompute_features.py
```

### Run quality checks

```bash
python scripts/run_quality_check.py --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### Run backtest

```bash
python scripts/run_backtest.py --track dual --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### Run calibration

```bash
python scripts/run_calibration.py
```

## Smoke Modes

There are two intended smoke modes:

### `offline`

- no IBKR required
- uses fixture data
- uses local DB defaults
- best for CI, onboarding, and platform sanity checks

### `ibkr`

- uses the real IBKR adapter
- validates contract discovery and quote plumbing
- useful for catching connectivity and market-data entitlement issues

If you are extending the platform, start every major change by keeping `offline` green.

## Testing

Run the full test suite:

```bash
pytest -q
```

The test suite covers:

- smoke flow
- data pipeline pieces
- calibration persistence
- IBKR adapter boundaries

This is still a starter suite, so add deeper tests as your production rules evolve.

## Development Notes

### Packaging

The project uses `setuptools` with editable installs via `pip install -e`.

### Python Version

- Python `>= 3.11`

### Linting

`ruff` is included in the `dev` dependency group:

```bash
ruff check .
```

### Dependency Groups

- `dev`: test and lint tooling
- `ibkr`: Interactive Brokers API support
- `postgres`: Psycopg support for Postgres or Timescale

## Extending the Starter

Typical ways to evolve this starter:

1. Add your own strategy package under `examples/` first, then move it into a dedicated downstream package.
2. Replace the simple risk rules with strategy-aware and portfolio-aware controls.
3. Add real signal engines and portfolio ranking logic.
4. Expand the DB schema for your execution, audit, and analytics needs.
5. Replace or extend the service shells under `platform_apps/` with your actual runtime services.

## Known Boundaries

- Included strategies are smoke examples, not production alpha.
- Historical data expectations are intentionally modest.
- The starter is shaped for clarity and reuse, not maximum throughput.
- Real deployment, secret management, CI/CD, and production observability still need to be layered on top.
- Historical bars for expired options are not the target of the bundled IBKR smoke flow.

## Why This Exists

Most trading projects rot when research code, live code, and backtest code diverge too early. This repository is meant to give you a cleaner starting point: one platform core, one set of contracts, and one place to extend from.

If you want a strategy-specific repository later, this starter should stay small and stable while that downstream repo carries the faster-moving alpha logic.
