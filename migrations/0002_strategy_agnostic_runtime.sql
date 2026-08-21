-- 把现有基础数据库升级到与策略无关的执行结构。
-- 启动相关服务前，使用常规迁移工具执行本文件。

ALTER TABLE orders ADD COLUMN IF NOT EXISTS broker_order_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS runtime_mode TEXT NOT NULL DEFAULT 'PAPER';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS instrument_key TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS conid BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS expiry DATE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS option_right TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS strike NUMERIC(18, 6);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS multiplier NUMERIC(18, 6) NOT NULL DEFAULT 1;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS tif TEXT NOT NULL DEFAULT 'DAY';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
UPDATE orders SET instrument_key = asset_type || ':' || symbol WHERE instrument_key IS NULL;
ALTER TABLE orders ALTER COLUMN instrument_key SET NOT NULL;

ALTER TABLE fills ADD COLUMN IF NOT EXISTS execution_id TEXT;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS instrument_key TEXT;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS conid BIGINT;
UPDATE fills SET execution_id = 'LEGACY-' || fill_id WHERE execution_id IS NULL;
UPDATE fills SET instrument_key = asset_type || ':' || symbol WHERE instrument_key IS NULL;
ALTER TABLE fills ALTER COLUMN execution_id SET NOT NULL;
ALTER TABLE fills ALTER COLUMN instrument_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_execution_id ON fills(execution_id);

ALTER TABLE positions ADD COLUMN IF NOT EXISTS instrument_key TEXT;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS conid BIGINT;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS expiry DATE;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS option_right TEXT;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS strike NUMERIC(18, 6);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS multiplier NUMERIC(18, 6) NOT NULL DEFAULT 1;
UPDATE positions SET instrument_key = asset_type || ':' || symbol WHERE instrument_key IS NULL;
ALTER TABLE positions ALTER COLUMN instrument_key SET NOT NULL;
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_pkey;
ALTER TABLE positions ADD PRIMARY KEY (strategy_code, instrument_key);
