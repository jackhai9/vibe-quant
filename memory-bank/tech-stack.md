<!-- Input: 技术选型依据 -->
<!-- Output: 技术栈清单 -->
<!-- Pos: memory-bank/tech-stack -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->

# 技术栈

> 本文档记录项目的技术选型及理由。
> 选型原则：最简单但最健壮，优先选择成熟稳定的库。

---

## 语言与运行时

| 技术 | 版本 | 理由 |
|------|------|------|
| **Python** | 3.11+ | 异步生态成熟，ccxt 官方支持，开发效率高 |
| **asyncio** | 标准库 | 事件驱动架构核心，支持高并发 I/O |

---

## 核心依赖

### 交易所交互

| 库 | 版本 | 用途 | 理由 |
|----|------|------|------|
| **ccxt** | >=4.0.0 | REST API（markets/positions/下单/撤单） | 统一接口，支持 100+ 交易所，Binance Futures 支持完善 |
| **aiohttp** | >=3.9.0 | WS（行情+用户数据）+ listenKey 管理 + Telegram 通知 | 统一 HTTP/WS，asyncio 原生支持，代理配置简单 |

### 配置管理

| 库 | 版本 | 用途 | 理由 |
|----|------|------|------|
| **PyYAML** | >=6.0 | YAML 配置文件解析 | 配置可读性好，支持注释 |
| **pydantic** | >=2.0.0 | 配置模型验证、类型检查、默认值 | 类型安全，自动验证，IDE 支持好 |
| **python-dotenv** | >=1.0.0 | 环境变量加载（.env 文件） | 敏感信息与配置分离 |

### 日志

| 库 | 版本 | 用途 | 理由 |
|----|------|------|------|
| **loguru** | >=0.7.0 | 日志记录（按天滚动、结构化） | 开箱即用，配置简单，支持彩色输出和文件滚动 |

### 测试

| 库 | 版本 | 用途 | 理由 |
|----|------|------|------|
| **pytest** | >=8.0.0 | 单元测试框架 | Python 社区标准，插件丰富 |
| **pytest-asyncio** | >=0.23.0 | 异步测试支持 | pytest 官方异步插件 |

---

## 未使用的备选方案

| 备选 | 未选理由 |
|------|----------|
| `binance-futures-connector` | ccxt 已足够，减少依赖数量 |
| `python-telegram-bot` | aiohttp 直连 Bot API 更轻量，无需额外依赖 |
| `uvloop` | 暂不需要极致性能，标准 asyncio 足够 |
| `orjson` | 标准 json 模块足够，消息量不大 |

---

## 架构决策

### 为什么用 ccxt + aiohttp WS 而不是 binance-futures-connector？

1. **ccxt 优势**：
   - REST API 封装完善，统一接口
   - 市场规则（tickSize/stepSize/minQty）自动解析
   - 社区活跃，文档丰富

2. **aiohttp WS 优势**：
   - asyncio 原生接口，和主流程无缝集成
   - HTTP 与 WS 统一（listenKey 管理 + WS 订阅共用一套依赖）
   - 代理支持更直接（HTTP/HTTPS 代理参数可复用）

### 为什么用 aiohttp 而不是 python-telegram-bot？

- Telegram Bot API 本质是 HTTP 调用
- aiohttp 已用于 WS + listenKey 管理，复用即可
- python-telegram-bot 功能过重（支持 Webhook、Handler 等），我们只需要发消息

### 为什么用 pydantic 而不是 dataclasses？

- pydantic 提供运行时类型验证
- 支持 `Field(default=..., gt=0)` 等约束
- YAML 配置直接解析为 pydantic 模型，自动验证

---

## 依赖安装

```bash
pip install -r requirements.txt
```

---

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `BINANCE_API_KEY` | ✅ | Binance API Key |
| `BINANCE_API_SECRET` | ✅ | Binance API Secret |
| `TELEGRAM_BOT_TOKEN` | ❌ | Telegram Bot Token（启用通知时需要） |
| `TELEGRAM_CHAT_ID` | ❌ | Telegram Chat ID（启用通知时需要） |
| `VQ_LOG_DIR` | ❌ | 日志目录（默认 `./logs`） |

---

## 版本兼容性

- Python 3.11+ 必需（使用了 `asyncio.TaskGroup`、`typing` 新特性）
- ccxt 4.x 与 3.x API 有差异，锁定 4.x
- pydantic 2.x 与 1.x 不兼容，锁定 2.x
- aiohttp 3.9+ 提供稳定的 ws_connect/heartbeat/timeout 支持

---
