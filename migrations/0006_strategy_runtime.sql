-- 持久化事件游标和策略状态，用于进程重启后恢复同一运行实例。
CREATE TABLE IF NOT EXISTS strategy_checkpoints (
  checkpoint_id BIGSERIAL PRIMARY KEY,
  strategy_code TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  runtime_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  trading_date DATE,
  event_cursor BIGINT NOT NULL DEFAULT 0,
  last_market_event_at TIMESTAMPTZ,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  heartbeat_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT uq_strategy_checkpoint_identity
    UNIQUE (strategy_code, strategy_version, runtime_id)
);

CREATE INDEX IF NOT EXISTS ix_strategy_checkpoint_heartbeat
  ON strategy_checkpoints(status, heartbeat_at);
