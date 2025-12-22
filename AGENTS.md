# AGENTS.md

This file provides authoritative guidance for AI coding agents (e.g., OpenAI Codex and similar systems) when working with code in this repository.

## Project Overview

vibe-quant is a Binance USDT-Margined perpetual futures position executor designed for **Hedge Mode reduce-only closing**. It uses ccxt + WebSocket to execute small-lot position exits with minimal market impact through execution mode rotation (maker → aggressive limit).

**Key constraints:**

- All orders must be `reduceOnly=True`
- Hedge mode requires `positionSide=LONG/SHORT`
- Target latency: < 200ms end-to-end

## Architecture

### Core Modules
- **ConfigManager**: Global defaults + per-symbol overrides, optional hot reload
- **ExchangeAdapter** (ccxt): Markets/positions/balance fetch, order placement/cancellation
- **WSClient**: Subscribes to trade + best bid/ask streams
- **SignalEngine**: Exit condition evaluation, sliding window returns, multiplier calculation
- **ExecutionEngine**: Per-side state machine, mode rotation, order management
- **RiskManager**: Liquidation distance fallback, stale data protection, rate limiting
- **Logger**: Daily rotating logs
- **Notifier**: Telegram notifications

### Execution Mode Rotation
Per `symbol + positionSide`, maintains state machine: `IDLE → PLACE → WAIT → (FILLED|TIMEOUT) → CANCEL → COOLDOWN → IDLE`

Two execution modes:
1. **MAKER_ONLY**: Post-only limit (GTX), timeout cancel
2. **AGGRESSIVE_LIMIT**: Limit closer to execution direction, no post-only

### Signal Conditions
- LONG exit: `last_trade > prev_trade && best_bid >= last_trade` OR bid improvement
- SHORT exit: `last_trade < prev_trade && best_ask <= last_trade` OR ask improvement
- Acceleration: Sliding window return triggers larger position slices

### Quantity Calculation
`final_mult = base_mult × roi_mult × accel_mult` (capped by `max_mult` and `max_order_notional`)

Completion: Position is done when remaining quantity rounds to 0 via `stepSize` or is below `minQty`.

## Configuration

YAML-based with global defaults and per-symbol overrides. Key sections:
- `global.execution`: TTL, cooldown, mode rotation thresholds, pricing strategy
- `global.accel`: Sliding window acceleration tiers
- `global.roi`: ROI-based multiplier tiers
- `global.risk`: Liquidation distance thresholds
- `global.rate_limit`: Orders/cancels per second limits
- `symbols.<SYMBOL>`: Per-symbol overrides

## Tech Stack (Planned)

- **Language**: Python 3.11+
- **Async**: asyncio
- **Exchange**: ccxt (REST) + binance-futures-connector or websocket-client (WS)
- **Config**: PyYAML + pydantic
- **Logging**: loguru
- **Notifications**: python-telegram-bot
- **Testing**: pytest + pytest-asyncio

# 核心工作规则

## 0. 关键约束（违反即任务失败）
- **必须使用中文回复**：所有解释和交互必须使用中文
- **文档即代码 (Docs-as-Code)**：功能、架构或代码的更新必须在工作结束前同步更新相关文档
- **安全红线**：禁止生成恶意代码，必须通过基础安全检查
- **先讨论后编码**：不明白的地方反问我，先不着急编码，需求澄清完成、关键假设达成一致后，方可进入编码

## 1. 动态文档架构体系 (Fractal Documentation System)
> 核心原则：系统必须具备"自我描述性"，任何代码变更必须反映在以下三个层级中

### 1.1 根目录文档 (Root MD)
- 任何功能、架构、写法更新，必须在工作结束后更新主目录的相关子文档

### 1.2 文件夹级文档 (Folder MD)
- 每个文件夹下应有 `README.md`
- 内容：极简架构说明（3行内）+ 文件清单（名字/地位/功能）
- **自维护声明**：文件开头必须声明："一旦我所属的文件夹有所变化，请更新我。"

### 1.3 文件级注释（File Header）
每个文件开头应包含三行极简注释：
- `Input`: 依赖外部的什么（参数/模块/数据）
- `Output`: 对外提供什么（API/组件/结果）
- `Pos`: 在系统中的地位是什么
- **自维护声明**：注释后必须声明："一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。"

**例外（无需文件头注释）**：
- 代理指引/协作约束文件：`AGENTS.md`、`CLAUDE.md`
- 运行时本地配置：`config/*.yaml`、`.env`（含 `.env.*`）
- 自动生成目录/文件：`.pytest_cache/`、`logs/`、`__pycache__/`、`*.log`、`*.log.gz`
- 其他明确标注“不要改动/自动生成”的文件

## 2. 开发规范
- 强调模块化（多文件），避免单体巨型文件
- 编码前必须阅读 memory-bank/architecture.md 和 memory-bank/design-document.md
- 完成重要功能或里程碑后，更新 memory-bank/architecture.md（含每个文件的作用说明）和 memory-bank/progress.md（记录做了什么以便知晓进度）
- 个人项目：**No backward compatibility** - 可自由打破旧格式，重构时可移除 legacy 代码

## 3. Markdown 编写规范
- **换行**：只有在同一段落内需要强制换行才用 `<br>`；列表项（- / 1.）本身就会换行，不应再加 `<br>`
- **最后更新**：仅 `README.md` 与 `docs/` 下文档末尾保留 `*最后更新: YYYY-MM-DD*`（`memory-bank/` 不要求）

## 4. 安全审计
- ultrathink：完整研究这个项目，重点看其是否有安全问题，是否有密钥泄漏。列出其对外交互的所有ur和接口。