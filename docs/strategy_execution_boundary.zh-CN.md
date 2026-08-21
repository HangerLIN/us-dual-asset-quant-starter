# 策略执行边界与风控门禁

## 进程边界

```text
策略进程
  │  StrategyExecutionClient：只能发送类型化 OrderIntent
  ▼
exec_svc
  │  API key + role + strategies + X-Strategy-Code
  ▼
ExecutionSDK
  │  落账、对账、安全门、风控授权、CAS、限流
  ▼
IBKRAdapter
  │  ExecutionSDK capability + IBKR 订单归属检查
  ▼
IBKR
```

策略进程不持有 IBKR host、port、client ID、账户凭据或订单数据库写权限，也不能在请求中
选择 PAPER/LIVE。模拟盘和实盘使用相同的 `OrderIntent`；模式由 `exec_svc` 的部署配置决定。

## StrategyRuntime 与订单事件

`StrategyRuntime` 把行情回调固定为“特征状态 → 信号 → 组合决策 → 执行选择 →
`LiveOrderIntent`”。注册键由 `strategy_code + version` 组成；同一个策略回调可运行在
BACKTEST、PAPER 或 LIVE，运行模式不会赋予策略直连 IBKR 的能力。
内置 `StrategyRegistry.discover()` 从 `us_dual_asset.strategies` Python entry-point 发现外部
策略包，`StrategyRuntimeConfig` 负责校验策略版本、runtime ID、模式和账户，再由
`StrategyRuntime.from_config()` 加载；无需把新策略硬编码进执行服务。

运行时提供交易日启停、暂停/恢复、心跳、健康状态和 checkpoint。checkpoint 保存特征状态、
最后行情时间、交易日与订单事件游标。`client_order_id` 由策略版本、runtime、模式、账户、
交易日、bar 和组合决策确定性生成；进程重启后相同决策不会随机产生第二笔订单。

策略通过 `GET /v1/order-events` 长轮询持久化 broker journal。事件按递增游标读取，并在
处理成功后才推进 checkpoint，因此断线或进程重启后可以续读部分成交、异步拒单、撤单和
佣金等事件。服务端仍按 API 身份与 `X-Strategy-Code` 过滤，策略 A 看不到策略 B 的事件。

`SERVICE_API_IDENTITIES` 示例：

```json
{
  "strategy-alpha": {
    "key": "由密钥管理器注入的随机密钥",
    "roles": ["read", "order_submitter"],
    "strategies": ["alpha"]
  }
}
```

服务端会拒绝跨策略提交、跨策略改单/撤单、伪造 `X-Strategy-Code` 和伪造
`authenticated_actor`。Bracket 和 OCA 的所有成员必须归属于同一策略。

## 风控策略必须生效

实盘新开仓不接受仅来自环境变量的临时参数，数据库中必须存在 `global` 或
`account:<IBKR账户>` 范围的 ACTIVE 策略：

1. 提议人创建 DRAFT；
2. 不同身份完成 APPROVED；
3. 操作员使用包含 scope/version 的确认字符串激活；
4. 同一 scope 只允许一个 ACTIVE 版本，可回滚到曾独立审批的旧版本；
5. 每次授权记录策略 fingerprint，订单发生变化后旧授权立即失效。

实盘还有不可放宽的硬包络：实时行情类型只能为 1、行情最多 30 秒、账户快照最多
60 秒、授权最多 30 秒，并禁止裸卖期权。

## 白名单、PAPER/LIVE、ARM 与 Kill Switch

- 白名单：订单账户必须在 `TRADING_ALLOWED_ACCOUNTS` 中，同时必须是当前 IBKR 会话实际
  管理的账户。
- READ_ONLY：拒绝所有送单。
- PAPER：只允许 `DU` 前缀账户，不能误发到真实账户。
- LIVE：拒绝 `DU` 账户，同时要求 `LIVE_TRADING_ENABLED=true`。
- ARM：真实送单前必须提交精确的 `ARM-LIVE:<account>`，默认五分钟失效。断线、熔断、
  重启或清除 Kill Switch 后需要重新对账和 ARM。
- Kill Switch：原因持久化到数据库，所有实例都能看到；触发时撤销本执行 client 的挂单并
  阻止新开仓。只有经过单独确认的应急 liquidation 才能发送严格 reduce-only 订单。

## 日亏损、资金与敞口

- `BLOCK:DAILY_LOSS`：账户当日 PnL 小于等于日亏损阈值后停止新开仓。
- `BLOCK:AVAILABLE_FUNDS`：买入最坏名义金额超过可用资金。
- `BLOCK:ORDER_NOTIONAL`：单笔订单超过限额。
- `BLOCK:SYMBOL_NOTIONAL`：成交后单标的净敞口超过限额。
- `BLOCK:GROSS_NOTIONAL`：现有持仓、挂单和新订单的全仓总敞口超过限额。
- `BLOCK:DAILY_ORDER_COUNT`：包含 Bracket/OCA 全成员的当日订单数超过限额。
- `BLOCK:DAILY_TRADED_NOTIONAL`：按最坏成交路径计算的当日成交金额超过限额。

账户、PnL、持仓、挂单来自下单前的 IBKR 快照和本地账本；快照过期或账户不匹配时
fail closed。Reduce-only 会核对具体 conid 的真实数量、方向和最大可平数量，但可以绕过
日亏损和敞口上限，以便安全减仓。

## 价格、行情和交易状态

- `BLOCK:STALE_QUOTE` / `BLOCK:STALE_ACCOUNT`：行情或账户快照过期。
- `BLOCK:NON_LIVE_MARKET_DATA`：实盘不是实时行情类型 1。
- `BLOCK:HALT_STATUS_UNKNOWN` / `BLOCK:TRADING_HALTED`：停牌状态未知或已经停牌。
- `BLOCK:INVALID_NBBO`：没有有效 bid/ask，或 ask 小于 bid。
- `BLOCK:PRICE_COLLAR`：限价/止损价偏离中间价超过策略阈值。
- `BLOCK:OPTION_SPREAD`：期权或组合报价价差过宽。
- `BLOCK:OUTSIDE_RTH`：策略未允许盘前盘后订单。

合约必须由 IBKR 唯一解析，并满足 conid、minTick、sizeIncrement、订单类型和交易时段约束。

## 期权、组合与做空

- 单腿卖出期权只能是严格 reduce-only；新开卖方仓位必须使用定义风险的 BAG。
- 垂直价差必须是同到期日、同类型、1:1、两条腿均有 conid 的保证型组合。
- 风控使用行权价宽度、带符号净权利金和乘数重新计算 `max_loss_per_unit`，不相信策略传入的
  最大亏损数字。
- Bracket 会验证止盈和保护性止损的方向、合约与数量。
- 股票/ETF 开空默认禁止；显式允许后仍要求 IBKR shortable 指标满足阈值。

## 失败语义

订单意图在任何外部调用前先持久化。风险授权绑定账户、client order ID、完整订单哈希和
短有效期。数据库 CAS 成功后才允许调用 IBKR；超时结果记录为 UNKNOWN 并通过开放订单、
成交和持仓对账判定，绝不盲目重发。未知 IBKR 订单、未知成交或事件持久化失败会触发
持久 Kill Switch。
