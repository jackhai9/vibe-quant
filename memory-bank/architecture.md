# 系统架构

> 本文档描述系统的整体架构和每个文件/模块的作用。
> 随着开发进展持续更新。

---

## 项目概述

Binance U 本位永续 Hedge 模式 Reduce-Only 小单平仓执行器。

**核心目标**：通过多次小量 + 执行模式轮转 + 超时撤单，完成 Hedge 模式下 LONG/SHORT 仓位的 reduce-only 平仓，尽量降低滑点与市场冲击。

---

## 技术栈

- **语言**：Python 3.11+
- **异步**：asyncio
- **交易所**：ccxt (REST)
- **WebSocket**：websockets (WS)
- **HTTP**：aiohttp（User Data Stream listenKey 管理）
- **配置**：PyYAML + pydantic
- **日志**：loguru
- **通知**：aiohttp（Telegram Bot API）
- **测试**：pytest + pytest-asyncio

---

## 数据存储（当前无数据库）

- **配置**：`config/config.yaml`（YAML）
- **日志**：`logs/`（按天滚动）
- **持久化数据库**：无（当前所有状态仅在内存中维护）

---

## 模块架构（设计规划）

```
┌─────────────────────────────────────────────────────────────┐
│                         main.py                              │
│                    (入口 + 事件循环)                          │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ ConfigManager│    │   WSClient   │    │ExchangeAdapter│
│  (配置加载)   │    │ (WS 数据流)  │    │  (ccxt REST)  │
└──────────────┘    └──────────────┘    └──────────────┘
                              │
                              ▼
                    ┌──────────────┐
                    │ SignalEngine │
                    │  (信号判断)   │
                    └──────────────┘
                              │
                              ▼
                    ┌──────────────┐
                    │ExecutionEngine│
                    │ (状态机+下单) │
                    └──────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ RiskManager  │    │    Logger    │    │   Notifier   │
│ (风控兜底)    │    │ (日志滚动)   │    │ (Telegram)   │
└──────────────┘    └──────────────┘    └──────────────┘
```

---

## 模块职责

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| **ConfigManager** | 加载 YAML 配置，支持 global + symbol 覆盖 | config.yaml | 配置对象 |
| **WSClient** | 订阅 bookTicker + aggTrade + markPrice@1s + User Data Stream，断线重连，重连后回调触发校准 | 配置 | MarketEvent, OrderUpdate, PositionUpdate |
| **ExchangeAdapter** | ccxt 封装：markets/positions/balance 查询，下单/撤单 | 配置, OrderIntent | OrderResult, Position |
| **SignalEngine** | 评估平仓触发条件，维护 prev/last trade price；计算 accel/ROI 倍数 | MarketEvent, Position | ExitSignal |
| **ExecutionEngine** | 状态机管理，下单/撤单/TTL 超时处理 | ExitSignal, 配置 | OrderIntent |
| **RiskManager** | 强平距离兜底（dist_to_liq）+ 全局限速（orders/cancels） | Position, MarketEvent | RiskFlag |
| **Logger** | 按天滚动日志，结构化字段 | 各模块事件 | 日志文件 |
| **Notifier** | Telegram 通知（成交/重连/风险触发/开仓告警） | 关键事件 | 消息推送 |

---

## 数据结构（已实现）

所有数据结构定义在 `src/models.py`，模块间通过这些结构传递数据，不直接访问内部状态。

### 枚举类型
| 枚举 | 用途 |
|------|------|
| `PositionSide` | LONG / SHORT |
| `OrderSide` | BUY / SELL |
| `OrderType` | LIMIT / STOP_MARKET |
| `TimeInForce` | GTC / GTX / IOC / FOK |
| `OrderStatus` | NEW / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED / EXPIRED |
| `ExecutionMode` | MAKER_ONLY / AGGRESSIVE_LIMIT |
| `ExecutionState` | IDLE / PLACING / WAITING / CANCELING / COOLDOWN |
| `SignalReason` | long_primary / long_bid_improve / short_primary / short_ask_improve |

### 核心数据结构
| 结构 | 用途 | 关键字段 |
|------|------|----------|
| `MarketEvent` | WS 原始事件 | symbol, best_bid/ask, last_trade_price, mark_price, event_type |
| `MarketState` | 聚合后市场状态 | symbol, best_bid/ask, last/previous_trade_price, is_ready |
| `Position` | 仓位信息 | symbol, position_side, position_amt, entry_price, unrealized_pnl |
| `PositionUpdate` | 仓位更新事件（WS） | symbol, position_side, position_amt, entry_price, unrealized_pnl |
| `SymbolRules` | 交易规则 | tick_size, step_size, min_qty, min_notional |
| `ExitSignal` | 平仓信号 | symbol, position_side, reason, timestamp_ms, roi_mult, accel_mult, roi, ret_window |
| `OrderIntent` | 下单意图 | symbol, side, position_side, qty, price, reduce_only=True, is_risk, client_order_id |
| `OrderResult` | 下单结果 | success, order_id, status, filled_qty, avg_price |
| `OrderUpdate` | 订单更新事件（WS） | order_id, status, filled_qty |
| `SideExecutionState` | 执行状态机 | state, mode, current_order_id, maker/aggr 计数器 |
| `RiskFlag` | 风险标记 | is_triggered, dist_to_liq, reason |

---

## 状态机（ExecutionEngine）

```
IDLE ──(信号触发)──▶ PLACING ──(下单成功)──▶ WAITING
                                            │
                    ┌───────────────────────┤
                    │                       │
                    ▼                       ▼
            (完全成交)               (TTL 超时)
                    │                       │
                    ▼                       ▼
                 IDLE ◀──(冷却)── COOLDOWN ◀── CANCELING
```

### 执行模式轮转（已实现）

每个 `symbol + positionSide` 维护独立的 `ExecutionMode`：

- `MAKER_ONLY`：post-only 限价（`timeInForce=GTX`），按 maker 策略定价
- `AGGRESSIVE_LIMIT`：普通限价（`timeInForce=GTC`），价格更贴近立即成交方向（SELL=best_bid，BUY=best_ask）

轮转规则（由配置控制）：

- `MAKER_ONLY` 连续超时 `>= maker_timeouts_to_escalate` → 切到 `AGGRESSIVE_LIMIT`
- `AGGRESSIVE_LIMIT` 成交次数 `>= aggr_fills_to_deescalate` → 切回 `MAKER_ONLY`
- `AGGRESSIVE_LIMIT` 连续超时 `>= aggr_timeouts_to_deescalate` → 切回 `MAKER_ONLY`

### 挂单清理（已实现）

- 下单时设置 `newClientOrderId`（前缀 `<client_order_prefix>-{run_id}-`，其中 `client_order_prefix` 为固定前缀，`run_id` 每次启动自动生成）。退出时只撤销本次运行前缀挂单（优先按 symbol 拉取 openOrders，降低交易所权重），避免误撤手动订单；注意：若进程崩溃/强杀，遗留挂单不会自动清理。

### 倍数系统（已实现）

- `ret_window`：基于 `last_trade_price` 的滑动窗口回报率，用于匹配 `accel_mult`（按档位取最高满足档）
- `roi`：用初始保证金口径计算（见 `README.md`），用于匹配 `roi_mult`（按档位取最高满足档）
- `final_mult = base_lot_mult × roi_mult × accel_mult`，并受 `max_mult` 和 `max_order_notional` 约束

### 风控与限速（已实现）

- markPrice 数据源：市场 WS 订阅 `@markPrice@1s`，解析为 `MarketEvent.mark_price`；该事件**不参与** stale 判定（stale 仅由 bookTicker/aggTrade 刷新），只用于风控计算
- 软风控（Step 9.1）：`dist_to_liq = abs(mark_price - liquidation_price) / mark_price`，触发后强制至少切到 `AGGRESSIVE_LIMIT`（不进入 `MARKET` 执行模式）
- 强制风控（panic_close）：当 `dist_to_liq` 落入 `global.risk.panic_close.tiers` 任一档位时，绕过信号/节流，按 `slice_ratio` 强制分片下单（reduceOnly），maker 连续超时达到 `maker_timeouts_to_escalate` 升级为 `AGGRESSIVE_LIMIT`；TTL 固定为 `execution.order_ttl_ms × ttl_percent`
- 仓位保护性止损（Step 9.3）：为每个“有持仓”的 `symbol + positionSide` 维护交易所端 `STOP_MARKET closePosition` 条件单（`MARK_PRICE` 触发），stopPrice 基于 `liquidation_price` 与 `dist_to_liq` 阈值反推；clientOrderId 使用稳定前缀以支持重启后续管，仓位归零时自动撤销
- risk 订单优先级：`OrderIntent.is_risk=true` 的下单/撤单绕过软限速（`max_orders_per_sec`/`max_cancels_per_sec`），避免在强平风险区被限速“卡住”
- 部分成交语义：`PARTIALLY_FILLED` 视为“有成交”，重置 `timeout_count`，避免误升级执行模式
- 全局限速（Step 9.2）：`max_orders_per_sec` / `max_cancels_per_sec`（滑动窗口计数），在主流程下单/撤单前检查

### 重连后校准（已实现）

- Market/UserData WS 重连成功后回调触发一次 REST 校准：重新加载 markets/rules 并刷新仓位
- 校准期间暂停下单，避免用旧规则/旧仓位继续执行

---

## 文件结构（已实现）

```
vibe-quant/
├── .gitignore                # Git 忽略文件
├── CLAUDE.md                 # Claude Code 指引
├── requirements.txt          # Python 依赖
├── config/
│   └── config.yaml           # 配置文件（敏感信息走环境变量）
├── deploy/
│   └── systemd/              # systemd 部署产物（service/env 示例/说明）
├── logs/                     # 日志目录（.gitignore）
├── memory-bank/              # 设计文档与进度
│   ├── architecture.md       # 本文件
│   ├── design-document.md    # 设计文档
│   ├── implementation-plan.md # 实施计划
│   ├── mvp-scope.md          # MVP 范围定义
│   ├── progress.md           # 开发进度
│   └── tech-stack.md         # 技术栈
├── README.md                 # 项目说明（含 ROI/accel 口径）
├── src/
│   ├── __init__.py           # 根模块，导出所有数据结构
│   ├── models.py             # 核心数据结构（枚举 + dataclass）
│   ├── main.py               # 入口，事件循环，优雅退出
│   ├── config/
│   │   ├── __init__.py
│   │   ├── loader.py         # YAML 加载，global + symbol 覆盖
│   │   └── models.py         # pydantic 配置模型
│   ├── exchange/
│   │   ├── __init__.py
│   │   └── adapter.py        # ccxt 封装（markets/positions/下单/撤单）
│   ├── ws/
│   │   ├── __init__.py
│   │   ├── market.py         # 市场数据 WS（bookTicker + aggTrade + markPrice@1s）
│   │   └── user_data.py      # User Data Stream（ORDER_TRADE_UPDATE + ACCOUNT_UPDATE）
│   ├── signal/
│   │   ├── __init__.py
│   │   └── engine.py         # 信号判断（LONG/SHORT 触发条件）
│   ├── execution/
│   │   ├── __init__.py
│   │   └── engine.py         # 状态机（IDLE→PLACE→WAIT→CANCEL→COOLDOWN）
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── manager.py        # 风控（强平距离、全局限速）
│   │   ├── protective_stop.py # 仓位保护性止损（STOP_MARKET 条件单）
│   │   └── rate_limiter.py   # 滑动窗口限速器
│   ├── notify/
│   │   ├── __init__.py
│   │   └── telegram.py       # Telegram 通知（成交/重连/风险/开仓告警）
│   └── utils/
│       ├── __init__.py
│       ├── logger.py         # loguru 日志配置
│       └── helpers.py        # 规整函数（round_to_tick/round_up_to_tick/step）
└── tests/
    ├── __init__.py
    ├── test_config.py        # 配置模块测试（12 用例）
    ├── test_exchange.py      # 交易所适配器测试（20 用例）
    ├── test_logger.py        # 日志模块测试（26 用例）
    ├── test_main_shutdown.py # 优雅退出/资源释放测试
	    ├── test_order_cleanup.py # 退出撤单隔离测试（clientOrderId 前缀）
	    ├── test_protective_stop.py # 保护性止损（交易所端条件单）测试
	    ├── test_risk_manager.py  # 风控与限速测试
    ├── test_ws_market.py     # 市场 WS 测试（23 用例）
    ├── test_ws_user_data.py  # 用户数据 WS 测试（27 用例）
    ├── test_signal.py        # 信号引擎测试（18 用例）
    └── test_execution.py     # 执行引擎测试（41 用例）
```

### 文件详细说明

| 文件 | 行数 | 说明 |
|------|------|------|
| `src/models.py` | 300 | 核心数据结构（枚举 + dataclass），定义所有模块间传递的数据结构 |
| `src/main.py` | 1407 | Application 类，模块初始化 + 事件循环 + 优雅退出 |
| `src/config/loader.py` | 253 | ConfigLoader 类，YAML 加载 + 环境变量 + global/symbol 合并 |
| `src/config/models.py` | 272 | pydantic 配置模型，支持类型验证和默认值 |
| `src/utils/logger.py` | 434 | loguru 日志配置，按天滚动 + 结构化事件日志 |
| `src/utils/helpers.py` | 131 | round_to_tick/round_up_to_tick/round_to_step/round_up_to_step/current_time_ms/symbol 转换 |
| `src/exchange/adapter.py` | 650 | ExchangeAdapter 类，ccxt 封装（markets/positions/下单/撤单/规整函数） |
| `src/ws/market.py` | 472 | MarketWSClient 类，bookTicker/aggTrade/markPrice@1s 解析，指数退避重连，陈旧检测，重连回调 |
| `src/ws/user_data.py` | 608 | UserDataWSClient 类，listenKey 管理 + ORDER_TRADE_UPDATE/ACCOUNT_UPDATE 解析，指数退避重连，重连回调 |
| `src/signal/engine.py` | 474 | SignalEngine 类，MarketState 聚合 + LONG/SHORT 信号判断 + 节流 + accel/ROI 倍数 |
| `src/execution/engine.py` | 874 | ExecutionEngine 类，状态机 + Maker/Aggr 定价 + 超时/冷却管理 + panic_close 支持 |
| `src/risk/manager.py` | 137 | RiskManager 类，dist_to_liq 风控兜底 + orders/cancels 全局限速 |
| `src/risk/protective_stop.py` | 343 | ProtectiveStopManager 类，维护交易所端 STOP_MARKET closePosition 保护止损单 |
| `src/risk/rate_limiter.py` | 46 | SlidingWindowRateLimiter，固定窗口滑动计数限速 |
| `src/notify/telegram.py` | 236 | Telegram 通知（成交/重连/风险触发/开仓告警；token/chat_id 走 env） |

---

## 关键决策

详见 `implementation-plan.md` 的"关键决策确认"章节。

---

## 更新日志

| 日期 | 更新内容 |
|------|----------|
| 2024-12-16 | 初始架构规划，完成 Step 0.1 |
| 2024-12-16 | 完成 Step 1.1，创建目录结构和所有模块接口 |
| 2024-12-16 | 完成 Step 1.2，实现配置系统（pydantic + YAML + 合并） |
| 2024-12-16 | 完成 Step 1.3，实现日志系统（loguru + 按天滚动 + 结构化） |
| 2024-12-17 | 完成 Step 2，实现交易所适配层（ccxt + 规整函数 + 仓位读取） |
| 2024-12-17 | 完成 Step 3.1，实现市场 WS（bookTicker + aggTrade + 重连） |
| 2024-12-17 | 完成 Step 3.2，实现数据陈旧（stale）检测（任一数据流更新即重置） |
| 2024-12-17 | 完成 Step 3.3，实现用户数据 WS（listenKey + ORDER_TRADE_UPDATE） |
| 2024-12-17 | 完成 Step 4，实现信号层（MarketState + LONG/SHORT 条件 + 节流） |
| 2024-12-17 | 完成 Step 5，实现执行层（状态机 + Maker 定价 + 超时/冷却管理） |
| 2024-12-17 | 完成 MVP 主程序集成（模块协调 + 事件循环 + 优雅退出） |
| 2025-12-17 | 完成 Step 6.2：WS 重连后 REST 校准（positions + markets），校准期间暂停下单 |
| 2025-12-17 | 完成 Step 6.3：优雅退出增强（只撤销本次运行挂单、资源释放、幂等 shutdown） |
| 2025-12-17 | 完成 Step 7.1-7.2：执行模式轮转（MAKER_ONLY ↔ AGGRESSIVE_LIMIT） |
| 2025-12-17 | 完成 Step 7.3：MARKET 支持（后续移除） |
| 2025-12-17 | 完成 Step 8.1-8.3：加速/ROI 倍数 + 倍数组合（qty scaling） |
| 2025-12-17 | 完成 Step 9.1-9.2：强平距离兜底 + 全局限速 |
| 2025-12-17 | 完成 Step 10.1：Telegram 通知（成交/重连/风险触发；env 凭证） |
| 2025-12-17 | 完成 Step 11.1：systemd 部署与自动重启策略（日志持久化） |
| 2025-12-18 | 增强 Step 3.3：处理 ACCOUNT_UPDATE 实时同步仓位（PositionUpdate），清理 0 仓位避免“幽灵仓位” |
| 2025-12-18 | 优化 Telegram 通知模板：中文多行格式（平多/平空），并增加开仓/加仓告警（on_open_alert） |
| 2025-12-18 | 新增 markPrice@1s 行情与多级强制风控（panic_close：按 tiers 强制分片平仓，TTL=order_ttl_ms×ttl_percent；risk 订单绕过软限速；部分成交重置 timeout_count） |
| 2025-12-18 | 移除 MARKET/allow_market，执行模式仅保留 MAKER_ONLY ↔ AGGRESSIVE_LIMIT |
| 2025-12-18 | 新增 Step 9.3：仓位保护性止损（交易所端 STOP_MARKET closePosition，MARK_PRICE 触发） |
| 2025-12-18 | 简化 accel 配置：合并 `tiers_long`/`tiers_short` 为单一 `tiers`，LONG/SHORT 方向自动处理 |
| 2025-12-19 | 完善 Symbol 配置覆盖：symbols 可覆盖 execution/accel/roi/risk 全部字段（含 panic_close） |
| 2025-12-19 | 修复保护止损 Binance Algo API 适配：clientOrderId 唯一化（时间戳后缀）、修复 openAlgoOrders 响应格式解析（2025-12-09 迁移后为数组）、日志 Decimal 自动格式化 |

---

## 配置系统架构

### 配置层次
```
config.yaml
    │
    ├── global:           # 全局默认配置
    │   ├── ws:           # WebSocket 配置
    │   ├── execution:    # 执行配置
    │   ├── accel:        # 加速配置
    │   ├── roi:          # ROI 配置
    │   ├── risk:         # 风控配置
    │   ├── rate_limit:   # 限速配置
    │   └── telegram:     # 通知配置（token/chat_id 通过 env）
    │
    └── symbols:          # Symbol 级别覆盖（可覆盖 execution/accel/roi/risk 全部字段）
        └── BTC/USDT:USDT:
            ├── execution:  # 覆盖执行配置（全部字段可覆盖）
            ├── accel:      # 覆盖加速配置（全部字段可覆盖）
            ├── roi:        # 覆盖 ROI 配置（全部字段可覆盖）
            └── risk:       # 覆盖风控配置（全部字段可覆盖）
```

### 配置合并流程
```
                    ┌─────────────────┐
                    │   config.yaml   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  ConfigLoader   │
                    │    .load()      │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
     ┌────────────┐  ┌────────────┐  ┌────────────┐
     │ AppConfig  │  │  API Key   │  │ API Secret │
     │ (pydantic) │  │  (env var) │  │  (env var) │
     └────────────┘  └────────────┘  └────────────┘
              │
              │ get_symbol_config("BTC/USDT:USDT")
              ▼
     ┌─────────────────────────────────────────┐
     │         MergedSymbolConfig              │
     │  (global defaults + symbol overrides)  │
     └─────────────────────────────────────────┘
```

### pydantic 模型结构
| 类别 | 模型 |
|------|------|
| 子配置 | ReconnectConfig, WSConfig, ExecutionConfig, AccelConfig, AccelTier, RoiConfig, RoiTier, RiskConfig, PanicCloseConfig, PanicCloseTier, ProtectiveStopConfig, RateLimitConfig, TelegramConfig, TelegramEventsConfig |
| Symbol 覆盖 | SymbolExecutionConfig, SymbolAccelConfig, SymbolRoiConfig, SymbolRiskConfig, SymbolConfig |
| 顶层 | GlobalConfig, AppConfig |
| 运行时 | MergedSymbolConfig |

---
