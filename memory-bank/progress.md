# 开发进度日志

> 本文档记录每个实施步骤的完成情况，供未来开发者参考。
> 说明：本文件的 Step 编号与 `memory-bank/implementation-plan.md` 对齐；不在计划内的工作以 "Milestone/附加改进" 形式记录，避免编号混淆。

---

## 进度总览

| 阶段 | 状态 |
|------|------|
| MVP（配置/WS/信号/执行/仓位收敛） | ✅ |
| 阶段 6：WS 重连 + 校准 + 优雅退出 | ✅ |
| 阶段 7：执行模式轮转（MAKER ↔ AGGRESSIVE） | ✅ |
| 阶段 8：加速/ROI 倍数 | ✅ |
| 阶段 9：风控兜底 + 限速 + 保护性止损 | ✅ |
| 阶段 10：Telegram 通知 | ✅ |
| 阶段 11：systemd 部署 | ✅ |
| **小额实盘验证** | ⏳ |

## Milestone/附加改进：保护性止损“外部接管”事件驱动恢复

**状态**：✅ 已完成<br>
**日期**：2025-12-20<br>
**动机**：当交易所/外部已有 `closePosition` 条件单时，本程序选择“外部接管”（撤掉自己并停止维护）。为了避免外部单被手动撤销后本程序无法及时恢复维护，本次补齐了 User Data Stream 的条件单事件解析与触发同步。<br>
**产出**：
- `src/models.py`：新增 `AlgoOrderUpdate`；`OrderUpdate` 增加 `order_type/close_position`
- `src/ws/user_data.py`：支持解析 `ALGO_UPDATE`（Algo Service 条件单更新），并在调试阶段直接打印关键字段（后续可降级为 debug 或移除）
- `src/main.py`：收到 `ALGO_UPDATE`（或外部 `ORDER_TRADE_UPDATE` 的 `closePosition`）后调度一次 protective stop 同步
- `tests/test_ws_user_data.py`：新增/更新解析测试覆盖 `cp/o/ALGO_UPDATE`
<br>
**补充改进（同批交付）**：<br>
- 保护性止损只允许“收紧”（LONG stopPrice 只上调；SHORT stopPrice 只下调），避免仓位变安全时把止损越推越远，并减少频繁撤旧建新带来的空窗风险<br>
- 保护性止损同步采用分级 debounce：`position_update` 1s；`startup/calibration` 0s；其余 0.2s（兼顾 REST 压力与关键场景恢复速度）<br>
- 启动同步时打印已存在的外部 `closePosition` 条件单（含 order_id/client_id），并在 `skip_external_stop` 时附带外部单关键字段便于排查；新增“外部多单”告警（同侧出现多张外部 stop/tp 时打印摘要）<br>
- 外部接管从 TTL 提示升级为“锁存 + 保险丝”：外部 stop/tp（`cp=True` 或 `reduceOnly=True`）一旦出现即锁存接管；锁存期间按 `external_takeover.rest_verify_interval_s` 周期触发 REST 校验，若长期无外部单则释放锁存并恢复自维护（配置：`global.risk.protective_stop.external_takeover.*`）<br>
- 测试：补充 `tests/test_protective_stop.py`（只收紧语义/启动外部单日志等）与 `tests/test_main_shutdown.py`（debounce 分级逻辑）

### 可选后续工作

| 优先级 | 内容 | 来源 |
|--------|------|------|
| 低 | 配置热更新（运行时 reload） | design-document 9.1 |
| 低 | 可观测性指标（撤单率、成交率、模式分布） | design-document 1.2 |
| 低 | Docker 部署 | implementation-plan 11.1 |
| 低 | JSON 日志格式 | design-document 12 |

---

## Step 0.1：确认运行目标与最小闭环范围（MVP）

**状态**：✅ 已完成<br>
**日期**：2024-12-16<br>
**产出**：`memory-bank/mvp-scope.md`

### 完成内容
1. 创建了 MVP 范围定义文档，明确划分"包含"与"不包含"
2. 交叉验证设计文档第 1、3、4、7 章，确认核心链路覆盖完整
3. 定义了 7 项验收标准

### MVP 核心链路
```
WS 行情 → 信号判断 → 下单/撤单 → 仓位收敛
```

### MVP 边界
- **包含**：配置系统、交易所适配、WS 数据、信号层（原始两类条件）、执行层（仅 MAKER_ONLY）、仓位收敛、日志、优雅退出
- **不包含**：模式轮转、加速/ROI 倍数、风控兜底、限速、Telegram、多 symbol 并发

### 评审结果
- 用户确认验证通过

---

## Step 1.1：建立目录结构与模块边界

**状态**：✅ 已完成<br>
**日期**：2024-12-16<br>
**产出**：`src/` 目录结构、`models.py`、各模块空实现

### 完成内容
1. 创建目录结构：`src/{config,exchange,ws,signal,execution,risk,notify,utils}/`
2. 创建核心数据结构 `src/models.py`（11 个枚举 + 11 个 dataclass）
3. 创建 8 个模块的空实现（接口定义 + docstring）
4. 创建所有 `__init__.py` 导出
5. 创建配置文件 `config/config.yaml`

### 文件清单（22 个 Python 文件）
```
src/
├── __init__.py, models.py, main.py
├── config/{__init__.py, loader.py, models.py}
├── exchange/{__init__.py, adapter.py}
├── ws/{__init__.py, market.py, user_data.py}
├── signal/{__init__.py, engine.py}
├── execution/{__init__.py, engine.py}
├── risk/{__init__.py, manager.py}
├── notify/{__init__.py, telegram.py}
└── utils/{__init__.py, logger.py, helpers.py}
```

### 数据结构
| 类别 | 内容 |
|------|------|
| 枚举 | PositionSide, OrderSide, OrderType, TimeInForce, OrderStatus, ExecutionMode, ExecutionState, SignalReason |
| 数据 | MarketEvent, MarketState, Position, SymbolRules, ExitSignal, OrderIntent, OrderResult, OrderUpdate, SideExecutionState, RiskFlag |

### 模块职责映射
| 需求 | 模块 |
|------|------|
| WS 重连 | `ws/market.py`, `ws/user_data.py` |
| reduceOnly 参数 | `exchange/adapter.py` |
| 信号判断 | `signal/engine.py` |
| 状态机 | `execution/engine.py` |
| 数据陈旧检测 | `risk/manager.py` |
| 日志滚动 | `utils/logger.py` |

### 评审结果
- 用户确认验证通过

---

## Step 1.2：配置文件与配置覆盖规则（global + symbols）

**状态**：✅ 已完成<br>
**日期**：2024-12-16<br>
**产出**：`src/config/models.py`、`src/config/loader.py`、`config/config.yaml`、`tests/test_config.py`

### 完成内容
1. 实现 pydantic 配置模型（`src/config/models.py`, 226 行）
   - 子配置: ReconnectConfig, WSConfig, ExecutionConfig, AccelConfig, RoiConfig, RiskConfig, RateLimitConfig, TelegramConfig
   - Symbol 覆盖: SymbolExecutionConfig, SymbolAccelConfig, SymbolRoiConfig, SymbolConfig
   - 顶层: GlobalConfig, AppConfig
   - 运行时合并: MergedSymbolConfig

2. 实现配置加载器（`src/config/loader.py`, 238 行）
   - YAML 文件加载（PyYAML）
   - 环境变量读取（BINANCE_API_KEY, BINANCE_API_SECRET）
   - `get_symbol_config()`: 合并 global + symbol 覆盖
   - `get_symbols()`: 获取所有配置的 symbol 列表

3. 创建测试配置文件（`config/config.yaml`）
   - 完整的 global 配置
   - BTC/USDT:USDT 和 ETH/USDT:USDT 覆盖示例

4. 编写单元测试（`tests/test_config.py`, 12 个测试用例）
   - 配置加载、API 密钥、Symbol 合并、默认值测试

### 配置合并规则
```
global 默认值 + symbol 覆盖 = MergedSymbolConfig
```
- symbol 覆盖优先
- 未指定的字段继承 global 默认值
- 不存在的 symbol 使用完全 global 默认值

### 测试结果
```
12 passed in 0.23s
```

### 评审结果
- 用户确认验证通过

---

## Step 1.3：日志系统（按天滚动 + 结构化）

**状态**：✅ 已完成<br>
**日期**：2024-12-16<br>
**产出**：`src/utils/logger.py`、`tests/test_logger.py`

### 完成内容
1. 配置 loguru 日志（`src/utils/logger.py`, 388 行）
   - 按天滚动 (`rotation="00:00"`)
   - 30 天保留 (`retention="30 days"`)
   - 旧日志 gzip 压缩
   - 控制台彩色输出 + 文件输出
   - 错误日志单独文件 (`error_*.log`)

2. 实现结构化日志格式
   - 格式: `{time} | {level} | {name}:{function}:{line} | {message}`
   - 12 种事件类型，自动选择日志级别

3. 便捷日志函数（15 个）
   - log_startup, log_shutdown
   - log_ws_connect, log_ws_disconnect, log_ws_reconnect
   - log_market_update, log_signal
   - log_order_place, log_order_cancel, log_order_fill, log_order_timeout
   - log_position_update, log_error

4. 单元测试（`tests/test_logger.py`, 26 个测试用例）

### 测试结果
```
38 passed in 0.35s (配置 12 + 日志 26)
```

### 评审结果
- 用户确认验证通过

---

## Step 2：交易所适配层（ExchangeAdapter）

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/exchange/adapter.py`、`tests/test_exchange.py`

### 完成内容
1. ccxt 初始化与 markets 加载（`src/exchange/adapter.py`, 590 行）
   - Binance USDT-Margined 永续合约 (`binanceusdm`)
   - Hedge 模式支持
   - 测试网切换
   - 交易规则提取（tickSize, stepSize, minQty, minNotional）

2. 价格/数量规整函数
   - `round_price()`: 按 tickSize 规整价格
   - `round_qty()`: 按 stepSize 规整数量
   - `ensure_min_notional()`: 确保满足最小名义价值

3. Hedge 模式仓位读取
   - `fetch_positions()`: 获取 LONG/SHORT 仓位
   - `is_position_complete()`: 判断仓位是否已完成
   - `get_tradable_qty()`: 获取可交易数量

4. 下单/撤单接口
   - `place_order()`: 下单（LIMIT，positionSide）
   - `cancel_order()`: 撤单
   - `cancel_all_orders()`: 批量撤单
   - `fetch_open_orders()`: 获取挂单

5. 单元测试（`tests/test_exchange.py`, 20 个测试用例）

### 类型检查修复
修复 pyright/pylance 严格模式下的类型错误：
- 添加 `exchange` 属性确保非空访问
- 使用 `cast()` 处理 ccxt 返回类型
- 添加 `# type: ignore` 忽略 ccxt 库类型问题

### 测试结果
```
58 passed in 1.49s (配置 12 + 交易所 20 + 日志 26)
```

### 评审结果
- 用户确认验证通过

---

## Step 3.1：市场数据 WebSocket（MarketWSClient）

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/ws/market.py`、`tests/test_ws_market.py`

### 完成内容
1. 实现 MarketWSClient 类（`src/ws/market.py`, ~410 行）
   - 连接 Binance Futures WebSocket（`wss://fstream.binance.com`）
   - Combined Streams URL 构建（支持多 symbol）
   - bookTicker 解析（best bid/ask）
   - aggTrade 解析（last trade price）
   - 指数退避重连（1s → 2s → 4s → ... → 30s max）
   - 数据陈旧检测（stale_data_ms 阈值）

2. 依赖更新（`requirements.txt`）
   - `websocket-client` 替换为 `websockets>=12.0`

3. 类型检查适配
   - `WebSocketClientProtocol` → `ClientConnection`（websockets 12+ API）
   - `is_connected` 属性使用 `state.name == "OPEN"` 检查

4. 单元测试（`tests/test_ws_market.py`, 23 个测试用例）
   - 初始化测试
   - Symbol 格式转换测试
   - Stream URL 构建测试
   - bookTicker/aggTrade 解析测试
   - 陈旧数据检测测试
   - 重连机制测试
   - 消息处理测试

### 核心接口
```python
class MarketWSClient:
    def __init__(
        self,
        symbols: List[str],           # ccxt 格式 symbol 列表
        on_event: Callable[[MarketEvent], None],  # 事件回调
        initial_delay_ms: int = 1000,  # 重连初始延迟
        max_delay_ms: int = 30000,     # 重连最大延迟
        multiplier: int = 2,           # 延迟倍数
        stale_data_ms: int = 1500,     # 陈旧阈值
    )

    async def connect() -> None       # 建立连接
    async def disconnect() -> None    # 断开连接
    def is_stale(symbol: str) -> bool # 检测陈旧
    @property is_connected -> bool    # 连接状态
    @property reconnect_count -> int  # 重连次数
```

### 测试结果
```
81 passed in 3.03s (配置 12 + 交易所 20 + 日志 26 + WS市场 23)
```

### 评审结果
- 用户确认验证通过

---

## Step 3.2：实现数据陈旧（stale）检测

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/ws/market.py`、`src/main.py`、`tests/test_ws_market.py`

### 完成内容
- per-symbol 维护最近更新时间戳；bookTicker 或 aggTrade 任一更新即刷新
- 提供 `MarketWSClient.is_stale(symbol)` 供上层做"数据陈旧时暂停下单"的保护
- 备注：该能力与 Step 3.1 同批交付（Step 3.1 内也包含相关测试与实现），此处单独列出以对齐 `implementation-plan.md` 编号

### 测试结果
```
同 Step 3.1（包含陈旧数据检测用例）
```

---

## Step 3.3：User Data Stream WebSocket

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/ws/user_data.py`、`tests/test_ws_user_data.py`、`src/main.py`、`src/models.py`

### 完成内容
1. 实现 UserDataWSClient 类（`src/ws/user_data.py`, ~480 行）
   - listenKey 管理（创建/续期/关闭）via REST API
   - User Data Stream WebSocket 连接
   - ORDER_TRADE_UPDATE 事件解析
   - ACCOUNT_UPDATE 仓位更新解析（positions `P`）
   - listenKey 30 分钟自动续期
   - listenKeyExpired 自动重连
   - 指数退避重连机制

2. 仓位缓存实时同步（`src/main.py`, `src/models.py`）
   - 新增 `PositionUpdate` 数据结构，用于承载 ACCOUNT_UPDATE 仓位事件
   - 应用收到 0 仓位更新时删除缓存，避免"幽灵仓位"
   - REST 刷新仓位时先清空再回填（避免交易所不返回 0 仓位导致残留）

3. 依赖更新（`requirements.txt`）
   - 添加 `aiohttp>=3.9.0`（REST API 调用）

4. 单元测试（`tests/test_ws_user_data.py`, 30 个测试用例）
   - 初始化测试
   - URL 测试（主网/测试网）
   - ORDER_TRADE_UPDATE 解析测试
   - ACCOUNT_UPDATE 解析测试
   - 订单状态解析测试
   - 消息处理测试
   - 重连机制测试
   - 常量测试

### 核心接口
```python
class UserDataWSClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        on_order_update: Callable[[OrderUpdate], None],
        on_position_update: Optional[Callable[[PositionUpdate], None]] = None,
        on_reconnect: Optional[Callable[[str], None]] = None,
        testnet: bool = False,
        proxy: Optional[str] = None,
        initial_delay_ms: int = 1000,
        max_delay_ms: int = 30000,
        multiplier: int = 2,
    )

    async def connect() -> None        # 建立连接
    async def disconnect() -> None     # 断开连接
    @property is_connected -> bool     # 连接状态
    @property reconnect_count -> int   # 重连次数
    @property listen_key -> Optional[str]  # 当前 listenKey
```

### 测试结果
```
pytest -q: 202 passed
```

### 评审结果
- 用户确认验证通过

---

## Step 4：信号层（SignalEngine）

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/signal/engine.py`、`tests/test_signal.py`

### 完成内容
1. 实现 SignalEngine 类（`src/signal/engine.py`, ~320 行）
   - MarketState 聚合（从 MarketEvent 更新 bid/ask/trade）
   - 数据就绪检测（需要 bid/ask + 至少两个 trade）
   - LONG 平仓条件判断：
     - `long_primary`: last > prev AND best_bid >= last
     - `long_bid_improve`: best_bid >= last AND best_bid > prev
   - SHORT 平仓条件判断：
     - `short_primary`: last < prev AND best_ask <= last
     - `short_ask_improve`: best_ask <= last AND best_ask < prev
   - 触发节流（min_signal_interval_ms，默认 200ms）
   - 状态清除和节流重置

2. 单元测试（`tests/test_signal.py`, 18 个测试用例）
   - 初始化测试
   - 市场数据更新测试
   - LONG/SHORT 退出条件测试
   - 节流测试
   - 边界情况测试

### 核心接口
```python
class SignalEngine:
    def __init__(self, min_signal_interval_ms: int = 200)

    def update_market(event: MarketEvent) -> None        # 更新市场状态
    def evaluate(symbol, position_side, position, current_ms) -> Optional[ExitSignal]  # 评估
    def get_market_state(symbol) -> Optional[MarketState]  # 获取状态
    def is_data_ready(symbol) -> bool                      # 数据就绪
    def reset_throttle(symbol, position_side) -> None      # 重置节流
    def clear_state(symbol) -> None                        # 清除状态
```

### 测试结果
```
126 passed in 2.16s (配置 12 + 交易所 20 + 日志 26 + WS市场 23 + WS用户 27 + 信号 18)
```

### 评审结果
- 用户确认验证通过

---

## Step 5：执行层（ExecutionEngine）

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**产出**：`src/execution/engine.py`、`tests/test_execution.py`

### 完成内容
1. 实现 ExecutionEngine 类（`src/execution/engine.py`, ~545 行）
   - 状态机管理（IDLE → PLACING → WAITING → CANCELING → COOLDOWN → IDLE）
   - 每个 symbol+position_side 独立的执行状态（SideExecutionState）
   - MVP 阶段仅支持 MAKER_ONLY 模式

2. 信号处理
   - `on_signal()`: 处理平仓信号，生成 OrderIntent
   - LONG 平仓 → SELL，SHORT 平仓 → BUY
   - 仅在 IDLE 状态处理新信号
   - 自动跳过已完成仓位（< min_qty）

3. Maker 定价策略
   - `build_maker_price()`: 计算 maker 挂单价格
   - `at_touch`: 挂在对手价（best_ask/best_bid）
   - `inside_spread_1tick`: 深入盘口一个 tick
   - `custom_ticks`: 深入盘口 N 个 tick

4. 数量计算
   - `compute_qty()`: 计算下单数量
   - base_qty = min_qty × base_lot_mult
   - 受仓位、max_order_notional 限制
   - 按 step_size 规整

5. 订单生命周期管理
   - `on_order_placed()`: 下单结果处理（成功 → WAITING，失败 → IDLE）
   - `on_order_update()`: 订单状态更新（FILLED → IDLE，CANCELED → COOLDOWN）
   - `check_timeout()`: TTL 超时检测，触发撤单
   - `check_cooldown()`: 冷却期结束检测，回到 IDLE

6. 单元测试（`tests/test_execution.py`, 41 个测试用例）
   - 初始化测试
   - 状态管理测试
   - Maker 价格计算测试
   - 数量计算测试
   - 仓位完成检查测试
   - 信号处理测试
   - 订单结果/更新处理测试
   - 超时/冷却检查测试
   - 完整状态机周期测试

### 核心接口
```python
class ExecutionEngine:
    def __init__(
        self,
        place_order: Callable[[OrderIntent], Awaitable[OrderResult]],
        cancel_order: Callable[[str, str], Awaitable[OrderResult]],
        order_ttl_ms: int = 800,
        repost_cooldown_ms: int = 100,
        base_lot_mult: int = 1,
        maker_price_mode: str = "inside_spread_1tick",
        maker_n_ticks: int = 1,
        max_mult: int = 50,
        max_order_notional: Decimal = Decimal("200"),
    )

    async def on_signal(signal, position_amt, rules, market_state, current_ms) -> Optional[OrderIntent]
    async def on_order_placed(symbol, position_side, result, current_ms) -> None
    async def on_order_update(update, current_ms) -> None
    async def check_timeout(symbol, position_side, current_ms) -> bool
    def check_cooldown(symbol, position_side, current_ms) -> bool
    def build_maker_price(position_side, best_bid, best_ask, tick_size) -> Decimal
    def compute_qty(position_amt, min_qty, step_size, last_trade_price) -> Decimal
    def is_position_done(position_amt, min_qty, step_size) -> bool
    def get_state(symbol, position_side) -> SideExecutionState
    def reset_state(symbol, position_side) -> None
```

### 测试结果
```
167 passed in 3.00s (配置 12 + 交易所 20 + 日志 26 + WS市场 23 + WS用户 27 + 信号 18 + 执行 41)
```

### 评审结果
- 用户确认验证通过

---

## Milestone：main.py 事件循环集成（MVP 集成）

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**提交**：`162740d`<br>
**产出**：`src/main.py`

### 完成内容
1. 实现 Application 类
   - 模块初始化（ConfigLoader, ExchangeAdapter, WSClient, SignalEngine, ExecutionEngine）
   - 主事件循环（信号评估 + 订单管理）
   - 优雅退出处理（SIGINT/SIGTERM）

2. 配置加载和初始化
   - 加载 YAML 配置
   - 设置日志系统
   - 获取交易规则
   - 获取初始仓位

3. 主事件循环
   - 50ms 间隔评估平仓信号
   - 100ms 间隔检查订单超时
   - 陈旧数据保护（不在数据陈旧时下单）

4. 模块协调
   - MarketWSClient → SignalEngine（市场数据更新）
   - SignalEngine → ExecutionEngine（信号触发）
   - ExecutionEngine → ExchangeAdapter（下单/撤单）
   - UserDataWSClient → ExecutionEngine（订单状态更新）

5. 优雅退出
   - 注册 SIGINT/SIGTERM 信号处理器
   - 停止主循环
   - 撤销所有挂单
   - 关闭 WebSocket 连接
   - 关闭交易所连接

6. 配置模型更新
   - GlobalConfig 添加 `testnet` 字段

### 核心接口
```python
class Application:
    def __init__(config_path: Path)

    async def initialize() -> None     # 初始化所有模块
    async def run() -> None            # 运行应用
    async def shutdown() -> None       # 优雅关闭
    def request_shutdown() -> None     # 请求关闭

async def main(config_path: Path) -> None  # 入口函数
```

### 事件流程
```
启动:
  main() → Application.initialize() → Application.run()
       │
       ├── ConfigLoader.load()
       ├── ExchangeAdapter.initialize()
       ├── SignalEngine()
       ├── ExecutionEngine() × N symbols
       ├── MarketWSClient.connect()
       └── UserDataWSClient.connect()

运行时:
  MarketEvent → SignalEngine.update_market()
                    ↓
  evaluate() → ExitSignal → ExecutionEngine.on_signal()
                                ↓
                          OrderIntent → place_order()
                                           ↓
                                      OrderResult → on_order_placed()

关闭:
  SIGINT/SIGTERM → request_shutdown()
                       ↓
                  shutdown()
                       ├── 取消任务
                       ├── 撤销所有挂单
                       ├── 关闭 WebSocket
                       └── 关闭交易所连接
```

### 测试结果
```
167 passed in 4.67s
pyright: 0 errors
```

### 评审结果
- 用户确认验证通过

---

## 阶段 6：基础健壮性（WS 重连 + 状态校准）

---

## Step 6.1：WS 断线自动重连

**状态**：✅ 已完成<br>
**日期**：2024-12-17<br>
**提交**：`162740d`<br>
**产出**：`src/ws/market.py`、`src/ws/user_data.py`

### 完成内容
1. 市场 WS 与用户数据 WS 均支持断线自动重连（指数退避：1s → 2s → 4s → ... → 30s，最大重试：无限）
2. `stale_data_ms` 数据陈旧检测：断流/陈旧时暂停信号执行，避免误下单
3. 重连成功后重置退避延迟（回到初始值）

---

## Step 6.2：重连后 REST 校准（positions + rules）

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`729e789`<br>
**产出**：`src/main.py`、`src/ws/{market.py,user_data.py}`、`tests/test_ws_{market,user_data}.py`

### 完成内容
1. WS 重连成功回调触发一次校准任务（markets/rules + positions）
2. 校准期间暂停下单，避免用旧规则/旧仓位继续执行
3. 增加 `[CALIBRATION]` 事件日志便于排障

### 测试结果
```
pytest -q: 195 passed
```

---

## Step 6.3：优雅退出（Graceful Shutdown）

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`50fb152`（退出撤单隔离）<br>
**产出**：`src/main.py`、`tests/test_{main_shutdown,order_cleanup}.py`

### 完成内容
1. SIGINT/SIGTERM：停止主循环 → 撤销挂单 → 断开 WS → 关闭交易所（均带超时保护）
2. 退出撤单隔离：所有订单设置 `newClientOrderId` 前缀，仅撤销本次运行挂单，避免误撤手动订单
3. shutdown/disconnect 幂等化与资源释放修复（避免 `Unclosed client session`/文件句柄泄漏）

### 测试结果
```
pytest -q: 181 passed
```

---

## 阶段 7：执行模式轮转（maker → aggressive limit）

---

## Step 7.1：加入 execution_mode 与超时计数器

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`9401efa`<br>
**产出**：`src/models.py`、`src/execution/engine.py`、`src/config/models.py`、`tests/test_execution.py`

### 完成内容
1. 每个 symbol+side 维护 mode 与计数器：maker_timeout_count / aggr_timeout_count / aggr_fill_count
2. 阈值配置生效：maker_timeouts_to_escalate / aggr_fills_to_deescalate / aggr_timeouts_to_deescalate
3. 模式切换事件日志：`[MODE_CHANGE]`

### 测试结果
```
pytest -q: 178 passed
```

---

## Step 7.2：实现 AGGRESSIVE_LIMIT 定价与下单

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`9401efa`<br>
**产出**：`src/execution/engine.py`

### 完成内容
- LONG 平仓：SELL @ best_bid（GTC）
- SHORT 平仓：BUY  @ best_ask（GTC）
- 仍保持 Hedge 模式 positionSide 正确

---

## Step 7.3：MARKET（已取消）

**状态**：🛑 已取消<br>
**日期**：2025-12-18<br>
**说明**：原实现见 `7d71492`，后续确认 `AGGRESSIVE_LIMIT` 足够接近吃单，且 LIMIT + 短 TTL 重试更可控，因此移除 MARKET/allow_market。

### 完成内容
- 移除 `allow_market` 配置与 `MARKET` 执行模式
- 风险触发仅升级到 `AGGRESSIVE_LIMIT`

---

## 阶段 8：加速倍数（滑动窗口）与 ROI 倍数档位

---

## Step 8.1：滑动窗口 ret 与 accel_mult

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`a401db7`<br>
**产出**：`src/signal/engine.py`、`tests/test_signal.py`

### 完成内容
- 维护 per-symbol trade 历史（基于 last_trade_price）
- `ret_window = p_now/p_window_ago - 1`，按档位选择 `accel_mult`

---

## Step 8.2：ROI 档位倍数与口径确认

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`a401db7`<br>
**产出**：`src/signal/engine.py`、`README.md`

### 完成内容
- ROI 口径写入 `README.md`（避免未来误解）
- 按档位选择 `roi_mult`

---

## Step 8.3：倍数乘法合成 + 双保险生效

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`a401db7`<br>
**产出**：`src/execution/engine.py`、`tests/test_execution.py`

### 完成内容
- `final_mult = base_lot_mult × roi_mult × accel_mult`（cap 到 `max_mult`）
- `max_order_notional` 限制名义价值后得到最终 qty

### 测试结果
```
pytest -q: 193 passed
```

---

## 阶段 9：风控兜底（强平距离）+ 全局限速

---

## Step 9.1：强平距离 dist_to_liq 计算与触发

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`7d71492`<br>
**产出**：`src/risk/manager.py`、`src/main.py`、`tests/test_risk_manager.py`

### 完成内容
- `dist_to_liq = abs(mark - liq) / mark`
- 风险触发：强制至少切到 `AGGRESSIVE_LIMIT`

---

## Step 9.2：全局限速（orders/cancels 每秒上限）

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`7d71492`<br>
**产出**：`src/risk/{manager.py,rate_limiter.py}`

### 完成内容
- 滑动窗口计数限速：`max_orders_per_sec` / `max_cancels_per_sec`
- 下单/撤单前检查并记录 `rate_limit` 事件日志

### 测试结果
```
pytest -q: 188 passed
```

---

## Step 9.3：仓位保护性止损（交易所端条件单）

**状态**：✅ 已完成<br>
**日期**：2025-12-18<br>
**提交**：`981e8f1`<br>
**产出**：`src/risk/protective_stop.py`、`src/exchange/adapter.py`、`src/main.py`、`src/models.py`、`src/config/{models.py,loader.py}`、`tests/test_protective_stop.py`

### 完成内容
- 为每个"有持仓"的 `symbol + positionSide` 维护交易所端 `STOP_MARKET closePosition` 条件单（`MARK_PRICE` 触发）
- `stopPrice` 按 `liquidation_price` 与阈值 `dist_to_liq` 反推，并按 tick 规整（LONG 向上、SHORT 向下）
- 支持 `global.risk.protective_stop.*` 配置与 `symbols.<symbol>.risk.protective_stop_dist_to_liq` 覆盖
- 仓位归零时主动撤销该侧"本次运行"的遗留挂单，避免反向开仓风险

### 测试结果
```
pytest -q: 215 passed
```

---

## 阶段 10：Telegram（成交/重连/风险触发）

---

## Step 10.1：Telegram 通知通道打通

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`f421a40`<br>
**产出**：`src/notify/telegram.py`、`src/main.py`、`src/execution/engine.py`、`src/models.py`、`src/config/models.py`、`tests/test_notify_telegram.py`

### 完成内容
- 仅发送三类事件：成交 / WS 重连成功 / 风险兜底触发
- 发送失败有限重试，使用后台 task fire-and-forget（不阻塞主执行链路）
- Telegram 凭证改为环境变量：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（YAML 不再包含 token/chat_id）
- 成交通知模板改为中文多行格式：标题合并展示"平多/平空"，symbol 简写，并附带仓位变化（before -> after）
- 新增开仓/加仓告警：`telegram.events.on_open_alert`（默认 true），收到 ACCOUNT_UPDATE 且仓位绝对值上升时发送"【告警】开多/开空"

### 测试结果
```
pytest -q: 204 passed
```

---

## 附加改进（不在 implementation-plan 编号内）

- `3e789cd`：新增 `maker_safety_ticks` 降低 post-only 被拒概率
- `d84d0a7`：相同信号快照不重复打印，降低日志刷屏
- `c5aa2a9`：日志事件中英文显示与级别优化
- `50fb152`：退出时只清理程序创建的订单（与 Step 6.3 相关）
- 2025-12-18：新增 `@markPrice@1s` 行情解析与多级强制风控 `panic_close`（按 tiers 强制分片平仓，TTL=order_ttl_ms×ttl_percent；risk 订单绕过软限速；部分成交重置 timeout_count）
- 2025-12-18：简化 accel 配置，合并 `tiers_long`/`tiers_short` 为单一 `tiers`，LONG/SHORT 方向由代码自动处理

---

## 附加改进：Symbol 配置覆盖完善

**状态**：✅ 已完成<br>
**日期**：2025-12-19<br>
**产出**：`src/config/models.py`、`src/config/loader.py`、`memory-bank/*.md`

### 完成内容
1. 完善 SymbolRiskConfig，支持覆盖 risk 全部字段：
   - `liq_distance_threshold`（第一级风控）
   - `panic_close_enabled`、`panic_close_ttl_percent`、`panic_close_tiers`（第二级风控）
   - `protective_stop_enabled`、`protective_stop_dist_to_liq`（第三级风控）

2. 更新 MergedSymbolConfig，添加 panic_close 相关字段

3. 修复 loader.py 中 `liq_distance_threshold` 的 symbol 覆盖（之前直接用 global，未检查 symbol 覆盖）

4. 更新文档：
   - design-document.md / tech-stack.md：accel 配置示例改为 `tiers`，添加 panic_close 配置、risk symbol 覆盖示例
   - architecture.md：配置层次图、文件行数、更新日志

### Symbol 覆盖能力（最终）
| 配置项 | Symbol 可覆盖 |
|--------|--------------|
| execution.* | ✅ 全部 |
| accel.* | ✅ 全部 |
| roi.* | ✅ 全部 |
| risk.* | ✅ 全部 |
| testnet/proxy/ws/rate_limit/telegram | ❌ 全局配置 |

---

## Step 11.1：运行方式与重启策略

**状态**：✅ 已完成<br>
**日期**：2025-12-17<br>
**提交**：`3bc8b0e`<br>
**产出**：`deploy/systemd/vibe-quant.service`、`deploy/systemd/README.md`、`deploy/systemd/vibe-quant.env.example`、`README.md`、`src/main.py`

### 完成内容
- 提供 systemd service 模板：异常退出自动重启（`Restart=on-failure`），并设置 `TimeoutStopSec` 以便优雅退出
- 日志目录支持持久化：通过 `VQ_LOG_DIR` 指定（systemd 模板默认 `/var/log/vibe-quant`）
- 提供 `/etc/vibe-quant/` 的 config/env 推荐布局与部署说明

---

## 附加改进：保护止损 Binance Algo API 适配

**状态**：✅ 已完成<br>
**日期**：2025-12-19<br>
**产出**：`src/risk/protective_stop.py`、`src/exchange/adapter.py`、`src/utils/logger.py`

### 问题背景
1. **clientOrderId 重复错误**：撤销旧订单后用相同 clientOrderId 下新单，Binance 报 `-4116 ClientOrderId is duplicated`
2. **Algo Order 查询失败**：`fetch_open_algo_orders` 返回空数组，无法识别现有保护止损单，导致重复下单报 `-4130`

### 根本原因
1. **Binance 要求 clientOrderId 在 7 天内唯一**：即使订单被撤销或成交，该 ID 在 7 天内都不能复用
2. **2025-12-09 起**，Binance 将条件订单（STOP_MARKET 等）迁移到 Algo Service，`GET /fapi/v1/openAlgoOrders` 响应格式从 `{"data": [...]}` 变为直接返回数组 `[...]`

### 修复内容
1. **clientOrderId 唯一化**（`protective_stop.py`）
   - `build_client_order_id` 添加时间戳后缀：`vq-ps-zenusdt-L-12345`
   - 新增 `_build_client_order_id_prefix` 和 `_match_client_order_id` 前缀匹配方法
   - `sync_symbol` 和 `on_order_update` 改用前缀匹配

2. **修复 Algo Order API 响应解析**（`adapter.py`）
   - `fetch_open_algo_orders` 支持响应为数组或字典两种格式

3. **优化日志 Decimal 格式化**（`logger.py`）
   - `_format_value` 使用 `format_decimal` 自动格式化，避免显示 `7.502000000000000000000000000`

4. **类型检查修复**（`adapter.py`）
   - 修复 pyright 报告的 `str | None` 类型错误

5. **外部止损单检测**（`protective_stop.py`）
   - 新增 `_is_close_position_stop` 方法检测外部 closePosition 止损单
   - `sync_symbol` 同步前检查是否已有外部止损单，有则跳过下单
   - 避免重复下单导致 -4130 错误和无用 API 请求

### 测试结果
```
pyright: 0 errors
pytest: 26 passed
```

---

## 附加改进：保护止损外部接管（reduceOnly stop/tp）与排障日志增强

**状态**：✅ 已完成<br>
**日期**：2025-12-20<br>
**提交**：`decc653`<br>
**产出**：`src/main.py`、`src/risk/protective_stop.py`、`src/ws/user_data.py`、`src/models.py`、`src/config/models.py`、`src/config/loader.py`

### 目标
- 将“外部接管”判定扩展为：**只要是 reduceOnly 的 stop/tp 条件单**，即视为外部接管（不要求 `closePosition=True`）。<br>
- 在调试阶段增强 WS 原始消息可观测性：打印关键字段（`cp/R/sp/wt/f/ps` 等），用于确认不同客户端（mac/ios）下单形态。<br>
- 降低外部接管期间的日志刷屏：对重复的 `skip_external_stop*` 做节流。<br>

### 关键改动
- 外部接管识别：`STOP/TAKE_PROFIT*` 且 `reduceOnly=True`（同时保留 `closePosition=True` 兜底）。<br>
- 外部接管锁存：WS 看到外部 stop/tp `NEW` 后锁存，直到终态事件或 REST 保险丝确认外部已消失才释放。<br>
- 日志增强：新增 `[WS_RAW_DETAIL] ORDER_TRADE_UPDATE` / `[WS_RAW_DETAIL] ALGO_UPDATE` 打印关键字段与 keys 列表。<br>
- 日志节流：新增 `global.risk.protective_stop.external_takeover.skip_log_throttle_s`（默认 2s）。<br>

### 测试结果
```
pyright: 0 errors
pytest: 全量通过
```

---

## 小额实盘验证

> 根据 design-document 第 13 节和 mvp-scope 验收标准

### 验证清单

| 验证项 | 验证方法 | 状态 |
|--------|----------|------|
| reduceOnly/positionSide | 下单后检查订单参数、不会反向开仓 | ⏳ |
| post-only (GTX) | maker 订单被交易所接受、不会立即成交 | ⏳ |
| 断线重连 | 手动断网/杀进程后自动恢复 | ⏳ |
| 模式轮转 | maker 连续超时后切到 AGGRESSIVE_LIMIT | ⏳ |
| 仓位收敛 | 运行至仓位归零或 < minQty | ⏳ |
| 优雅退出 | Ctrl+C 后挂单被撤销 | ⏳ |
| 保护性止损 | 交易所界面能看到 STOP_MARKET 条件单 | ⏳ |

### 环境准备

- [ ] `config/config.yaml` 配置文件
- [ ] 环境变量：`BINANCE_API_KEY`、`BINANCE_API_SECRET`
- [ ] （可选）Telegram：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
- [ ] 测试网 or 主网小额仓位

### 验证记录

（验证过程中填写）
