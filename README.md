<!-- Input: 项目概述与使用方式 -->
<!-- Output: 使用说明与快速上手（含执行反馈与自动发现持仓说明） -->
<!-- Pos: 项目根 README -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->

# vibe-quant

Binance U 本位永续合约 **Hedge 模式 Reduce-Only 平仓执行器**。

通过小单分批 + 按 symbol 的策略模式选择（`orderbook_price` / `orderbook_pressure`）+ 执行模式轮转（Maker → Aggressive Limit）+ 多级风控，实现低滑点、低市场冲击的仓位退出。

术语约定：本文档中“reduce-only”指 **reduce-only 语义约束**（`positionSide + side + qty<=position`），不指必须下发的交易所参数 `reduceOnly`。

## 核心特性

- **Hedge 模式专用**：不强制下发 `reduceOnly`（交易所限制），reduce-only 语义由 `positionSide + side + qty<=position` 约束保证；支持 `positionSide=LONG/SHORT`
- **双策略模式**：每个 symbol 可独立选择 `orderbook_price` 或 `orderbook_pressure`；同一 symbol 上两种模式互斥
- **执行模式轮转**：Maker 挂单优先，超时自动升级为 Aggressive Limit
- **智能倍数系统**：ROI + 加速度双倍数叠加，动态调整单笔数量
- **成交率反馈**：根据 maker 成交率动态调整升级阈值
- **多级风控**：`orderbook_price` 的一级风控执行升级 + 全模式 `panic_close` 强制分片 + 交易所端保护止损
- **实时数据**：WebSocket 订阅 bookTicker / aggTrade / markPrice / User Data Stream；`orderbook_pressure` symbol 额外订阅 `depth10@100ms`
- **Telegram 通知**：成交、重连、风险触发、开仓告警
- **撤单分层**：普通/条件单分离，混合场景提供 cancel_any_order
- **自动发现持仓**：运行时按账户持仓自动接管，`symbols` 仅用于参数覆盖

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                         main.py                             │
│                    (入口 + 事件循环)                          │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐     ┌──────────────┐     ┌───────────────┐
│ ConfigManager│     │   WSClient   │     │ExchangeAdapter│
│  (配置加载)   │     │  (WS 数据流)  │     │  (ccxt REST)  │
└──────────────┘     └──────────────┘     └───────────────┘
                              │
                              ▼
                     ┌──────────────┐
                     │ SignalEngine │
                     │   (信号判断)  │
                     └──────────────┘
                              │
                              ▼
                     ┌───────────────┐
                     │ExecutionEngine│
                     │ (状态机+下单)   │
                     └───────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ RiskManager  │     │    Logger    │     │   Notifier   │
│ (风控兜底)    │     │   (日志滚动)   │     │  (Telegram)  │
└──────────────┘     └──────────────┘     └──────────────┘
```

## 快速开始

### 环境要求

- Python 3.11+
- Binance 合约账户（Hedge 模式）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置环境变量

```bash
# 必需
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"

# 可选（Telegram 通知）
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

### 编辑配置文件

```bash
# 复制示例配置
cp config/config.example.yaml config/config.yaml

# 编辑配置，按需设置参数；symbols 可选，仅用于覆盖
vim config/config.yaml
```

### 启动

```bash
python -m src.main
```

默认读取 `config/config.yaml`，也可指定其他配置文件：

```bash
python -m src.main path/to/config.yaml
```

**macOS 防睡眠**：使用 `caffeinate` 防止系统睡眠导致程序中断：

```bash
caffeinate -is python -m src.main
```

- `-i` 防止系统空闲睡眠
- `-s` 防止系统睡眠（接通电源时）

> 生产环境建议使用 systemd 部署（见 [部署指南](docs/deployment.md)），服务器不存在睡眠问题。

## 文档

| 文档 | 说明 |
|------|------|
| [配置参数手册](docs/configuration.md) | 完整配置参数说明与调优建议 |
| [部署指南](docs/deployment.md) | 本地开发、systemd、Docker 部署 |
| [故障排查](docs/troubleshooting.md) | 常见问题与解决方案 |
| [系统架构](memory-bank/architecture.md) | 详细架构设计与模块说明 |
| [开发进度](memory-bank/progress.md) | 开发里程碑与变更记录 |

## 项目结构

```
vibe-quant/
├── config/
│   └── config.yaml          # 配置文件
├── src/
│   ├── main.py              # 入口，事件循环，优雅退出
│   ├── models.py            # 核心数据结构
│   ├── config/              # 配置加载与验证
│   ├── exchange/            # ccxt 交易所适配
│   ├── ws/                  # WebSocket（市场数据 + 用户数据流）
│   ├── signal/              # 信号判断引擎
│   ├── execution/           # 执行状态机
│   ├── risk/                # 风控（强平距离、限速、保护止损）
│   ├── notify/              # Telegram 通知
│   └── utils/               # 日志、辅助函数
├── tests/                   # 单元测试
├── docs/                    # 用户文档
├── deploy/                  # 部署配置（systemd）
└── memory-bank/             # 设计文档
```

## 策略模式

每个 `symbol` 可通过 `symbols.<symbol>.strategy.mode` 选择互斥的信号路径：

| 模式 | 行情来源 | 下单语义 | 数量语义 |
|------|----------|----------|----------|
| `orderbook_price` | `aggTrade` + `bookTicker` | 沿用原有 primary / improve 触发与执行模式轮转 | `base_mult × roi_mult × accel_mult` |
| `orderbook_pressure` | `bookTicker` 的 `B/A` + `depth10@100ms` | 达到顶档量阈值后主动吃一档；未达阈值时仅挂 1 笔固定档位被动单 | `min_qty × base_mult` |

`orderbook_pressure` 运行补充：
- 需要同时配置 `strategy.mode: orderbook_pressure` 和 `pressure_exit`
- `pressure_exit.enabled` 缺省为 `true`；若显式设为 `false`，配置校验会直接拒绝启动
- `bookTicker` 与 `depth10` 任一来源超过 `stale_data_ms` 未刷新，该模式本轮跳过，并重置主动条件 dwell

## 执行模式

每个 `symbol + positionSide` 维护独立的执行状态机：

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

**两种执行模式**：

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `MAKER_ONLY` | Post-only 限价（GTX），享受 Maker 费率 | 默认模式，流动性充足时 |
| `AGGRESSIVE_LIMIT` | 普通限价（GTC），价格更贴近成交方向 | Maker 连续超时后自动升级 |

补充：`long_bid_improve` / `short_ask_improve` 信号触发时，系统会直接切换到 `AGGRESSIVE_LIMIT` 并在同一轮信号内提交限价单（仍可能因盘口变化未立即成交）。

`orderbook_pressure` 复用同一套执行状态机，但信号会自带 `price_override` / `ttl_override_ms` / `cooldown_override_ms` / `base_mult_override` / `qty_jitter_pct`：
- 主动条件成立：LONG 下 `SELL @ best_bid`，SHORT 下 `BUY @ best_ask`
- 主动条件未成立：LONG 挂 `ask[passive_level]`，SHORT 挂 `bid[passive_level]`
- 被动单保持 `GTX` 语义；若收到 `-5022 post only reject`，不会自动降级成 taker 单

## 倍数系统

最终下单数量 = `base_mult × roi_mult × accel_mult`（受 `max_mult` 和 `max_order_notional` 约束）

### ROI 倍数（`roi_mult`）

基于初始保证金计算：

```
notional = abs(position_amt) × entry_price
initial_margin = notional / leverage
roi = unrealized_pnl / initial_margin
```

ROI 为比例值：`0.10` 表示 10%。

### 加速倍数（`accel_mult`）

基于滑动窗口回报率：

```
ret_window = (price_now / price_window_ago) - 1
```

价格快速变动时自动放大单笔数量，加速平仓。

## 风控机制

| 层级 | 触发条件 | 行为 |
|------|----------|------|
| **一级风控（orderbook_price）** | `dist_to_liq` < 阈值，且已有 `orderbook_price` 信号 | 至少升级为 `AGGRESSIVE_LIMIT` |
| **一级风控（orderbook_pressure）** | `dist_to_liq` < 阈值，且已有 `orderbook_pressure` 信号 | 记录风险事件/通知，但不把未达阈值的被动单改写成主动单 |
| **强制平仓** | `dist_to_liq` 进入 panic_close 档位 | 绕过信号，按 slice_ratio 强制分片平仓 |
| **保护止损** | 交易所端 STOP_MARKET | 程序崩溃/断网时最后防线 |
| **外部接管** | 同侧存在外部 stop/tp（`closePosition=true` 字段 或 `reduceOnly=true` 字段） | 撤销我方保护止损并暂停维护，直到外部单消失 |

`dist_to_liq = abs(mark_price - liquidation_price) / mark_price`

对 `orderbook_pressure` 来说，真正的强制执行兜底是 `panic_close`；`liq_distance_threshold` 不负责改写其主动/被动语义。

## 开发

### 运行测试

```bash
# 全部测试
pytest

# 指定模块
pytest tests/test_execution.py -v

# 带覆盖率
pytest --cov=src --cov-report=term-missing
```

### 类型检查

```bash
pyright src/
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 异步 | asyncio |
| 交易所 REST | ccxt |
| WebSocket | aiohttp |
| HTTP | aiohttp |
| 配置 | PyYAML + pydantic |
| 日志 | loguru |
| 通知 | aiohttp（Telegram Bot API）|
| 测试 | pytest + pytest-asyncio |

## License

MIT
