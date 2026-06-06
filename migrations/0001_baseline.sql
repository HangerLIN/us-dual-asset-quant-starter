CREATE TABLE IF NOT EXISTS bars1m_equity (
  symbol TEXT NOT NULL,
  ts_end TIMESTAMPTZ NOT NULL,
  open NUMERIC(18, 6) NOT NULL,
  high NUMERIC(18, 6) NOT NULL,
  low NUMERIC(18, 6) NOT NULL,
  close NUMERIC(18, 6) NOT NULL,
  volume BIGINT NOT NULL DEFAULT 0,
  vwap NUMERIC(18, 6),
  PRIMARY KEY (symbol, ts_end)
);

CREATE TABLE IF NOT EXISTS bars1m_option (
  conid BIGINT NOT NULL,
  ts_end TIMESTAMPTZ NOT NULL,
  underlying_symbol TEXT NOT NULL,
  expiry DATE NOT NULL,
  right TEXT NOT NULL,
  strike NUMERIC(18, 6) NOT NULL,
  bid NUMERIC(18, 6),
  ask NUMERIC(18, 6),
  mid NUMERIC(18, 6),
  last NUMERIC(18, 6),
  volume BIGINT,
  open_interest BIGINT,
  PRIMARY KEY (conid, ts_end)
);

CREATE TABLE IF NOT EXISTS option_chain_meta (
  trade_date DATE NOT NULL,
  conid BIGINT NOT NULL,
  underlying_symbol TEXT NOT NULL,
  expiry DATE NOT NULL,
  right TEXT NOT NULL,
  strike NUMERIC(18, 6) NOT NULL,
  dte INTEGER,
  delta NUMERIC(18, 6),
  bid NUMERIC(18, 6),
  ask NUMERIC(18, 6),
  mid NUMERIC(18, 6),
  open_interest BIGINT,
  volume BIGINT,
  PRIMARY KEY (trade_date, conid)
);

CREATE TABLE IF NOT EXISTS stock_universe (
  universe_code TEXT NOT NULL,
  symbol TEXT NOT NULL,
  asset_type TEXT NOT NULL DEFAULT 'EQUITY',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (universe_code, symbol)
);

CREATE TABLE IF NOT EXISTS dim_trading_calendar (
  trade_date DATE PRIMARY KEY,
  is_trading_day BOOLEAN NOT NULL DEFAULT TRUE,
  session_open TIMESTAMPTZ,
  session_close TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ingestion_progress (
  task_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  last_cursor TEXT,
  failure_reason TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS data_quality_reports (
  report_id BIGSERIAL PRIMARY KEY,
  run_key TEXT NOT NULL,
  check_name TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  symbol TEXT,
  status TEXT NOT NULL,
  expected_rows BIGINT,
  actual_rows BIGINT,
  reason TEXT,
  payload JSONB,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
  order_id BIGSERIAL PRIMARY KEY,
  client_order_id TEXT NOT NULL UNIQUE,
  strategy_code TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity NUMERIC(18, 6) NOT NULL,
  limit_price NUMERIC(18, 6),
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fills (
  fill_id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(order_id),
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity NUMERIC(18, 6) NOT NULL,
  fill_price NUMERIC(18, 6) NOT NULL,
  filled_at TIMESTAMPTZ NOT NULL,
  fees NUMERIC(18, 6) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  strategy_code TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  quantity NUMERIC(18, 6) NOT NULL,
  avg_price NUMERIC(18, 6) NOT NULL,
  mark_price NUMERIC(18, 6) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (strategy_code, asset_type, symbol)
);

CREATE TABLE IF NOT EXISTS risk_limits (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'global'
);

CREATE TABLE IF NOT EXISTS risk_events (
  event_id BIGSERIAL PRIMARY KEY,
  event_ts TIMESTAMPTZ NOT NULL,
  symbol TEXT,
  event_code TEXT NOT NULL,
  payload JSONB
);

CREATE TABLE IF NOT EXISTS bt_runs (
  run_id BIGSERIAL PRIMARY KEY,
  strategy_code TEXT NOT NULL,
  strategy_version TEXT,
  calibration_version TEXT,
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  parameters JSONB
);

CREATE TABLE IF NOT EXISTS bt_order_events (
  event_id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL REFERENCES bt_runs(run_id),
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  event_type TEXT NOT NULL,
  reason_code TEXT,
  event_time TIMESTAMPTZ NOT NULL,
  payload JSONB
);

CREATE TABLE IF NOT EXISTS bt_metrics_total (
  metric_id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL REFERENCES bt_runs(run_id),
  metric_name TEXT NOT NULL,
  metric_value NUMERIC(18, 6) NOT NULL,
  payload JSONB
);

CREATE TABLE IF NOT EXISTS calibration_runs (
  calibration_id BIGSERIAL PRIMARY KEY,
  strategy_code TEXT NOT NULL,
  calibration_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'COMPLETED',
  train_start DATE,
  train_end DATE,
  validation_start DATE,
  validation_end DATE,
  metadata JSONB
);

CREATE TABLE IF NOT EXISTS calibration_params (
  param_id BIGSERIAL PRIMARY KEY,
  calibration_id BIGINT NOT NULL REFERENCES calibration_runs(calibration_id),
  param_name TEXT NOT NULL,
  param_value JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_metrics (
  metric_id BIGSERIAL PRIMARY KEY,
  calibration_id BIGINT NOT NULL REFERENCES calibration_runs(calibration_id),
  metric_name TEXT NOT NULL,
  metric_value NUMERIC(18, 6) NOT NULL
);
