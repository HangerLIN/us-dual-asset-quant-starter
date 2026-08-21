# US Dual-Asset Quant Starter

面向美股、ETF 和期权的量化交易基础骨架，覆盖离线回测、IBKR 模拟盘和具备强制安全
边界的实盘执行路径。仓库可直接作为新项目底座，并在其上扩展策略、组合构建、执行规则和
部署流程。

它围绕一个核心理念设计：实盘、模拟盘和回测路径应该共享同一套核心对象流和数据契约，而不是随着时间推移演变成三套彼此漂移的系统。

## 这个 Starter 覆盖什么

- 支持美国市场资产类型：`EQUITY`、`ETF`、`OPTION` 和有界风险期权 `COMBO`
- 为信号、组合决策、执行请求、成交和订单生命周期事件提供共享 schema
- 对本地 SQLite 友好的开发路径
- 真实的 IBKR 适配器边界，覆盖 bar、quote、期权链发现和期权 L1 历史数据
- 基于 SQLAlchemy 的模型，覆盖行情数据、校准、回测运行、指标和执行产物
- 小而真实的数据质量检查，包括股票分钟级覆盖率和期权报价合理性
- 基于数据库的回测 runner，用于演练 signal -> portfolio -> risk -> execution 风格的流程
- 策略代码以外部 Python 包接入，仓库本身不内置 Alpha 或交易规则
- 为 `md_gw`、`risk_svc`、`exec_svc`、`pnl_svc` 和 `backtest` 提供服务入口；策略作为隔离客户端运行
- 提供标准 `StrategyRuntime`、策略版本注册与发现、checkpoint、恢复和订单事件续读
- 提供 `StrategyExecutionClient`，使新策略只能提交类型化 `OrderIntent`
- 提供 READ_ONLY/PAPER/LIVE、账户白名单、ARM、持久 Kill Switch 和数据库风控策略

这是一个 starter 平台，不是完整的 alpha 引擎。测试策略只存在于测试支持代码中，用于验证
管道，不提供任何交易优势。

## 设计目标

1. 保持平台边界清晰。
2. 让离线验证便宜且快速。
3. 让 IBKR 路径足够真实，以便尽早暴露集成问题。
4. 保持核心工作流可组合，让策略代码可以位于平台下游。

## 架构

策略运行与执行边界如下：

```text
行情事件
  -> 特征状态
  -> 策略信号
  -> 组合决策
  -> OrderIntent
  -> StrategyExecutionClient（HTTP + 策略身份）
  -> exec_svc（唯一持有 IBKR 连接和订单数据库写权限）
  -> ExecutionSDK（对账、安全门、风控、幂等、租约）
  -> IBKRAdapter（私有 capability）
  -> IBKR
```

订单回报沿相反方向进入持久化 broker journal。策略通过 `/v1/order-events` 使用递增游标读取
部分成交、异步拒单、撤单、成交和佣金事件；游标写入 checkpoint，断线或重启后可以续读。

从高层看：

- `platform_core.schemas` 定义共享领域对象
- `platform_core.data` 处理数据摄取、fixture 加载和质量检查
- `platform_core.features` 构建衍生特征
- `platform_core.portfolio` 将候选标的转换为仓位分配
- `platform_core.risk` 应用基础风控约束
- `platform_core.execution` 处理感知报价的执行请求塑形
- `platform_core.plugins` 提供插件契约、策略注册发现与统一运行时
- `platform_core.sdk` 提供策略客户端、执行、风控、对账、生命周期和 Flex SDK
- `platform_core.backtest` 运行基于数据库的模拟
- `platform_core.reporting` 汇总结果
- `platform_apps.*` 提供可继续扩展成完整平台的服务入口

## 仓库结构

```text
.
├── infra/
│   ├── docker-compose.yml
│   └── prometheus/
├── migrations/
│   ├── 0001_baseline.sql
│   └── 0002...0006 通用运行时、执行安全、风控、改单与策略 checkpoint 迁移
├── platform_apps/
│   ├── backtest/
│   ├── exec_svc/
│   ├── md_gw/
│   ├── pnl_svc/
│   └── risk_svc/
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
│   ├── strategy/
│   ├── runtime/
│   ├── sdk/
│   └── schemas/
├── scripts/
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

## 快速开始

### 1. 创建虚拟环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 运行离线 smoke

```bash
python scripts/run_smoke.py --mode offline
pytest -q
```

离线 smoke 路径会：

- 在 SQLite 中创建本地表
- 加载股票和期权 fixture 数据
- 运行质量检查
- 运行一个基于数据库的双资产回测
- 打印 JSON 格式的汇总报告

这是在接入真实行情数据或基础设施之前，验证 starter 是否健康的最快方式。

## 环境配置

如果你想使用自定义配置，可以复制示例文件：

```bash
cp .env.example .env
```

重要配置项包括：

- `DATABASE_URL`
- `REDIS_URL`
- `IB_HOST`
- `IB_PORT`
- `IB_EXEC_CLIENT_ID`
- `IB_MARKET_DATA_CLIENT_ID`
- `IB_PNL_CLIENT_ID`
- `IB_ACCOUNT`
- `SMOKE_SYMBOLS`
- `SMOKE_DAYS`
- `OPTION_DTE_MIN`
- `OPTION_DTE_MAX`
- `RISK_NOTIONAL_CAP`
- `OPTION_SPREAD_PCT_MAX`
- `ORDER_TTL_SECONDS`

默认配置刻意保持轻量，便于本地开发。如果没有提供 `.env`，starter 会回退到本地默认值，包括 `sqlite:///./starter.db`。

## StrategyRuntime：接入新策略

`StrategyRuntime` 在 BACKTEST、PAPER 和 LIVE 中复用同一组回调：

```text
BarEvent -> FeatureState -> SignalPlugin -> PortfolioConstructor
         -> ExecutionSelectionPlugin -> LiveOrderIntent -> IntentExecutor
```

核心能力：

- `StrategyRegistry` 使用 `strategy_code + version` 注册不可变策略版本；
- `StrategyRegistry.discover()` 从 `us_dual_asset.strategies` Python entry-point 发现外部策略包；
- `StrategyRuntimeConfig` 校验策略、版本、运行实例、模式和账户；
- `MemoryStrategyCheckpointStore` 适合测试，`SQLAlchemyStrategyCheckpointStore` 用于持久运行；
- 支持交易日开始/结束、心跳、健康状态、暂停、恢复和停止；
- `client_order_id` 根据策略版本、运行实例、模式、账户、交易日、bar 和组合决策确定性生成；
- 重启后重复 bar 会跳过，提交中断则依靠相同订单编号由 `ExecutionSDK` 幂等恢复；
- 策略进程不接收 IBKR 地址、client ID、交易模式或数据库写权限。

外部策略包可以在自己的 `pyproject.toml` 中发布工厂：

```toml
[project.entry-points."us_dual_asset.strategies"]
alpha = "my_strategy.runtime:build_pipeline"
```

`build_pipeline()` 必须返回包含 signal、portfolio、execution 和 features 的
`StrategyPipeline`。

仓库还保留 `platform_core.strategy` 的轻量 `BaseStrategy` 与 `package.module:attribute`
加载器，供离线回测和迁移旧策略使用。它对应的 `TradingEngine` 只允许构造
`SimulatedBroker`；PAPER/LIVE 会明确拒绝直接创建 IBKR broker，必须改用上述
`StrategyRuntime + StrategyExecutionClient` 边界。

## 使用 IBKR 运行

IBKR 路径用于验证集成边界，而不是针对已过期期权做历史研究。

### 前置条件

- TWS 或 IB Gateway 已运行且已登录
- 已启用 API 访问
- 如果不想使用 SQLite，需要可用的 Postgres 或 Timescale 兼容数据库
- 如果扩展工作需要 Redis，需要可用的 Redis

### 安装 IBKR 和 Postgres 额外依赖

```bash
pip install -e ".[dev,ibkr,postgres]"
```

### 启动本地基础设施

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis
```

### 初始化数据库

```bash
python scripts/init_db.py
```

### 探测连接

```bash
python scripts/probe_ibkr.py --symbol SPY
```

### 运行 IBKR smoke

```bash
python scripts/run_smoke.py --mode ibkr --symbols SPY --days 1 --track dual
```

在 `ibkr` 模式下，smoke 会：

- 摄取股票 bar
- 探测近期未过期的期权合约
- 摄取期权链元数据
- 尝试获取期权 L1 历史数据
- 运行质量检查
- 如果期权数据不可用，则回退到 `equity` track

这种回退行为是有意设计的。它可以让 smoke 在期权覆盖不完整时依然有用。

### 验证模拟盘订单生命周期

模拟盘订单生命周期统一通过持久化 SDK 验收，包括启动对账、WhatIf、风控、送单和撤单：

```bash
python scripts/test_ibkr_execution_sdk.py \
  --confirm-paper --adopt-existing-positions --symbol SPY --quantity 1
```

也可以只验证连接、对账、账户快照和 readiness，完全不调用下单：

```bash
python scripts/test_ibkr_execution_sdk.py --read-only --adopt-existing-positions
```

完整人工验收可追加 `--full`；单独检查真实断线重连可使用 `--reconnect-only`。会产生真实
模拟成交的 `--fill-only` 必须在流动时段人工监督，并建议配合持久 `--ledger-path`，以便稍后
使用 `--flex-only` 对账。所有送单、改单和清理都经过 `ExecutionSDK`，脚本不会绕过
capability 直接调用 Adapter 写入口。1100/1101/1102 只有在观察窗口内真实中断并恢复
Gateway/网络才算通过。

推荐把成交验收账本持久化，以便稍后执行 Flex 对账：

```bash
python scripts/test_ibkr_execution_sdk.py \
  --confirm-paper --adopt-existing-positions --fill-only \
  --symbol F --quantity 1 --ledger-path /tmp/paper-acceptance.db

python scripts/test_ibkr_execution_sdk.py \
  --confirm-paper --adopt-existing-positions --flex-only \
  --ledger-path /tmp/paper-acceptance.db
```

默认 `pytest` 永不连接 TWS、IB Gateway 或外部网络；真实模拟盘验收始终是显式人工操作。

### 实盘 SDK 与安全门禁

生产送单统一从 `platform_core.sdk.ExecutionSDK` 进入。底层 `IBKRAdapter.place_order()`
默认只允许 `DU` 模拟账户；真实账户如果没有显式注入 `TradingSafetyController` 会被拒绝。
执行 SDK 在送单前依次要求：

- `client_order_id` 幂等意图已先写入持久化账本；
- IBKR 会话完成开放订单、当日成交和持仓三方对账并处于 `READY`；
- 既有持仓必须由操作员审核后用 `ADOPT-POSITIONS:<account>` 显式接管，重试不会自动
  把差异变成基线；
- 账户在 `TRADING_ALLOWED_ACCOUNTS` 白名单；
- 账户/PnL 与行情快照未过期，实盘行情类型必须为 real-time (`1`)；
- 日亏、购买力、单笔、单标的、全仓敞口、价差和价格 collar 全部通过；
- 合约已由 IBKR 唯一解析到 conid，价格和数量符合 minTick/marketRule/sizeIncrement；
- 数据库中存在已经独立审批并激活的账户级或全局风险策略；
- 实盘同时设置 `TRADING_MODE=LIVE`、`LIVE_TRADING_ENABLED=true`，并通过
  `ARM-LIVE:<account>` 做有时效的运行时解锁。

建议先使用以下模拟盘配置：

```dotenv
TRADING_MODE=PAPER
TRADING_ALLOWED_ACCOUNTS=DU1234567
LIVE_TRADING_ENABLED=false
IB_MARKET_DATA_TYPE=1
SERVICE_API_KEYS=paper-only-random-secret
```

真实账户不是简单把端口或 account 参数换掉。完成模拟盘故障测试后，还需显式改为：

```dotenv
TRADING_MODE=LIVE
TRADING_ALLOWED_ACCOUNTS=U1234567
LIVE_TRADING_ENABLED=true
```

然后按顺序调用带 `X-API-Key` 的 `POST /v1/session/start` 完成对账，再调用
`POST /v1/session/arm-live`。重连、清除 kill switch 后都必须重新对账和解锁。

执行、行情、PnL 进程分别使用 `IB_EXEC_CLIENT_ID`、`IB_MARKET_DATA_CLIENT_ID` 和
`IB_PNL_CLIENT_ID`，三者必须不同。实盘模式不接受旧式共享 key，必须配置
`SERVICE_API_IDENTITIES`：它把每个密钥绑定到不可冒充的 actor 和明确角色：`read`、
`order_submitter`、`execution_operator`、`risk_authorizer`、`risk_proposer`、
`risk_approver`、`risk_operator`、`reconciler`。请求体中的 actor（如保留）必须与认证身份
一致；风险参数提议者不能审批自己的改动。

策略进程不应导入 `IBKRAdapter` 或内部 Trading Runtime，只使用
`StrategyExecutionClient` 向 `exec_svc` 提交类型化 `OrderIntent`。每个带
`order_submitter` 角色的实盘身份必须配置显式 `strategies` 列表，服务端会同时核对
API key、`X-Strategy-Code` 和请求体中的 `strategy_code`。改单、撤单和订单查询也会回查
账本中的策略归属，防止一个策略操作另一个策略的订单。

```python
from platform_core.sdk import StrategyExecutionClient, StrategyExecutionClientConfig

client = StrategyExecutionClient(
    strategy_code="alpha",
    config=StrategyExecutionClientConfig(
        base_url="https://exec.internal",
        api_key="从密钥管理器注入的策略专用密钥",
    ),
)
result = client.place(order_request, client_order_id="alpha-20260821-0001")
```

非回环地址强制 HTTPS；客户端没有 PAPER/LIVE 参数，也没有 IBKR host、port、client ID
或凭据。运行模式只由执行服务决定。行情和 PnL Runtime 被强制为 READ_ONLY，风险服务使用
完全不连接 IBKR 的数据库 Runtime；只有 `exec_svc` 构造可下单 Runtime。已配置执行边界后，
直接调用 `runtime.broker.place_order()` 也会被适配器拒绝。

SDK 还提供：

- `ExecutionSDK` + `SQLAlchemyOrderLedger`：幂等意图、broker order/client/perm ID、版本化改撤、
  append-only 回报、execId、佣金和成交更正；
- `IBKRReconciliationSDK` + `SessionSupervisorSDK`：启动/重连三方对账、显式持仓接管、
  单写实例租约、心跳、重订阅、账户快照刷新、周期全量对账和自动 TTL；
- `LiveRiskGateway` + `RiskLimitControlSDK` + `OrderPacingSDK`：账户级 PnL/保证金/敞口、
  日订单数和日成交额、不可变策略指纹、四眼审批/激活/回滚、消息速率与 OER；
- `ContractRulesSDK` + `DefinedRiskComboSDK`：conid/minTick/marketRule/交易时段，以及由两条
  期权腿和带符号 BAG 净价重新计算最大亏损的 1:1 保证型垂直价差；默认拒绝裸卖期权；
- `TradingSafetyController` + `OrderSupervisorSDK` + `OptionLifecycleSDK`：静态门闩、短时实盘
  解锁、持久 kill switch、owned/global cancel 二次确认、reduce-only 平仓、exercise/lapse；
- `IBKRFlexStatementProvider` + `EndOfDayReconciliationSDK`：独立 Flex 下载和标准化，并核对
  成交、佣金、持仓和 NAV，出现差异即持久停机。

实盘行情风控使用短时 streaming quote，要求当前 bid/ask、real-time 数据类型、明确且未停牌
状态；开空时还要求 IBKR shortable 信号。Bracket 的父单和第一条子单使用
`transmit=false`，最后一条保护子单释放整组；OCA 成员是独立订单，每一条都发送，由
`ocaGroup/ocaType` 保证任一成交或撤销后联动处理其余成员。

同一账户还使用数据库 execution lease 保证只有一个执行实例能够连接和送单；每次真正
调用 `placeOrder` 前另有数据库 compare-and-set submission claim，避免多进程重复订单。
订单回报还必须同时匹配 account、clientId 与 orderRef/permId/orderId；未知开放订单或成交会
立即持久熔断并唤醒监督线程撤销本 client 的挂单。周期由
`IB_RECONCILIATION_INTERVAL_SECONDS` 控制（默认 60 秒）。

数据库升级 SQL 从 `migrations/0002_strategy_agnostic_runtime.sql` 到
`migrations/0006_strategy_runtime.sql`。本地空库可以继续运行 `python scripts/init_db.py`；
已有 Postgres 数据库应按编号顺序执行迁移。

风控门禁和策略执行隔离的逐项说明见
[`docs/strategy_execution_boundary.zh-CN.md`](docs/strategy_execution_boundary.zh-CN.md)。

### 独立日终对账

在 Client Portal 创建 XML Activity Flex Query，至少包含：`Trades`（IB ExecID、commission）、
`OpenPositions`（conid、合约字段、数量、成本）以及基准币种 NAV。把 `IB_FLEX_TOKEN` 和
`IB_FLEX_QUERY_ID` 从密钥管理器注入环境后，调用 `POST /v1/reconciliation/eod/flex`，传入
左闭右开的 UTC 时间区间。Provider 使用 Flex v3 两阶段获取、User-Agent、响应大小上限、
安全 XML 解析和一秒轮询；任何必需 section 缺失都会 fail closed。

模拟盘和实盘复用同一 SDK，但实盘绝不是只换端口或账户参数：账户交易权限、实时行情订阅、
借券、停牌、交易时段、TWS/Gateway 每日维护以及告警值班都不同。应先做真实账户只读监控，
不需要为了证明连接而下实盘测试单。具体步骤见
[实盘运行手册](docs/live_trading_runbook.zh-CN.md)。

## 执行服务接口

所有敏感接口都要求 `X-API-Key`；策略订单接口还要求与凭据授权一致的
`X-Strategy-Code`。

| 接口 | 用途 | 最低角色 |
| --- | --- | --- |
| `POST /v1/orders` | 提交单笔订单意图 | `order_submitter` |
| `POST /v1/orders/bracket` | 提交 Bracket | `order_submitter` |
| `POST /v1/orders/oca` | 提交 OCA | `order_submitter` |
| `POST /v1/orders/combo` | 提交定义风险期权组合 | `order_submitter` |
| `POST /v1/orders/replace` | 乐观版本改单 | `order_submitter` |
| `POST /v1/orders/cancel` | 乐观版本撤单 | `order_submitter` |
| `GET /v1/orders/{id}` | 查询本策略订单 | `read` |
| `GET /v1/order-events` | 按游标读取本策略事件 | `read` |
| `POST /v1/session/start` | 连接、对账并启动监督器 | `execution_operator` |
| `POST /v1/session/arm-live` | 短时实盘授权 | `execution_operator` |
| `POST /v1/kill-switch` | 持久熔断与撤单 | `execution_operator` |
| `POST /v1/kill-switch/clear` | 清除熔断并强制重新对账 | `execution_operator` |
| `POST pnl_svc:/v1/reconciliation/eod/flex` | 独立 Flex 对账 | `reconciler` |

服务端从已认证凭据生成 actor，调用方不能在请求体中伪造审计身份。Bracket、OCA 和 Combo
会整体校验策略归属，不能混入其他策略订单。

## 常用命令

### 初始化数据库

```bash
python scripts/init_db.py
```

### 摄取股票 bar

```bash
python scripts/ingest_equity.py --symbols SPY,QQQ --start 2026-05-27 --end 2026-05-27
```

### 摄取期权链

```bash
python scripts/ingest_option_chain.py --symbols SPY --as-of 2026-05-27 --dte-min 7 --dte-max 45
```

### 摄取期权 L1

```bash
python scripts/ingest_option_l1.py --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### 重新计算特征

```bash
python scripts/recompute_features.py
```

### 运行质量检查

```bash
python scripts/run_quality_check.py --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### 运行回测

```bash
python scripts/run_backtest.py --track dual --symbols SPY --start 2026-05-27 --end 2026-05-27
```

### 运行校准

```bash
python scripts/run_calibration.py
```

### 本地启动服务

```bash
uvicorn platform_apps.md_gw.main:app --host 127.0.0.1 --port 8101
uvicorn platform_apps.risk_svc.main:app --host 127.0.0.1 --port 8102
uvicorn platform_apps.exec_svc.main:app --host 127.0.0.1 --port 8103
uvicorn platform_apps.pnl_svc.main:app --host 127.0.0.1 --port 8104
```

这些命令适合本地开发。生产环境还需要 TLS、服务身份注入、独立数据库角色、进程监督和
健康检查编排；不要直接把开发用 Uvicorn 命令当作实盘部署方案。

## Smoke 模式

项目设计了两种 smoke 模式：

### `offline`

- 不需要 IBKR
- 使用 fixture 数据
- 使用本地数据库默认配置
- 最适合 CI、入门上手和平台健康检查

### `ibkr`

- 使用真实 IBKR 适配器
- 验证合约发现和报价管道
- 适合捕捉连接和行情数据权限问题

如果你在扩展平台，每次做较大改动时都应该先保持 `offline` 通过。

## Docker 的定位

当前 [`infra/docker-compose.yml`](infra/docker-compose.yml) 只提供 Postgres、Redis、
Prometheus 和 Grafana，适合本地依赖启动。它暂时不包含 exec、md、pnl、risk 服务镜像，
也不包含 TLS 代理、密钥管理、自动迁移、备份、TWS/IB Gateway 监督或 CI/CD。

Docker 不是 SDK、回测或本地 TWS 连接的前置条件。生产中使用容器的主要价值是固定依赖、
隔离服务、提供健康检查和可复现发布；它不会自动解决 TLS、最小权限、密钥轮换或交易安全。

## 测试

运行完整测试套件：

```bash
pytest -q
```

测试套件覆盖：

- smoke、行情日历、数据管道和校准持久化；
- SDK 客户端与真实进程内 ASGI 路由；
- 身份、角色、策略归属和跨策略隔离；
- READ_ONLY/PAPER/LIVE、ARM、Kill Switch 和清除后的重新授权；
- 日亏损、资金、单笔/单标的/总敞口、频率、成交额、价格和期权风险边界；
- 幂等、租约、送单、改单、撤单、Bracket、OCA、Combo、事件和恢复；
- EOD、Flex、成交与佣金对账；
- IBKRAdapter 所有写入口的 ExecutionSDK capability 限制；
- StrategyRuntime 版本发现、模式复用、checkpoint、确定性订单编号和事件游标。

测试护栏默认禁止外部 socket、使用内存数据库和 FakeBroker，并隔离设置缓存。当前全量验收为
`148 passed`；真实 IBKR PAPER 脚本不属于默认 pytest。

## 开发说明

### 打包

项目使用 `setuptools`，并通过 `pip install -e` 进行 editable install。

### Python 版本

- Python `>= 3.11`

### Lint

`dev` 依赖组包含 `ruff`：

```bash
ruff check platform_core platform_apps tests scripts
```

### 依赖组

- `dev`：测试和 lint 工具
- `ibkr`：Interactive Brokers API 支持
- `postgres`：Postgres 或 Timescale 的 Psycopg 支持

## 扩展这个 Starter

常见演进方式：

1. 在独立策略包中实现插件并通过 entry-point 注册，复用同一运行时进入回测、PAPER 和 LIVE。
2. 在数据库中提议、审批并激活账户级或全局风控策略，不绕过 ExecutionSDK。
3. 添加策略专用特征、信号和组合排序逻辑。
4. 按照执行、审计和分析需求扩展数据库 schema 与事件消费者。
5. 为服务补充镜像、TLS、密钥系统、告警、备份和进程监督后再部署实盘。

## 已知边界

- 内置策略是 smoke 示例，不是生产 alpha。
- 对历史数据的要求刻意保持适中。
- 这个 starter 追求清晰和可复用，而不是最大吞吐量。
- 当前 Compose 不是完整生产部署；密钥管理、TLS、备份、CI/CD 和进程守护仍需继续建设。
- 已过期期权的历史 bar 不是内置 IBKR smoke 流程的目标。
- 实际 1100/1101/1102 验收必须人工中断并恢复 Gateway 上游连接。
- Flex 真实验收要求有效的 `IB_FLEX_TOKEN` 和 `IB_FLEX_QUERY_ID`。

## 为什么会有这个项目

大多数交易项目会在研究代码、实盘代码和回测代码过早分化之后逐渐腐化。这个仓库希望提供一个更干净的起点：一个平台核心、一套契约，以及一个统一的扩展位置。

如果之后需要策略专用仓库，这个 starter 应该保持小而稳定，而更快变化的 alpha 逻辑则放到下游仓库中承载。
