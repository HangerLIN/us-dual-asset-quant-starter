-- 将经纪商已确认的订单参数与进行中的改单参数分开保存。
-- 对账据此判断超时的改单是否实际生效。

ALTER TABLE broker_orders
  ADD COLUMN IF NOT EXISTS pending_request_hash TEXT;

ALTER TABLE broker_orders
  ADD COLUMN IF NOT EXISTS pending_request_payload JSONB;
