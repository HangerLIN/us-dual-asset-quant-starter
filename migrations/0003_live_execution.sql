-- 创建安全执行、经纪商回调、成交、账户快照和运行控制所需的持久化结构。
CREATE TABLE IF NOT EXISTS broker_orders (
  order_record_id BIGSERIAL PRIMARY KEY,
  client_order_id TEXT NOT NULL UNIQUE,
  intent_hash TEXT NOT NULL,
  current_request_hash TEXT NOT NULL,
  account TEXT NOT NULL,
  strategy_code TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  conid BIGINT,
  currency TEXT NOT NULL DEFAULT 'USD',
  venue TEXT,
  expiry DATE,
  option_right TEXT,
  strike NUMERIC(18, 6),
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  quantity NUMERIC(18, 6) NOT NULL,
  limit_price NUMERIC(18, 6),
  stop_price NUMERIC(18, 6),
  tif TEXT NOT NULL,
  order_ref TEXT NOT NULL,
  transmit BOOLEAN NOT NULL DEFAULT FALSE,
  what_if BOOLEAN NOT NULL DEFAULT FALSE,
  outside_rth BOOLEAN NOT NULL DEFAULT FALSE,
  good_after_time TIMESTAMPTZ,
  good_till_date TIMESTAMPTZ,
  oca_group TEXT,
  oca_type INTEGER,
  reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
  request_payload JSONB NOT NULL,
  pending_request_hash TEXT,
  pending_request_payload JSONB,
  state TEXT NOT NULL,
  broker_status TEXT,
  broker_order_id BIGINT,
  broker_client_id INTEGER,
  permanent_id BIGINT,
  parent_order_id BIGINT,
  risk_decision_id TEXT,
  submission_attempt_id TEXT,
  submission_started_at TIMESTAMPTZ,
  filled NUMERIC(18, 6) NOT NULL DEFAULT 0,
  remaining NUMERIC(18, 6) NOT NULL,
  avg_fill_price NUMERIC(18, 6) NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  last_event_at TIMESTAMPTZ,
  CONSTRAINT uq_broker_order_identity UNIQUE (account, broker_client_id, broker_order_id)
);
CREATE INDEX IF NOT EXISTS ix_broker_orders_perm_id ON broker_orders(permanent_id);
CREATE INDEX IF NOT EXISTS ix_broker_orders_state ON broker_orders(account, state);

CREATE TABLE IF NOT EXISTS broker_order_events (
  event_id BIGSERIAL PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,
  order_record_id BIGINT REFERENCES broker_orders(order_record_id),
  event_type TEXT NOT NULL,
  event_time TIMESTAMPTZ NOT NULL,
  account TEXT,
  client_order_id TEXT,
  broker_order_id BIGINT,
  permanent_id BIGINT,
  execution_id TEXT,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_broker_events_order_time
  ON broker_order_events(order_record_id, event_time);
CREATE INDEX IF NOT EXISTS ix_broker_events_exec_id ON broker_order_events(execution_id);

CREATE TABLE IF NOT EXISTS broker_executions (
  execution_record_id BIGSERIAL PRIMARY KEY,
  execution_id TEXT NOT NULL UNIQUE,
  execution_root_id TEXT NOT NULL,
  is_correction BOOLEAN NOT NULL DEFAULT FALSE,
  superseded BOOLEAN NOT NULL DEFAULT FALSE,
  order_record_id BIGINT REFERENCES broker_orders(order_record_id),
  broker_order_id BIGINT NOT NULL,
  permanent_id BIGINT,
  broker_client_id INTEGER,
  account TEXT NOT NULL,
  order_ref TEXT,
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  conid BIGINT,
  currency TEXT NOT NULL DEFAULT 'USD',
  venue TEXT,
  expiry DATE,
  option_right TEXT,
  strike NUMERIC(18, 6),
  side TEXT NOT NULL,
  quantity NUMERIC(18, 6) NOT NULL,
  price NUMERIC(18, 6) NOT NULL,
  executed_at TIMESTAMPTZ NOT NULL,
  commission NUMERIC(18, 6),
  commission_currency TEXT,
  realized_pnl NUMERIC(18, 6),
  raw_payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_broker_executions_order ON broker_executions(order_record_id);
CREATE INDEX IF NOT EXISTS ix_broker_executions_account_time
  ON broker_executions(account, executed_at);

CREATE TABLE IF NOT EXISTS broker_positions (
  position_key TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  strategy_code TEXT,
  asset_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  conid BIGINT,
  currency TEXT NOT NULL DEFAULT 'USD',
  venue TEXT,
  expiry DATE,
  option_right TEXT,
  strike NUMERIC(18, 6),
  quantity NUMERIC(18, 6) NOT NULL,
  avg_cost NUMERIC(18, 6) NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_broker_positions_account ON broker_positions(account);

CREATE TABLE IF NOT EXISTS broker_account_snapshots (
  snapshot_id BIGSERIAL PRIMARY KEY,
  account TEXT NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL,
  net_liquidation NUMERIC(20, 6) NOT NULL,
  available_funds NUMERIC(20, 6) NOT NULL,
  buying_power NUMERIC(20, 6) NOT NULL,
  maintenance_margin NUMERIC(20, 6) NOT NULL,
  daily_pnl NUMERIC(20, 6) NOT NULL,
  realized_pnl NUMERIC(20, 6),
  unrealized_pnl NUMERIC(20, 6),
  gross_position_notional NUMERIC(20, 6) NOT NULL,
  open_order_notional NUMERIC(20, 6) NOT NULL,
  daily_order_count INTEGER NOT NULL DEFAULT 0,
  daily_traded_notional NUMERIC(20, 6) NOT NULL DEFAULT 0,
  market_data_type INTEGER,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_account_snapshots_account_time
  ON broker_account_snapshots(account, captured_at);

CREATE TABLE IF NOT EXISTS risk_decisions (
  decision_id TEXT PRIMARY KEY,
  client_order_id TEXT NOT NULL,
  account TEXT NOT NULL,
  approved BOOLEAN NOT NULL,
  code TEXT NOT NULL,
  detail TEXT NOT NULL,
  order_hash TEXT NOT NULL,
  decided_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  computed_notional NUMERIC(20, 6) NOT NULL,
  projected_symbol_notional NUMERIC(20, 6) NOT NULL,
  reasons JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_order
  ON risk_decisions(client_order_id, decided_at);

CREATE TABLE IF NOT EXISTS risk_policy_versions (
  policy_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  policy_payload JSONB NOT NULL,
  proposed_by TEXT NOT NULL,
  approved_by TEXT,
  activated_by TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  approved_at TIMESTAMPTZ,
  activated_at TIMESTAMPTZ,
  CONSTRAINT uq_risk_policy_scope_version UNIQUE (scope, version)
);
CREATE INDEX IF NOT EXISTS ix_risk_policy_active
  ON risk_policy_versions(scope, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_policy_one_active_scope
  ON risk_policy_versions(scope) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS broker_reconciliation_runs (
  reconciliation_id BIGSERIAL PRIMARY KEY,
  account TEXT NOT NULL,
  trigger TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  open_order_count INTEGER NOT NULL DEFAULT 0,
  execution_count INTEGER NOT NULL DEFAULT 0,
  position_count INTEGER NOT NULL DEFAULT 0,
  issues JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS statement_reconciliation_runs (
  statement_reconciliation_id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  provider TEXT NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,
  issues JSONB NOT NULL,
  statement_payload JSONB NOT NULL,
  reconciled_by TEXT NOT NULL,
  reconciled_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_statement_reconciliation_account_time
  ON statement_reconciliation_runs(account, period_end);

CREATE TABLE IF NOT EXISTS trading_controls (
  scope TEXT PRIMARY KEY,
  killed BOOLEAN NOT NULL DEFAULT TRUE,
  reason TEXT,
  changed_by TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_leases (
  account TEXT PRIMARY KEY,
  holder_id TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL,
  renewed_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);
