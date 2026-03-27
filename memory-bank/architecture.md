<!-- Input: 系统模块、运行方式与关键约束 -->
<!-- Output: 架构与文件结构说明（含 Telegram Bot 命令控制/暂停恢复、交易所初始化诊断、按 symbol 策略模式、执行竞态/自恢复安全约束、一级风控日志降噪与 -4118 挂单占仓锁存） -->
<!-- Pos: memory-bank/architecture 总览与执行状态机约束 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# 系统架构

> 本文档描述系统的整体架构和每个文件/模块的作用。
> 随着开发进展持续更新。

---

## 项目概述

Binance U 本位永续 Hedge 模式 Reduce-Only 小单平仓执行器。

**核心目标**：通过多次小量 + 执行模式轮转 + 超时撤单，完成 Hedge 模式下 LONG/SHORT 仓位的 reduce-only 平仓（reduce-only 语义约束：`positionSide + side + qty<=position`），尽量降低滑点与市场冲击。

**运行时策略**：基于账户持仓自动发现并管理 symbols；配置中的 `symbols` 仅用于参数覆盖，不作为订阅白名单。

---

## 技术栈

- **语言**：Python 3.11+
- **异步**：asyncio
- **交易所**：ccxt (REST)
- **WebSocket**：aiohttp (WS)
- **HTTP**：aiohttp（REST + Telegram）
- **配置**：PyYAML + pydantic
- **日志**：loguru
- **通知**：aiohttp（Telegram Bot API）
- **测试**：pytest + pytest-asyncio

---

## 数据存储（当前无数据库）

- **配置**：`config/config.yaml`（YAML）
- **日志**：`logs/`（`vibe-quant_YYYY-MM-DD.log`/`error_YYYY-MM-DD.log`，旧日志 `.gz`）
- **持久化数据库**：无（当前所有状态仅在内存中维护）

---

## 模块架构（设计规划）

```

运行时流程：`fetch_positions(None)` → 生成 `active_symbols` → 仅为 active symbols 构建 WS/信号/执行引擎。

说明：运行时 `active_symbols` 由账户持仓自动发现并维护，`symbols` 不再作为订阅/执行的白名单。
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
| **WSClient** | 订阅 bookTicker + aggTrade + markPrice@1s；对 `orderbook_pressure` symbol 额外订阅 `depth10@100ms`；断线重连，重连后回调触发校准 | 配置 | MarketEvent, OrderUpdate, AlgoOrderUpdate, PositionUpdate, LeverageUpdate |
| **ExchangeAdapter** | ccxt 封装：markets/positions/balance 查询，下单/撤单（普通/条件单分离，混合场景用 cancel_any_order）；启动期 `load_markets()` 对网络类失败做有限重试，并输出 proxy/direct 诊断；在 `-4118` 后复核同侧普通平仓挂单是否已覆盖剩余可交易仓位 | 配置, OrderIntent | OrderResult, Position |
| **SignalEngine** | 按 symbol 在 `orderbook_price` / `orderbook_pressure` 两条互斥路径间评估平仓条件；维护 prev/last trade price、盘口量 dwell、active burst pacing 与来源 freshness；产出公共 ROI/accel sizing context；`orderbook_pressure` 生成带 TTL/cooldown jitter、基准片大小 jitter 与 burst pacing 元数据的 ExitSignal，并可显式启用公共倍数 | MarketEvent, Position | ExitSignal |
| **ExecutionEngine** | 复用单套状态机；支持 signal 自带 `price/ttl/cooldown/base_mult/jitter` 覆盖，并维持 reduce-only 边界；对 `orderbook_pressure` 的基准片大小在最终可下单量上应用公共 roi/accel modifiers、双边 jitter 与 recent-size anti-repeat；`-4118` 后锁存“同侧挂单占仓”状态并暂停无效重试；一级风控持续时保持 `AGGRESSIVE_LIMIT`，避免 maker/aggressive 抖动 | ExitSignal, 配置 | OrderIntent |
| **RiskManager** | 强平距离兜底（dist_to_liq）+ 全局限速（orders/cancels） | Position, MarketEvent | RiskFlag |
| **Logger** | 按天滚动日志，结构化字段 | 各模块事件 | 日志文件 |
| **Notifier** | Telegram 通知（串行发送 + retry_after 限流等待）+ Bot 命令接收（暂停/恢复/状态查询） | 关键事件、Bot 命令 | 消息推送、暂停控制 |

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
| `StrategyMode` | orderbook_price / orderbook_pressure |
| `SignalExecutionPreference` | passive / aggressive |
| `SignalReason` | long_primary / long_bid_improve / short_primary / short_ask_improve / pressure_* |

### 核心数据结构
| 结构 | 用途 | 关键字段 |
|------|------|----------|
| `MarketEvent` | WS 原始事件 | symbol, best_bid/ask, best_bid_qty/ask_qty, bid_levels/ask_levels, last_trade_price, mark_price, event_type |
| `MarketState` | 聚合后市场状态 | symbol, best_bid/ask, best_bid_qty/ask_qty, bid_levels/ask_levels, last/previous trade, source timestamps, is_ready |
| `Position` | 仓位信息 | symbol, position_side, position_amt, entry_price, unrealized_pnl |
| `PositionUpdate` | 仓位更新事件（WS） | symbol, position_side, position_amt, entry_price, unrealized_pnl |
| `LeverageUpdate` | 杠杆更新事件（WS） | symbol, leverage, timestamp_ms |
| `SymbolRules` | 交易规则 | tick_size, step_size, min_qty, min_notional |
| `ExitSignal` | 平仓信号 | symbol, position_side, reason, strategy_mode, execution_preference, price/ttl/cooldown/base_mult/jitter overrides |
| `OrderIntent` | 下单意图 | symbol, side, position_side, qty, price, reduce_only=True, is_risk, client_order_id |
| `OrderResult` | 下单结果 | success, order_id, status, filled_qty, avg_price, error_code, error_message |
| `OrderUpdate` | 订单更新事件（WS） | order_id, status, filled_qty, is_maker, realized_pnl, fee, fee_asset |
| `ReduceOnlyBlockInfo` | `-4118` 复核结果 | position_amt, tradable_position_amt, blocking_qty, own/external_blocking_* |
| `SideExecutionState` | 执行状态机 | state, mode, current_order_id, maker/aggr 计数器, signal override 上下文, liq_distance_active, reduce_only_block |
| `RiskFlag` | 风险标记 | is_triggered, dist_to_liq, reason |

### 策略模式（已实现）

- `orderbook_price`：沿用原有 trade + bookTicker 信号、ROI/accel 数量体系与模式轮转
- `orderbook_pressure`：按 symbol 显式启用；同一 symbol 上与 `orderbook_price` 互斥
  - LONG 观察 `best_bid_qty`，SHORT 观察 `best_ask_qty`
  - 顶档量连续超过阈值 `sustain_ms` 后，主动吃一档（LONG=`SELL @ best_bid`，SHORT=`BUY @ best_ask`）
  - 主动条件未成立时，仅保留 1 笔固定档位的被动单（LONG=`ask[passive_level]`，SHORT=`bid[passive_level]`）
  - 数量基准为 `min_qty × execution.base_mult`；默认继承 `execution.use_roi_mult` / `execution.use_accel_mult`，也可通过 `pressure_exit.use_roi_mult` / `pressure_exit.use_accel_mult` 显式覆盖，并继续受 `execution.max_mult`、`execution.max_order_notional` 与剩余仓位约束
  - `bookTicker` 与 `depth10` 任一来源超过 `stale_data_ms` 未刷新时，本轮跳过并重置 dwell

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
- 若 `liq_distance` 等一级风控仍处于触发态，则暂停上述 `AGGRESSIVE_LIMIT → MAKER_ONLY` 的自动降级，直到风险解除

直接吃单规则（跳过模式轮转）：

- **improve 信号**：当信号类型为 `long_bid_improve` 或 `short_ask_improve` 时，直接切换到 `AGGRESSIVE_LIMIT` 吃单（价格正在朝有利方向移动，无需等待 maker 成交）
- **风险触发（orderbook_price）**：当 `dist_to_liq` 低于风险阈值时，至少切换到 `AGGRESSIVE_LIMIT`
- **风险触发（orderbook_pressure）**：记录 risk 事件/通知，但保持 signal 自带的 `price_override` / `TIF` / 主动-被动语义；真正的强制执行由 `panic_close` 兜底

### 挂单清理（已实现）

- 下单时设置 `newClientOrderId`（前缀 `<client_order_prefix>-{run_id}-`，其中 `client_order_prefix` 为固定前缀，`run_id` 每次启动自动生成）。退出时只撤销本次运行前缀挂单（优先按 symbol 拉取 openOrders，降低交易所权重），避免误撤手动订单；注意：若进程崩溃/强杀，遗留挂单不会自动清理。
- 订单 TTL 超时后，撤单成功的订单进入 `COOLDOWN` 并保留订单上下文，直到 `ws_fill_grace_ms` 窗口结束；撤单失败的订单保留在 `WAITING` 并按短 backoff 重试撤单，避免丢失 live order 上下文后重复下单。
- 若撤单与成交/终态 WS 回执并发，且交易所返回 `-2011 Unknown order sent` / order-not-found，执行引擎按“订单已离场”处理：保留上下文进入 `COOLDOWN` 等待 grace；若发现 `WAITING/CANCELING` 但 `current_order_id` 丢失，则自动自恢复。若该侧仍保留最近成交终态上下文，则强制等待 `ws_fill_grace_ms` 以完成持仓对齐，避免用 stale `position_amt` 复用同轮信号重新下单；`panic_close` 也走同一恢复分支，但仅在不存在 recent fill 上下文时允许立刻继续兜底下单。
- 若普通平仓单收到 `-4118 ReduceOnly Order Failed`，执行引擎会立即让 `ExchangeAdapter` 复核同 symbol + 同 `positionSide` + 同平仓方向的普通挂单剩余量；当这些挂单已覆盖当前 `tradable_position_amt` 时，锁存 `reduce_only_block`，后续 signal / panic close 在复核窗口内直接跳过，直到仓位归零或挂单覆盖释放后再恢复。
- `orderbook_pressure` 的被动 `GTX` 单若收到 `-5022 post only reject`，不会自动回退为 `GTC` taker 单；被动语义保持不变，等待下一轮盘口重评估。

### 倍数系统（已实现）

- `ret_window`：基于 `last_trade_price` 的滑动窗口回报率，用于匹配 `accel_mult`（按档位取最高满足档）
- `roi`：用初始保证金口径计算（见 `README.md`），用于匹配 `roi_mult`（按档位取最高满足档）
- `roi_mult / accel_mult` 是公共 sizing modifiers；不同策略先决定自己的基准片大小，再选择是否叠加这两个倍数
- `orderbook_price` 的基准倍数是 `execution.base_mult`，默认通过 `execution.use_roi_mult` / `execution.use_accel_mult` 启用公共倍数
- `orderbook_pressure` 的基准倍数也是 `execution.base_mult`，默认继承 `execution.use_roi_mult` / `execution.use_accel_mult`；若在 `pressure_exit.use_*` 显式配置，则以 pressure 自己的值为准
- 最终倍数受 `max_mult` 约束；两条主策略的最终 qty 都受 `max_order_notional` 约束

### 风控与限速（已实现）

- markPrice 数据源：市场 WS 订阅 `@markPrice@1s`，解析为 `MarketEvent.mark_price`；该事件**不参与** stale 判定（stale 仅由 bookTicker/aggTrade 刷新），只用于风控计算
- 一级风控（Step 9.1）：`dist_to_liq = abs(mark_price - liquidation_price) / mark_price`
  - `orderbook_price`：触发后强制至少切到 `AGGRESSIVE_LIMIT`（不进入 `MARKET` 执行模式）
    - 同一 `symbol + positionSide + risk_stage` 只在“进入风险区”时记录一次 `[RISK]` warning，并在风险解除时记录一次 recovery info；持续风险期间不重复刷 warning/Telegram
    - 风险持续期间，执行引擎保持 `AGGRESSIVE_LIMIT`，不因 aggressive fill/timeout 自动降回 `MAKER_ONLY`
  - `orderbook_pressure`：触发后记录 risk 事件/通知，但不把未达阈值的被动单改成主动单
- 强制风控（panic_close）：当 `dist_to_liq` 落入 `global.risk.panic_close.tiers` 任一档位时，绕过信号/节流，按 `slice_ratio` 强制分片下单（reduce-only 语义约束，不下发 `reduceOnly`），maker 连续超时达到 `maker_timeouts_to_escalate` 升级为 `AGGRESSIVE_LIMIT`；TTL 固定为 `execution.order_ttl_ms × ttl_percent`
- 仓位保护性止损（Step 9.3）：为每个”有持仓”的 `symbol + positionSide` 维护交易所端 `STOP_MARKET closePosition` 条件单（`MARK_PRICE` 触发），stopPrice 基于 `liquidation_price` 与 `dist_to_liq` 阈值反推；clientOrderId 使用稳定前缀以支持重启后续管，仓位归零时自动撤销；交叉保证金下若爆仓价方向异常（如 SHORT 的 liq_price < mark_price，因对冲方向导致）则跳过该侧保护止损
- 外部止损接管（Step 9.3 扩展）：当检测到同侧存在**外部 stop/tp 条件单**（满足 `STOP/TAKE_PROFIT*` 且 `reduceOnly=true` 字段；或 `closePosition=true` 字段 兜底）时，视为外部接管：撤销我方保护止损并暂停维护。
  - 外部止损有效性检查：若外部止损价接近/劣于爆仓价（0.01% 以内），视为无效并取消；仅当不存在有效外部止损时由程序接管并重新挂单。
  - REST 校验以 raw openOrders（`GET /fapi/v1/openOrders`）为主，必要时回退 ccxt openOrders，并合并 openAlgoOrders，避免 ccxt 漏掉部分 closePosition 条件单。
  - 多外部单并存时，WS 收到某一张终态不代表接管结束：先标记 pending，并触发一次 REST verify，只有 verify 确认“同侧外部 stop/tp 已不存在”才 release 并恢复自维护。
- risk 订单优先级：`OrderIntent.is_risk=true` 的下单/撤单绕过软限速（`max_orders_per_sec`/`max_cancels_per_sec`），避免在强平风险区被限速“卡住”
- 部分成交语义：`PARTIALLY_FILLED` 视为“有成交”，重置 `timeout_count`，避免误升级执行模式
- 成交日志：以 WS 成交回执为准输出 `role=maker|taker`；REST 立即成交仅完成状态并缓存 `order_id`，在 `ws_fill_grace_ms` 窗口内接收迟到 WS 回执补打日志，超时后先通过 REST 查询 maker 状态、已实现盈亏与手续费；查询成功则输出 `maker/taker`、`pnl`、`fee`，失败才回退为 `role=unknown`
- 全局限速（Step 9.2）：`max_orders_per_sec` / `max_cancels_per_sec`（滑动窗口计数），在主流程下单/撤单前检查

- 保护止损同步调度：`_schedule_protective_stop_sync` 采用两阶段取消策略——debounce sleep 阶段可被新调度安全取消（合并触发），REST 执行阶段不取消（避免幽灵单/状态丢失），新任务等前任务完成后再执行，保证同一 symbol 串行
- 保护止损 adoption 日志：`_sync_side` 发现既有订单且本地状态缺失时打 info 日志（`adopt_existing`/`keep_existing_tighter`），本地状态已存在时静默（避免刷屏）
- A（事件触发刷新）：User Data `ACCOUNT_UPDATE` 解析账户事件（reason + 余额变动 + 是否含仓位变动），命中保证金相关 reason，或“余额变动但无仓位变动”时调度一次全量仓位 REST 刷新（去抖后执行）
- B（低频兜底刷新）：主循环新增低频全量仓位刷新任务（默认 300s），用于兜底更新 `liquidation_price`
- C（受控放松止损）：保护止损在“爆仓价改善超过阈值 + 冷却窗口满足”时允许放松（LONG 下调 / SHORT 上调）；其余场景保持“只收紧”

### 重连后校准（已实现）

- Market/UserData WS 重连成功后回调触发一次 REST 校准：重新加载 markets/rules 并刷新仓位
- 校准期间暂停下单，避免用旧规则/旧仓位继续执行

---

## 文件结构（已实现）

说明：各目录包含 README.md 作为目录级说明。

```
vibe-quant/
├── .gitignore                # Git 忽略文件
├── CLAUDE.md                 # Claude Code 指引
├── requirements.txt          # Python 依赖
├── config/
│   ├── README.md             # config 目录说明
│   └── config.yaml           # 配置文件（敏感信息走环境变量）
├── deploy/
│   ├── README.md             # deploy 目录说明
│   └── systemd/              # systemd 部署产物（service/env 示例/说明）
├── logs/                     # 日志目录（含 README.md）
├── memory-bank/              # 设计文档与进度（含 README.md）
│   ├── README.md             # memory-bank 目录说明
│   ├── architecture.md       # 本文件
│   ├── design-document.md    # 设计文档
│   ├── implementation-plan.md # 实施计划
│   ├── mvp-scope.md          # MVP 范围定义
│   ├── progress.md           # 开发进度
│   └── tech-stack.md         # 技术栈
├── README.md                 # 项目说明（含 ROI/accel 口径）
├── src/
│   ├── README.md             # src 目录说明
│   ├── __init__.py           # 根模块，导出所有数据结构
│   ├── models.py             # 核心数据结构（枚举 + dataclass）
│   ├── main.py               # 入口，事件循环，优雅退出
│   ├── config/
│   │   ├── README.md         # src/config 目录说明
│   │   ├── __init__.py
│   │   ├── loader.py         # YAML 加载，global + symbol 覆盖
│   │   └── models.py         # pydantic 配置模型
│   ├── exchange/
│   │   ├── README.md         # src/exchange 目录说明
│   │   ├── __init__.py
│   │   └── adapter.py        # ccxt 封装（markets/positions/下单/撤单：普通/条件单分离）
│   ├── ws/
│   │   ├── README.md         # src/ws 目录说明
│   │   ├── __init__.py
│   │   ├── market.py         # 市场数据 WS（bookTicker + aggTrade + markPrice@1s）
│   │   └── user_data.py      # User Data Stream（ORDER_TRADE_UPDATE + ALGO_UPDATE + ACCOUNT_UPDATE）
│   ├── signal/
│   │   ├── README.md         # src/signal 目录说明
│   │   ├── __init__.py
│   │   └── engine.py         # 信号判断（LONG/SHORT 触发条件）
│   ├── execution/
│   │   ├── README.md         # src/execution 目录说明
│   │   ├── __init__.py
│   │   └── engine.py         # 状态机（IDLE→PLACE→WAIT→CANCEL→COOLDOWN）
│   ├── risk/
│   │   ├── README.md         # src/risk 目录说明
│   │   ├── __init__.py
│   │   ├── manager.py        # 风控（强平距离、全局限速）
│   │   ├── protective_stop.py # 仓位保护性止损（STOP_MARKET 条件单）
│   │   └── rate_limiter.py   # 滑动窗口限速器
│   ├── notify/
│   │   ├── README.md         # src/notify 目录说明
│   │   ├── __init__.py
│   │   ├── telegram.py       # Telegram 通知（成交/重连/风险/开仓告警）
│   │   ├── bot.py            # Telegram Bot 命令接收器（getUpdates long polling）
│   │   └── pause_manager.py  # 暂停状态管理器（全局/per-symbol，支持定时暂停）
│   ├── stats/
│   │   ├── README.md         # src/stats 目录说明
│   │   ├── __init__.py
│   │   ├── market_recorder.py # 原始市场数据录制（bookTicker/depth10/aggTrade）
│   │   └── pressure_stats.py # orderbook_pressure 旁路统计（触发频率/成功下单/首次成交/价格走势）
│   └── utils/
│       ├── README.md         # src/utils 目录说明
│       ├── __init__.py
│       ├── logger.py         # loguru 日志配置
│       └── helpers.py        # 规整函数（round_to_tick/round_up_to_tick/step）
└── tests/
    ├── README.md             # tests 目录说明
    ├── __init__.py
    ├── test_config.py        # 配置模块测试（12 用例）
    ├── test_exchange.py      # 交易所适配器测试（20 用例）
    ├── test_logger.py        # 日志模块测试（26 用例）
    ├── test_main_shutdown.py # 优雅退出/资源释放测试
	    ├── test_order_cleanup.py # 退出撤单隔离测试（clientOrderId 前缀）
	    ├── test_protective_stop.py # 保护性止损（交易所端条件单）测试（34 用例）
	    ├── test_risk_manager.py  # 风控与限速测试
    ├── test_ws_market.py     # 市场 WS 测试（23 用例）
    ├── test_ws_user_data.py  # 用户数据 WS 测试（27 用例）
    ├── test_signal.py        # 信号引擎测试（18 用例）
    ├── test_pressure_stats.py # 盘口量统计收集器测试
    ├── test_pause_manager.py # PauseManager 测试（29 用例）
    ├── test_telegram_bot.py  # TelegramBot 测试（14 用例）
    └── test_execution.py     # 执行引擎测试（83 用例）
```

### 文件详细说明

| 文件 | 行数 | 说明 |
|------|------|------|
| `src/models.py` | 460 | 核心数据结构（枚举 + dataclass），定义所有模块间传递的数据结构 |
| `src/main.py` | 3044 | Application 类，模块初始化 + 事件循环 + 优雅退出 |
| `src/config/loader.py` | 270 | ConfigLoader 类，YAML 加载 + 环境变量 + global/symbol 合并 |
| `src/config/models.py` | 312 | pydantic 配置模型，支持类型验证和默认值 |
| `src/utils/logger.py` | 483 | loguru 日志配置，按天滚动 + 结构化事件日志 |
| `src/utils/helpers.py` | 159 | round_to_tick/round_up_to_tick/round_to_step/round_up_to_step/current_time_ms/symbol 转换 |
| `src/exchange/adapter.py` | 1200 | ExchangeAdapter 类，ccxt 封装（markets/positions/下单/撤单/规整函数） |
| `src/ws/market.py` | 572 | MarketWSClient 类，bookTicker/aggTrade/markPrice@1s 解析，指数退避重连，陈旧检测，重连回调 |
| `src/ws/user_data.py` | 744 | UserDataWSClient 类，listenKey 管理 + ORDER_TRADE_UPDATE/ALGO_UPDATE/ACCOUNT_UPDATE 解析，指数退避重连，重连回调 |
| `src/signal/engine.py` | 471 | SignalEngine 类，MarketState 聚合 + LONG/SHORT 信号判断 + 节流 + accel/ROI 倍数 |
| `src/stats/market_recorder.py` | 245 | MarketDataRecorder，原始市场数据录制（采样/日切/gzip/保留清理） |
| `src/stats/pressure_stats.py` | 320 | PressureStatsCollector，orderbook_pressure 旁路统计（trigger/成功下单/首次成交/价格窗口聚合） |
| `src/execution/engine.py` | 1705 | ExecutionEngine 类，状态机 + Maker/Aggr 定价 + 超时/冷却管理 + panic_close 支持 |
| `src/risk/manager.py` | 142 | RiskManager 类，dist_to_liq 风控兜底 + orders/cancels 全局限速 |
| `src/risk/protective_stop.py` | 848 | ProtectiveStopManager 类，维护交易所端 STOP_MARKET closePosition 保护止损单 |
| `src/risk/rate_limiter.py` | 51 | SlidingWindowRateLimiter，固定窗口滑动计数限速 |
| `src/notify/telegram.py` | 241 | Telegram 通知（成交/重连/风险触发/开仓告警；token/chat_id 走 env） |
| `src/notify/bot.py` | 200 | TelegramBot 类，getUpdates long polling 命令接收器 |
| `src/notify/pause_manager.py` | 227 | PauseManager 类，全局/per-symbol 暂停状态管理，支持定时暂停自动恢复 |

---

## 关键决策

详见 `implementation-plan.md` 的"关键决策确认"章节。

---

## 更新日志

| 日期 | 更新内容 |
|------|----------|
| 2026-01-18 | 启用运行时持仓自动发现：active_symbols 作为唯一运行时来源，symbols 仅作为配置覆盖 |
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
| 2025-12-19 | 修复保护止损 Binance Algo API 适配：clientOrderId 唯一化（7天内唯一）、修复 openAlgoOrders 响应格式解析、外部止损单检测（避免 -4130）、日志 Decimal 自动格式化 |
| 2025-12-20 | 增强用户数据 WS：解析 ALGO_UPDATE 与 ORDER_TRADE_UPDATE 的 closePosition(cp) 字段，并在外部条件单状态变化时触发保护止损同步（外部接管/自动恢复） |
| 2025-12-20 | 保护性止损策略增强：只允许"收紧"止损（不放松），同步调度采用分级 debounce（position_update 1s，startup/calibration 0s，其余 0.2s）；外部接管采用锁存 + REST 保险丝（可配），并在启动/接管时打印外部单摘要与外部多单告警 |
| 2025-12-21 | Bug 修复：外部接管 release 后立即触发 resync（避免保护止损 52s 空档）；新增杠杆同步功能（LeverageUpdate + ACCOUNT_CONFIG_UPDATE 解析 + REST positionRisk 启动校准） |
| 2025-12-23 | Bug 修复：WS 重连 guard 与撤单超时后的状态机恢复（COOLDOWN 保留订单上下文） |
| 2025-12-23 | 新增外部止损有效性检查：无效外部止损取消并由程序接管 |
| 2026-02-21 | 新增 Telegram Bot 命令控制：/pause、/resume、/status、/help（PauseManager + TelegramBot + main.py 集成） |
| 2026-02-23 | 修复保护止损 -2021 错误：交叉保证金下爆仓价方向异常时跳过该侧保护止损 |
| 2026-02-23 | 修复保护止损同步调度竞态：两阶段取消策略（debounce 阶段可取消、执行阶段不取消），补 adoption 条件日志 |
| 2026-03-05 | 新增 A+B+C：ACCOUNT_UPDATE 账户事件触发仓位刷新 + 低频全量仓位刷新兜底 + 爆仓价改善阈值下的受控放松保护止损 |

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
| 子配置 | ReconnectConfig, WSConfig, ExecutionConfig, AccelConfig, AccelTier, RoiConfig, RoiTier, RiskConfig, PanicCloseConfig, PanicCloseTier, ProtectiveStopConfig, RateLimitConfig, TelegramConfig, TelegramEventsConfig, TelegramBotConfig |
| Symbol 覆盖 | SymbolExecutionConfig, SymbolAccelConfig, SymbolRoiConfig, SymbolRiskConfig, SymbolConfig |
| 顶层 | GlobalConfig, AppConfig |
| 运行时 | MergedSymbolConfig |

---
