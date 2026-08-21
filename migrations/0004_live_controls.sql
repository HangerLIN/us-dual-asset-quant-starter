-- 为执行 0003_live_execution.sql 后尚未包含策略版本和日终对账的数据库补充升级。

ALTER TABLE broker_account_snapshots
  ADD COLUMN IF NOT EXISTS daily_order_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE broker_account_snapshots
  ADD COLUMN IF NOT EXISTS daily_traded_notional NUMERIC(20, 6) NOT NULL DEFAULT 0;

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
