<!-- Input: 配置项与环境变量 -->
<!-- Output: 配置说明与调优建议 -->
<!-- Pos: 文档/配置参数手册 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# 配置参数手册

> vibe-quant 详细配置参数说明与调优建议

---

## 概述

vibe-quant 使用 YAML 配置文件（`config/config.yaml`）管理所有运行参数。配置分为两个层级：

- **global**：全局默认配置，适用于所有交易对
- **symbols**：按交易对覆盖配置，优先级高于 global

配置加载使用 pydantic 进行类型验证，确保参数合法性。

---

## 环境变量

除了 YAML 配置文件，系统还需要以下环境变量（通过 `.env` 文件或 systemd 的 `EnvironmentFile` 提供）：

### 必需环境变量

#### BINANCE_API_KEY
- **类型**: `string`
- **说明**: 币安 API Key（需要期货交易权限）
- **获取方式**: 币安官网 → 账户管理 → API 管理 → 创建 API Key
- **权限要求**: 启用"期货交易"权限，建议**不启用**提现权限

#### BINANCE_API_SECRET
- **类型**: `string`
- **说明**: 币安 API Secret（与 API Key 配对）
- **安全提示**:
  - 该密钥具有交易权限，务必妥善保管
  - 不要提交到 git 仓库
  - 建议设置 IP 白名单限制

### 可选环境变量

#### TELEGRAM_BOT_TOKEN
- **类型**: `string`
- **说明**: Telegram Bot Token（用于推送通知）
- **必需条件**: 仅当 `global.telegram.enabled=true` 时需要
- **获取方式**: 与 [@BotFather](https://t.me/botfather) 对话创建 Bot

#### TELEGRAM_CHAT_ID
- **类型**: `string`
- **说明**: Telegram Chat ID（接收通知的聊天 ID）
- **获取方式**:
  1. 与你的 Bot 发送任意消息
  2. 访问 `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
  3. 找到 `"chat":{"id":123456789}` 字段

#### VQ_LOG_DIR
- **类型**: `string`
- **默认值**: `logs/`（相对于工作目录）
- **说明**: 日志文件存储目录
- **systemd 部署**: 通常设置为 `/var/log/vibe-quant`
- **日志文件**: `vibe-quant_YYYY-MM-DD.log` 与 `error_YYYY-MM-DD.log`（旧日志压缩为 `.gz`）

### 环境变量配置示例

**.env 文件（本地开发）**:
```bash
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789
```

**systemd EnvironmentFile（生产环境）**:
参考 `deploy/systemd/vibe-quant.env.example`

---

## 配置文件结构

```yaml
global:
  testnet: false           # 是否使用测试网
  proxy: "http://..."      # 可选代理
  ws: {...}                # WebSocket 配置
  execution: {...}         # 执行配置
  accel: {...}             # 加速系统
  roi: {...}               # ROI 倍数系统
  risk: {...}              # 风控配置
  rate_limit: {...}        # 限速配置
  telegram: {...}          # 通知配置

symbols:
  "BTC/USDT:USDT": {...}   # 按交易对覆盖
  "ETH/USDT:USDT": {...}
```

---

## 全局配置（global）

### 基础配置

#### testnet
- **类型**: `boolean`
- **默认值**: `false`
- **说明**: 是否连接到币安测试网（testnet.binance.vision）
- **建议**: 开发测试时设为 `true`，生产环境必须为 `false`

#### proxy
- **类型**: `string | null`
- **默认值**: `null`
- **格式**: `"http://host:port"` 或 `"https://host:port"`
- **说明**: HTTP/HTTPS 代理地址，用于所有对外网络请求（ccxt API、WebSocket、Telegram）
- **使用场景**: 网络受限环境需要通过代理访问币安 API
- **注意**:
  - 系统不会读取 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量
  - 只有此配置项有效
  - socks5 代理需要使用 `proxychains` 等工具

---

### WebSocket 配置 (ws)

#### stale_data_ms
- **类型**: `int`
- **默认值**: `1500`
- **单位**: 毫秒
- **说明**: 数据陈旧阈值，超过此时间未收到更新则认为数据过期
- **影响**: 过期数据不会触发平仓信号，防止基于陈旧行情做出错误决策
- **调优建议**:
  - 正常网络: `1500-2000ms`
  - 不稳定网络: `3000-5000ms`（增加容忍度）

#### reconnect.initial_delay_ms
- **类型**: `int`
- **默认值**: `1000`
- **说明**: WebSocket 断线后首次重连延迟

#### reconnect.max_delay_ms
- **类型**: `int`
- **默认值**: `30000`
- **说明**: 重连延迟的最大值（指数退避上限）

#### reconnect.multiplier
- **类型**: `int`
- **默认值**: `2`
- **说明**: 重连延迟倍数（指数退避系数）
- **退避策略**: `delay = min(initial_delay × multiplier^retry_count, max_delay)`

---

### 执行配置 (execution)

#### 时序参数

##### order_ttl_ms
- **类型**: `int`
- **默认值**: `800`
- **单位**: 毫秒
- **说明**: 订单存活时间（Time To Live），超时未成交则撤单
- **影响**:
  - 过小: 订单频繁超时，成交率低
  - 过大: 价格偏离时无法及时调整
- **调优建议**:
  - 高波动品种: `500-1000ms`
  - 低波动品种: `2000-5000ms`
  - 当前配置示例: 全局 `4000ms`，ZEN 覆盖为 `5000ms`

##### repost_cooldown_ms
- **类型**: `int`
- **默认值**: `100`
- **单位**: 毫秒
- **说明**: 撤单后到下次下单的冷却时间
- **目的**: 防止频繁撤单-重下触发交易所限速
- **建议**: 保持默认值 `100ms`，除非遇到限速问题

##### min_signal_interval_ms ⭐
- **类型**: `int`
- **默认值**: `200`
- **单位**: 毫秒
- **说明**: 同一仓位（symbol + positionSide）两次平仓信号的最小间隔（节流器）
- **作用**: 防止信号风暴，限制平仓触发频率
- **工作机制**:
  ```
  Time    Event                      节流状态             结果
  ----    ----------------------     ----------------     ------
  0ms     满足平仓条件               无上次信号           ✓ 产生信号
  50ms    再次满足条件               50ms < 200ms         ✗ 被节流
  250ms   再次满足条件               250ms > 200ms        ✓ 产生信号
  ```
- **调优建议**:
  - 超短线 scalping: `50-100ms`（风险：信号过频）
  - **通用场景**: `200ms`（推荐，平衡响应性和稳定性）
  - 稳健大仓位: `500-1000ms`（减少频繁触发）

##### base_lot_mult
- **类型**: `int`
- **默认值**: `1`
- **说明**: 基础片大小倍数，最终下单量 = `minQty × base_lot_mult × roi_mult × accel_mult`
- **用途**: 全局调整每次平仓的基础仓位大小
- **示例**: 设为 `2` 表示每次至少平 `2 × minQty`

---

#### Maker 定价策略

##### maker_price_mode
- **类型**: `enum`
- **可选值**: `"at_touch"` | `"inside_spread_1tick"` | `"custom_ticks"`
- **默认值**: `"inside_spread_1tick"`
- **说明**: Maker 订单的定价模式（Post-only 限价单）
  - `at_touch`: 挂在对手价（买一/卖一）
  - `inside_spread_1tick`: 挂在对手价内缩 1 tick（更优价格，成交概率更高）
  - `custom_ticks`: 自定义内缩 tick 数（由 `maker_n_ticks` 指定）
- **调优建议**:
  - 追求快速成交: `inside_spread_1tick` 或 `custom_ticks: 2-3`
  - 追求最优价格: `at_touch`

##### maker_n_ticks
- **类型**: `int`
- **默认值**: `1`
- **说明**: 当 `maker_price_mode = "custom_ticks"` 时，内缩的 tick 数量

##### maker_safety_ticks
- **类型**: `int`
- **默认值**: `1`
- **约束**: `>= 1`
- **说明**: Post-only Maker 订单的安全距离（ticks），防止触发 `-5022` 错误（订单会立即成交）
- **影响**:
  - 越大: 越不容易触发错误，但成交难度增加
  - 越小: 成交概率高，但可能被拒单
- **建议**: 波动大的品种可设为 `2-3`

---

#### 风险控制

##### max_mult
- **类型**: `int`
- **默认值**: `50`
- **说明**: 最大加速倍数上限，`final_mult = min(base_mult × roi_mult × accel_mult, max_mult)`
- **目的**: 防止极端行情下单笔订单过大
- **调优建议**:
  - 保守: `50-100`
  - 激进: `200-500`（需确保账户余额充足）

##### max_order_notional
- **类型**: `decimal`
- **默认值**: `200`
- **单位**: USDT
- **说明**: 单笔订单最大名义价值，`notional = quantity × price`
- **目的**: 限制单笔订单的市场冲击和风险暴露
- **调优建议**:
  - 小仓位: `100-300 USDT`
  - 中等仓位: `500-1000 USDT`
  - 大仓位: `2000+ USDT`（当前全局配置为 `2000`）

---

#### 模式轮转（Execution Mode Rotation）

系统维护每个 `symbol + positionSide` 的执行模式状态机：`MAKER_ONLY` ↔ `AGGRESSIVE_LIMIT`

##### maker_timeouts_to_escalate
- **类型**: `int`
- **默认值**: `2`
- **说明**: `MAKER_ONLY` 模式下，连续超时 >= N 次后升级到 `AGGRESSIVE_LIMIT`
- **目的**: Maker 订单多次未成交时，切换到更激进策略
- **特殊值**: `0` = 禁用升级（永远停留在 MAKER_ONLY）

##### aggr_fills_to_deescalate
- **类型**: `int`
- **默认值**: `1`
- **说明**: `AGGRESSIVE_LIMIT` 模式下，成交 >= N 次后降级回 `MAKER_ONLY`
- **目的**: 成交顺利时回归低滑点策略
- **特殊值**: `0` = 禁用降级

##### aggr_timeouts_to_deescalate
- **类型**: `int`
- **默认值**: `2`
- **说明**: `AGGRESSIVE_LIMIT` 模式下，连续超时 >= N 次后降级回 `MAKER_ONLY`
- **目的**: 激进策略失效时回归 Maker
- **特殊值**: `0` = 禁用降级

**轮转策略示例**:
```
MAKER_ONLY → 超时2次 → AGGRESSIVE_LIMIT → 成交1次 → MAKER_ONLY
MAKER_ONLY → 超时2次 → AGGRESSIVE_LIMIT → 超时2次 → MAKER_ONLY
```

---

### 加速系统 (accel)

基于滑动窗口回报率动态调整平仓片大小。

#### window_ms
- **类型**: `int`
- **默认值**: `2000`
- **单位**: 毫秒
- **说明**: 滑动窗口大小，用于计算 `ret = (p_now / p_window_ago) - 1`
- **调优建议**:
  - 快速响应: `1000-1500ms`
  - 平滑响应: `2000-3000ms`

#### tiers
- **类型**: `List[{ret, mult}]`
- **说明**: 加速档位，LONG/SHORT 共用（系统自动处理方向）
- **格式**:
  ```yaml
  - { ret: 0.0003, mult: 3 }   # 窗口涨/跌幅 ≥ 0.03% → mult=3
  - { ret: 0.001, mult: 7 }    # 窗口涨/跌幅 ≥ 0.1% → mult=7
  ```
- **选择逻辑**: 取满足条件的最大档位
- **方向处理**:
  - LONG 仓位: 取正方向回报率（价格上涨）
  - SHORT 仓位: 取负方向回报率（价格下跌），匹配时取绝对值

**示例档位**（当前配置）:
| 回报率阈值 | 倍数 | 说明 |
|-----------|------|------|
| 0.03% | 3 | 轻微加速 |
| 0.1% | 7 | 中等加速 |
| 0.5% | 45 | 快速行情 |
| 1.0% | 90 | 剧烈波动 |
| 2.0% | 400 | 极端行情（接近 max_mult 上限） |

---

### ROI 倍数系统 (roi)

基于未实现盈亏比例（ROI）调整平仓速度。

#### ROI 计算口径
```
notional = abs(position_amt) × entry_price
initial_margin = notional / leverage
roi = unrealized_pnl / initial_margin
```

#### tiers
- **类型**: `List[{roi, mult}]`
- **说明**: ROI 档位（比例值，0.1 = 10%）
- **格式**:
  ```yaml
  - { roi: 0.1, mult: 3 }    # ROI ≥ 10% → mult=3
  - { roi: 0.3, mult: 6 }    # ROI ≥ 30% → mult=6
  ```
- **选择逻辑**: 取满足条件的最大档位
- **目的**: 盈利越多，平仓越快（落袋为安）

**示例档位**（当前配置）:
| ROI 阈值 | 倍数 | 说明 |
|---------|------|------|
| 10% | 3 | 小幅盈利 |
| 20% | 5 | 可观盈利 |
| 50% | 8 | 大幅盈利 |
| 70% | 10 | 极高盈利（加速离场） |

---

### 风控配置 (risk)

三级风控体系：预警 → 强制平仓 → 保护性止损。

#### levels
风控等级映射（可扩展）。<br>
用于日志展示 `risk_level`，避免在代码里写死 1/2/3，后续要新增第 4 等级或“在 2 和 3 之间插入新等级”时，只需要改配置，不需要改代码。<br>

- **类型**: `Dict[str, int]`
- **默认值**（内置）:
  - `liq_distance_breach: 1`
  - `panic_close: 2`
  - `protective_stop: 3`
- **说明**:
  - key 为风险阶段/类型（`risk_stage`），value 为等级数字（`risk_level`）。
  - 当前日志里会在以下事件附带 `risk_stage`/`risk_level`：
    - `[RISK_TRIGGER]`（风险预警 / 强制平仓）
    - `[PROTECTIVE_STOP]`（保护性止损相关事件）

**示例配置**:
```yaml
risk:
  levels:
    liq_distance_breach: 1
    panic_close: 2
    protective_stop: 3
```

#### liq_distance_threshold
- **类型**: `decimal`
- **默认值**: `0.015` (1.5%)
- **说明**: 强平距离预警阈值
- **计算**: `dist = abs(mark_price - liquidation_price) / mark_price`
- **触发**: 满足信号条件且 `dist <= threshold` 时，强制切换到 `AGGRESSIVE_LIMIT` 模式

---

#### 强制平仓（panic_close）

不依赖信号触发，按梯度强制平仓。

##### enabled
- **类型**: `boolean`
- **默认值**: `false`
- **说明**: 是否启用强制平仓兜底

##### ttl_percent
- **类型**: `decimal`
- **默认值**: `0.5`
- **说明**: 强制平仓订单的 TTL = `execution.order_ttl_ms × ttl_percent`
- **目的**: 紧急情况下缩短等待时间

##### tiers
- **类型**: `List[{dist_to_liq, slice_ratio, maker_timeouts_to_escalate}]`
- **说明**: 按强平距离分级的平仓策略
- **字段**:
  - `dist_to_liq`: 强平距离阈值
  - `slice_ratio`: 每次平仓的仓位比例（0~1）
  - `maker_timeouts_to_escalate`: 先尝试 Maker 的次数

**示例配置**:
```yaml
tiers:
  - { dist_to_liq: 0.04, slice_ratio: 0.02, maker_timeouts_to_escalate: 2 }  # 距离强平4%，每次平2%
  - { dist_to_liq: 0.03, slice_ratio: 0.05, maker_timeouts_to_escalate: 2 }  # 距离强平3%，每次平5%
  - { dist_to_liq: 0.02, slice_ratio: 0.10, maker_timeouts_to_escalate: 2 }  # 距离强平2%，每次平10%
```

**选择逻辑**: 取满足条件的最危险档位（dist_to_liq 最小）

---

#### 保护性止损（protective_stop）

交易所端条件单兜底（STOP_MARKET + closePosition），防止程序崩溃/断网。

##### enabled
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: 是否启用保护性止损

##### dist_to_liq
- **类型**: `decimal`
- **默认值**: `0.01` (1%)
- **说明**: 止损触发距离，系统按 `liquidation_price` 反推 `stopPrice`，使触发时强平距离约为此值
- **特性**:
  - 订单类型: `STOP_MARKET`
  - 触发价格类型: `MARK_PRICE`（标记价格）
  - `closePosition=true`（全平）
- **调优建议**:
  - 保守: `0.02-0.03` (2-3%)
  - 激进: `0.005-0.01` (0.5-1%)
  - 当前配置: 全局 `1%`，ZEN 覆盖为 `1.5%`

##### external_takeover
外部止损/止盈接管开关与策略。<br>
当检测到同侧存在“外部 stop/tp 条件单”时，本程序会停止维护自己的保护止损，避免与外部单冲突导致 `-4130` 或出现同侧多张条件单。<br>

**外部接管判定（当前实现）**：
- **WS 事件**：`ORDER_TRADE_UPDATE` 或 `ALGO_UPDATE` 中，若订单类型为 `STOP/TAKE_PROFIT*` 且（`closePosition=true` **或** `reduceOnly=true`），则视为外部接管。
- **REST 校验**：以交易所原始接口 `GET /fapi/v1/openOrders`（raw openOrders）为主，必要时回退 `fetch_open_orders`，并合并 `fetch_open_algo_orders`，扫描同侧订单：若存在 `STOP/TAKE_PROFIT*` 且（`closePosition=true` **或** `reduceOnly=true`），则视为外部接管。
  - 说明：部分客户端下的条件单在 ccxt 的 openOrders 里可能不完整/缺字段（例如 `origQty=0` 的 closePosition 单），因此 raw openOrders 是可靠兜底。

**释放策略（避免多外部单并存时误释放）**：<br>
- WS 收到某一张外部 stop/tp 的终态（CANCELED/FILLED/EXPIRED/REJECTED）**不直接释放**外部接管，而是触发一次 REST verify；只有 verify 确认“同侧外部 stop/tp 已不存在”才释放并恢复自维护。

字段说明：

###### enabled
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: 是否启用外部接管逻辑

###### rest_verify_interval_s
- **类型**: `int`
- **默认值**: `30`
- **说明**: 外部接管锁存期间，触发 REST 校验的最小间隔（秒）
- **目的**: 防止只靠 WS（或竞态/漏消息）导致接管状态无法释放

###### max_hold_s
- **类型**: `int`
- **默认值**: `300`
- **说明**: 外部接管锁存的最长持续时间（秒）
- **行为**: 超时后会触发一次 REST 校验兜底（并可能释放接管）

**示例配置**:
```yaml
risk:
  levels:
    liq_distance_breach: 1
    panic_close: 2
    protective_stop: 3
  protective_stop:
    enabled: true
    dist_to_liq: 0.01
    external_takeover:
      enabled: true
      rest_verify_interval_s: 30
      max_hold_s: 300
```

---

### 限速配置 (rate_limit)

防止触发币安 API 限速。

#### max_orders_per_sec
- **类型**: `int`
- **默认值**: `5`
- **说明**: 每秒最大下单数（系统内部限流）

#### max_cancels_per_sec
- **类型**: `int`
- **默认值**: `8`
- **说明**: 每秒最大撤单数

**币安限制参考**:
- 订单权重限制: 1200/分钟（Order 类）
- 单个订单权重: `1` (NEW_ORDER) / `1` (CANCEL_ORDER)
- 建议保持一定余量，避免极端情况触发限速

---

### Telegram 通知 (telegram)

#### enabled
- **类型**: `boolean`
- **默认值**: `false`
- **说明**: 是否启用 Telegram 通知
- **凭证**: 通过环境变量配置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`

#### events
通知事件开关：

##### on_fill
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: 订单成交通知

##### on_reconnect
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: WebSocket 重连通知

##### on_risk_trigger
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: 风险触发通知（强制平仓、保护性止损等）

##### on_open_alert
- **类型**: `boolean`
- **默认值**: `true`
- **说明**: 开仓/加仓告警（检测到新仓位或仓位增加）

---

## Symbol 级别覆盖 (symbols)

可按交易对覆盖 global 配置中的任意参数，**未指定的字段使用 global 默认值**。

### 配置结构

```yaml
symbols:
  "SYMBOL/USDT:USDT":
    execution:
      order_ttl_ms: 3000          # 覆盖全局配置
      max_order_notional: 500
    accel:
      window_ms: 1500
      tiers: [...]                # 完全覆盖档位
    roi:
      tiers: [...]
    risk:
      # 可选：覆盖等级映射（只影响日志展示）
      # levels:
      #   liq_distance_breach: 1
      #   panic_close: 2
      #   protective_stop: 3
      liq_distance_threshold: 0.02
      panic_close:
        enabled: true
        tiers: [...]
      protective_stop:
        dist_to_liq: 0.015
```

### 示例：ZEN 永续覆盖配置

```yaml
ZEN/USDT:USDT:
  execution:
    order_ttl_ms: 5000            # ZEN 波动大，延长 TTL
    max_order_notional: 300       # 降低单笔名义价值
    maker_safety_ticks: 2         # 增加安全距离
    maker_timeouts_to_escalate: 1 # 更快升级到激进模式
    aggr_fills_to_deescalate: 1
    aggr_timeouts_to_deescalate: 1
  risk:
    protective_stop:
      dist_to_liq: 0.015          # ZEN 风险更高，保护止损距离加大
```

---

## 配置优先级

**Symbol 配置 > Global 配置**

合并逻辑（伪代码）:
```python
final_config = {
    **global_config,           # 全局默认值
    **symbol_config            # Symbol 覆盖（如果有）
}
```

**字段级覆盖**：
- Symbol 中指定的字段会覆盖 global 中的对应字段
- Symbol 中未指定的字段使用 global 默认值
- 对于列表类型（如 `tiers`），Symbol 配置会**完全替换** global 配置（非合并）

---

## 常见配置场景

### 场景1：超短线 Scalping

```yaml
global:
  execution:
    order_ttl_ms: 500
    min_signal_interval_ms: 50
    maker_timeouts_to_escalate: 1   # 快速升级
  accel:
    window_ms: 1000                  # 短窗口
```

### 场景2：稳健大仓位

```yaml
global:
  execution:
    order_ttl_ms: 5000
    min_signal_interval_ms: 500     # 减少触发频率
    max_order_notional: 1000        # 增大单笔上限
    maker_timeouts_to_escalate: 3   # 延迟升级
```

### 场景3：高风险品种（如 ZEN、MEME 币）

```yaml
symbols:
  "ZEN/USDT:USDT":
    execution:
      order_ttl_ms: 5000
      maker_safety_ticks: 2         # 防止拒单
      max_order_notional: 300       # 降低风险暴露
    risk:
      protective_stop:
        dist_to_liq: 0.02           # 更早触发保护
```

### 场景4：测试环境

```yaml
global:
  testnet: true                     # 使用测试网
  execution:
    max_order_notional: 50          # 小额测试
  telegram:
    enabled: false                  # 关闭通知
  risk:
    protective_stop:
      enabled: false                # 测试时可能不需要
```

---

## 调优建议总结

| 参数 | 激进 | 保守 | 说明 |
|------|------|------|------|
| `min_signal_interval_ms` | 50-100ms | 500-1000ms | 平衡响应性和稳定性 |
| `order_ttl_ms` | 500-1000ms | 3000-5000ms | 取决于品种波动性 |
| `max_order_notional` | 1000-2000 | 100-500 | 风险承受能力 |
| `maker_timeouts_to_escalate` | 1 | 3-5 | 升级速度 |
| `protective_stop.dist_to_liq` | 0.5-1% | 2-3% | 保护止损触发距离 |
| `accel.window_ms` | 1000ms | 3000ms | 加速响应灵敏度 |

---

## 相关文档

- **快速启动**: [`README.md`](../README.md)
- **设计文档**: [`memory-bank/design-document.md`](../memory-bank/design-document.md)
- **系统架构**: [`memory-bank/architecture.md`](../memory-bank/architecture.md)
- **开发进度**: [`memory-bank/progress.md`](../memory-bank/progress.md)

---

*最后更新: 2025-12-22*
