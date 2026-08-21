# 策略无关的美股量化平台起步项目

[English](README.md) | [简体中文](README.zh-CN.md)

这是一个面向美股、ETF 和期权研究、模拟交易与实盘交易的无策略基础平台。策略以独立 Python 包存在，并在回测、IBKR 模拟盘和 IBKR 实盘中使用同一套运行时协议。

本仓库刻意不内置任何 Alpha、入场规则、出场规则或示例策略。

## 运行流程

```text
IBKR / 历史数据库
  -> MarketQuote / BarEvent
  -> 外部 StrategyPlugin
  -> PortfolioDecision
  -> 平台风控闸门
  -> ExecutionRequest
  -> 持久化 OrderManager
  -> SimulatedBroker / IBKR 模拟盘 / IBKR 实盘
  -> BrokerOrderUpdate / ExecutionFill
  -> 持仓、账户状态、数据库、事件流
```

三种模式共用同一个 `TradingEngine`、`OrderManager`、数据模型、风控闸门和事件类型。策略通过部署配置切换运行模式，无需修改策略代码。

## 已具备的平台能力

- 外部策略 SDK，以及 `package.module:attribute` 形式的加载器
- 统一的行情、K 线、决策、风控、订单、成交、账户和持仓数据模型
- 使用 conid 或完整到期日/看涨看跌/行权价/乘数信息表示稳定的期权标识
- 历史数据库数据通过生产运行时路径进行与策略无关的回放
- 用于回测和契约测试的确定性模拟经纪商
- IBKR 行情、合约发现、模拟盘订单、实盘订单、撤单、状态、成交、佣金、账户、持仓和未结订单恢复边界
- 通过 `client_order_id` 实现持久、幂等的 OMS 存储
- 适合单体开发的内存事件总线，以及面向服务拆分的 Redis Streams 发布
- 轮询式实时行情源与行情转 K 线聚合
- 数据摄取、测试夹具、数据质量检查、SQLite 开发环境和 Postgres 数据库结构
- 实盘双重安全锁：启用开关与精确确认值缺一不可

## 仓库职责边界

平台负责：

- 标准化行情与执行事件
- 策略生命周期与插件加载
- 向策略提供账户和持仓上下文
- 风控审批
- 订单构造、提交、持久化、恢复与对账
- 回测、模拟盘和实盘运行时编排

外部策略包负责：

- 策略特有的指标和特征状态
- 入场与出场决策
- 以 `PortfolioDecision` 表达的仓位意图
- 策略参数与版本管理

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ibkr]"
pytest -q
python scripts/run_smoke.py --mode offline
```

离线冒烟命令会验证测试数据摄取和数据质量处理，不会加载或执行任何策略。

## 外部策略协议

独立策略包可以继承 `BaseStrategy`，也可以直接实现 `StrategyPlugin`：

```python
from platform_core.strategy import BaseStrategy, StrategyContext


class MyStrategy(BaseStrategy):
    strategy_code = "my-strategy"
    strategy_version = "0.1.0"

    def __init__(self, parameters=None):
        self.parameters = parameters or {}

    def on_bar(self, event, context: StrategyContext):
        # 返回零个或多个 PortfolioDecision 对象。
        return []
```

完整生命周期还包括 `on_start`、`on_quote`、`on_order_update`、`on_fill` 和 `on_stop`。`StrategyContext` 包含运行模式、部署参数、最新行情、经纪商持仓和账户快照。

加载路径格式为：

```text
my_strategy.package:MyStrategy
```

无需在本仓库中注册策略或修改源代码。

可快速创建一个独立的空策略包：

```bash
python scripts/scaffold_strategy.py earnings-reversal --output-dir ../quant-strategies
```

生成的包实现完整生命周期协议，但不包含交易规则。在同一环境中安装平台和生成的策略包后，即可在回测或运行时命令中使用其导入路径。

## 回测外部策略

先摄取或载入历史数据，然后运行：

```bash
python scripts/run_backtest.py \
  --strategy my_strategy.package:MyStrategy \
  --strategy-params '{"notional": 10000}' \
  --track dual \
  --symbols SPY \
  --start 2026-05-27 \
  --end 2026-05-27
```

回放路径为 `TradingEngine -> risk -> OrderManager -> SimulatedBroker`，与模拟盘/实盘选择经纪商后的执行路径一致。订单、成交、持仓、事件、盈亏、费用、总名义金额和期末权益会被持久化或输出到报告。

## IBKR 模拟盘运行时

启动 TWS 或 IB Gateway 并启用 API 访问，然后配置：

```dotenv
IB_HOST=127.0.0.1
IB_PAPER_PORT=7497
IB_PAPER_ACCOUNT=DU1234567
IB_CLIENT_ID=11
```

启动外部策略：

```bash
python scripts/run_runtime.py \
  --mode paper \
  --strategy my_strategy.package:MyStrategy \
  --strategy-params '{"notional": 1000}' \
  --symbols SPY \
  --publish-redis
```

使用 `--once` 可为每个标的仅处理一次行情快照，作为连接和运行时探针。

对于期权或需要精确合约标识的场景，请提供标的清单：

```bash
python scripts/run_runtime.py \
  --mode paper \
  --strategy my_strategy.package:MyStrategy \
  --instruments-file instruments.json
```

`instruments.json` 是符合 `InstrumentRef` 数据模型的 JSON 列表，包含期权到期日、看涨/看跌、行权价、可用时的 conid，以及可选的合约乘数元数据。

## IBKR 实盘运行时与安全锁

实盘模式默认禁用。它需要独立的账户/端口，并同时通过两个显式开关：

```dotenv
IB_LIVE_PORT=7496
IB_LIVE_ACCOUNT=U1234567
ALLOW_LIVE_TRADING=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_ARE_REAL
```

之后运行命令与模拟盘保持相同结构：

```bash
python scripts/run_runtime.py \
  --mode live \
  --strategy my_strategy.package:MyStrategy \
  --symbols SPY
```

在真实部署中，请隔离模拟盘与实盘的账户、数据库、Redis Streams、客户端 ID、密钥及部署凭证。

## 数据流与服务

每个运行时都会发布标准化的 `PlatformEvent` 对象，例如：

- `BAR_RECEIVED`
- `QUOTE_RECEIVED`
- `PORTFOLIO_DECISION`
- `RISK_REJECTED`
- `ORDER_SUBMITTED`
- `ORDER_UPDATE`
- `FILL`

本地开发使用 `InMemoryEventBus`。`RedisStreamEventBus` 将同一事件信封发布至 `quant:events`，使行情、策略、风控、执行和盈亏服务日后能够拆分，而无需修改策略代码。

`platform_apps/` 下的 FastAPI 包仍是部署入口外壳。可工作的单进程参考运行时为 `platform_apps.runtime.main` / `scripts/run_runtime.py`。

## 数据库与迁移

- `migrations/0001_baseline.sql` 用于创建全新数据库结构。
- `migrations/0002_strategy_agnostic_runtime.sql` 用于升级原始起步项目的执行相关表。
- SQLite 开发环境使用 SQLAlchemy `create_all`；生产 Postgres 应在启动模拟盘/实盘服务前应用迁移。

执行持久化包含运行模式、账户、追踪 ID、客户端与经纪商订单 ID、完整标的标识、订单状态、成交、费用和合约感知的持仓。

## 开发质量门禁

```bash
pytest -q
ruff check .
python -m compileall -q platform_core platform_apps scripts tests
```

将策略晋级实盘前，应在策略包中补充确定性回放、样本外验证、模拟盘会话对账、重启恢复、风控拒绝、部分成交和断线行为测试。平台安全锁可防止意外启动实盘，但不能证明策略安全或盈利。

## 起步项目的已知边界

- IBKR 集成需要 TWS/Gateway 以及相应的行情权限。
- 实时行情当前使用可替换的轮询数据源；高频场景应在相同标准化事件协议之后提供流式适配器。
- 回测经纪商刻意保持确定性和基础实现；高级滑点、交易所排队、指派、行权、保证金和公司行动模型应放入可替换的模拟组件。
- 生产部署仍需补充组织特定的密钥管理、告警、仪表盘、CI/CD 和基础设施定义。
