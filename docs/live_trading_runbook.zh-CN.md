# IBKR 实盘运行手册

这份手册只覆盖平台与券商接管。它不授权任何策略上线，也不要求用真实资金下“测试单”。
模拟盘验证订单语义；真实账户先做只读验证，实盘送单只应来自已经审批的策略。

## 1. 部署前硬条件

- TWS/IB Gateway 开启 API，只允许受控主机访问；执行、行情、PnL 使用不同 client ID。
- Postgres 已按编号执行 `0001_baseline.sql` 至 `0006_strategy_runtime.sql`，并有备份、
  恢复演练和时钟同步。
- `IB_MARKET_DATA_TYPE=1`，真实账户具备标的、期权和实时行情权限。
- `TRADING_ALLOWED_ACCOUNTS` 只包含明确账户；禁止通配符。
- 服务放在 TLS 反向代理或 service mesh 后；凭据从密钥管理器注入，不写入仓库或日志。
- Prometheus 指标已接入值班告警；至少覆盖 broker 非 READY、event pipeline failure、kill switch、
  reconciliation blocked、快照过期和 Flex 差异。

## 2. 身份与角色

`LIVE` 不接受 `SERVICE_API_KEYS`。配置 `SERVICE_API_IDENTITIES`，例如：

```dotenv
SERVICE_API_IDENTITIES='{"strategy-router":{"key":"<secret-1>","roles":["read","order_submitter","risk_authorizer"]},"exec-operator":{"key":"<secret-2>","roles":["read","execution_operator"]},"risk-proposer":{"key":"<secret-3>","roles":["read","risk_proposer"]},"risk-approver":{"key":"<secret-4>","roles":["read","risk_approver"]},"risk-operator":{"key":"<secret-5>","roles":["read","risk_operator"]},"eod-reconciler":{"key":"<secret-6>","roles":["read","reconciler"]}}'
```

每个 secret 至少 16 字符且必须唯一。审计 actor 来自身份名称；请求体不能换一个名字冒充。
提议和审批使用不同身份。若需要取消其他 client/TWS 的全部订单，调用身份必须同时具有
`execution_operator` 与 `risk_operator`，并提交 `GLOBAL-CANCEL:<account>` 确认。

## 3. 真实账户只读验收

先以 `TRADING_MODE=READ_ONLY`、`LIVE_TRADING_ENABLED=false` 启动。连接后检查：

1. server version、market data farm、heartbeat；
2. managed account 是否正好命中白名单；
3. open/completed orders、全账户 executions、positions 是否完整；
4. real-time quote 的 bid/ask、halt status 与时间戳；
5. 账户 summary、PnL、逐持仓市值是否可用。

若首次接入已有持仓，`POST /v1/session/start` 会返回
`POSITION_BASELINE_REQUIRED`。人工核对后调用：

```json
{
  "account": "U1234567",
  "confirmation": "ADOPT-POSITIONS:U1234567"
}
```

到 `POST /v1/session/adopt-positions`。身份自动写入审计。不要通过重试绕过该步骤。

## 4. 激活受治理的风险参数

实盘默认要求数据库中存在 `account:<id>` 或 `global` 的 ACTIVE policy；只有环境默认值时，
readiness 的 `risk_policy_ready` 为 false，不能解锁。

1. `risk_proposer` 调用 `POST /v1/policies`；
2. 不同的 `risk_approver` 调用 `POST /v1/policies/{id}/approve`；
3. `risk_operator` 调用 `POST /v1/policies/{id}/activate`，confirmation 为
   `ACTIVATE-RISK-POLICY:<scope>:<version>`；
4. 读取 `GET /v1/policy?account=<id>`，保存 policy ID、version 与 fingerprint。

上线限额应从极小值开始，至少明确：单笔、单标的、全仓、日亏、日订单数、日成交额、行情
时效、价格 collar、期权价差、盘外交易、开空和裸卖期权策略。默认不开盘外、不允许裸卖、
不允许新增股票空头。

## 5. 启动和解锁顺序

把服务重启为：

```dotenv
TRADING_MODE=LIVE
LIVE_TRADING_ENABLED=true
RISK_REQUIRE_ACTIVE_POLICY_FOR_LIVE=true
IB_MARKET_DATA_TYPE=1
IB_RECONCILIATION_INTERVAL_SECONDS=60
```

然后严格按顺序执行：

1. `POST /v1/session/start`：获取 execution lease，连接并对账，刷新账户快照；
2. `GET /readyz`：必须同时满足 broker、DB、lease、账户快照、kill 和 active policy；
3. `POST /v1/session/arm-live`，confirmation 为 `ARM-LIVE:<account>`；
4. 只允许策略路由器调用订单 API。

arm 有时效；断线、数据农场故障、事件落库故障、kill 或重连都会失效。恢复后必须重新对账、
重新检查 readiness、再人工 arm，不会自动恢复送单。
监督线程按配置周期重新抓取开放订单、当日成交和持仓；未知 order/client/perm identity、无法归属
的成交或无法由受管成交解释的仓位变化会持久熔断。券商异常回报会立即唤醒线程，不等待下一次
常规心跳。

## 6. 订单边界

- 每笔订单必须有稳定且唯一的 `client_order_id`，同 ID 不同内容会被拒绝。
- SDK 在 `placeOrder` 前持久化意图、风险决策与 CAS submission claim。
- IBKR 返回不确定或超时时，订单进入 `UNKNOWN`，禁止用新 ID盲目重试；先对账。
- 改单意图与券商已确认条款分开持久化；超时后由全量快照判断改单生效、未生效或条款冲突。
- Bracket/OCA 前序成员均不 transmit，最后一个成员才释放整组。
- BAG 只接受保证型 1:1 两腿期权垂直价差；最大亏损由腿、行权价、乘数和带符号净价重算。
- 单腿期权卖出默认只能 `reduce_only`；股票新增空头默认关闭，并要求 shortable 控制后才可放开。
- 改单使用 revision + 原 broker identity；组合单和 linked order 不允许绕过组工作流改单。

## 7. Kill、平仓与恢复

- 普通 kill 持久化原因并取消本 client 拥有的订单。
- 全局 cancel 会影响人工单和其他 client，必须双角色与精确确认。
- kill 后只允许经过 `LIQUIDATE:<account>` 确认的 reduce-only 紧急平仓。
- `CLEAR-KILL-SWITCH:<account>` 只清除控制记录，不会恢复 READY；必须重新对账。
- 任何 unmanaged order/execution、position mismatch、local order missing 或 statement mismatch
  都按阻断处理，先调查，不直接 adopt。

## 8. 独立日终对账

在 IBKR Client Portal 建立 XML Activity Flex Query，包含：

- Trades：`IB ExecID`、commission；
- OpenPositions：account、asset category、symbol、conid、currency、position、cost basis，期权还需
  expiry、put/call、strike、multiplier；
- base-currency Net Asset Value / net liquidation。

配置 `IB_FLEX_TOKEN`、`IB_FLEX_QUERY_ID` 后，每个结算日调用
`POST /v1/reconciliation/eod/flex`。Flex 获取、XML 解析或必需字段缺失都会失败；成交、佣金、
持仓或 NAV 差异会写入 reconciliation report 并持久触发 kill switch。

## 9. 首次实盘发布标准

发布前应在模拟盘完成：部分成交、拒单、撤改单、断线 1100/1101/1102、行情农场故障、进程
崩溃后启动对账、重复请求、TTL、kill、持仓差异和 Flex 差异演练。真实账户只做上述只读验收。
不需要专门下一笔真实资金“测试单”；首笔实盘订单应当就是经审批、极小限额、有人值守的正式
策略订单，并具备可立即 kill 和对账的回退路径。
