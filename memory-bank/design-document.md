<!-- Input: 需求与设计目标 -->
<!-- Output: 设计方案与约束 -->
<!-- Pos: memory-bank/design-document -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->

# Binance U 本位永续 Hedge 模式：Reduce-Only 小单平仓执行器（ccxt + WebSocket）

> 目标：监听 Binance U 本位永续合约盘口与成交，在**不试图影响价格**的前提下，通过“多次小量 + 执行模式轮转（maker → 激进限价）+ 超时撤单 + 风险兜底”，完成 Hedge 模式下 LONG/SHORT 仓位的 reduce-only 平仓，尽量降低滑点与市场冲击。  
> 说明：本设计仅覆盖“减少冲击、提高成交效率、风险控制”的执行工程，不包含任何试图推动价格或影响市场的行为。

---

## 1. 需求摘要

### 1.1 功能需求
- **合约不写死**：支持运行时指定或配置热加载 symbol。
- Binance U 本位永续，**双向持仓（Hedge Mode）**。
- **自动识别仓位**：LONG/SHORT 两侧都可能存在，分别独立执行 reduce-only 平仓。
- 数据源（WebSocket）：
  - 买一/卖一（best bid/ask）
  - 最近一笔成交价（last trade）
- 平仓触发条件（信号层）：
  - LONG：trade 动能 + 买一抬升支持
  - SHORT：trade 动能 + 卖一压低支持
  - 加速：短窗（滑动窗口）内价格向有利方向快速运动时，增大减仓片大小
- 下单（执行层）：
  - 支持**执行模式轮转**：`MAKER_ONLY` → `AGGRESSIVE_LIMIT`
  - maker：post-only 限价挂单，超时撤单
  - 激进限价：更贴近成交方向的 LIMIT（不使用 post-only）
- 订单安全：
  - 全部订单 `reduceOnly=True`
  - Hedge 模式必须指定 `positionSide=LONG/SHORT`
- 收敛结束：仓位量小到不可交易余量（minQty/stepSize 规整后为 0）则认为完成，不留下尘埃仓位。
- 日志：本地文件按天滚动
- Telegram：成交、断线重连、风险兜底触发

### 1.2 非功能需求
- 延迟目标：端到端 < 200ms（事件驱动、异步 I/O、避免阻塞）。
- 稳定性：断线自动重连、状态可恢复、幂等处理撤单/重下。
- 可配置：每个 symbol 可覆盖参数、档位、模式轮转规则。
- 可观测：撤单率、成交率、平均等待时间、触发频率、模式分布等。

---

## 2. 关键概念与数据定义

### 2.1 市场数据
- `best_bid`, `best_ask`：买一/卖一价格（WS）
- `last_trade_price`：最近成交价（WS）
- `previous_trade_price`：上一次成交价（用于动能比较）
- `ret_window`：滑动窗口回报率（用于“加速”）

### 2.2 仓位数据（Hedge）
- LONG：`positionSide=LONG`，平仓方向 `SELL`
- SHORT：`positionSide=SHORT`，平仓方向 `BUY`
- `position_amt`：该侧仓位数量（合约数量），需按 `stepSize` 规整

### 2.3 交易所规则（每 symbol）
- `tickSize`：价格最小变动
- `stepSize`：数量步进
- `minQty`：最小下单量
- （可选）`minNotional`：最小名义价值（若存在则也要满足）

---

## 3. 平仓触发条件（信号层）

### 3.1 LONG 平仓条件（用户定义）
```python
long_primary = last_trade_price > previous_trade_price and best_bid >= last_trade_price
long_bid_improve = (not long_primary) and best_bid >= last_trade_price and best_bid > previous_trade_price
long_exit_condition_met = long_primary or long_bid_improve
```

### 3.2 SHORT 平仓条件（用户定义）
```python
short_primary = last_trade_price < previous_trade_price and best_ask <= last_trade_price
short_ask_improve = (not short_primary) and best_ask <= last_trade_price and best_ask < previous_trade_price
short_exit_condition_met = short_primary or short_ask_improve
```

### 3.3 加速条件（新增：执行加速器）
- 滑动窗口回报率（window=`accel_window_ms`）：
  - `ret = (p_now / p_window_ago - 1)`，其中 `p` 使用 `last_trade_price`
- 多仓加速：`ret >= accel_threshold_long`（由档位配置决定）
- 空仓加速：`ret <= -accel_threshold_short`（由档位配置决定）
- 加速只改变“单次减仓片大小”，不改变方向，不做任何影响盘口的行为。

### 3.4 触发节流
- `min_signal_interval_ms`：同一侧仓位两次触发最小间隔（默认 200ms）
- 全局限速：`max_orders_per_sec`、`max_cancels_per_sec`

---

## 4. 执行层：执行模式轮转（核心）

### 4.1 执行模式定义
对每个 `symbol + positionSide` 独立维护一个 `execution_mode`：

- `MAKER_ONLY`
  - Post-only LIMIT（GTX）
  - 超时撤单
- `AGGRESSIVE_LIMIT`
  - LIMIT（不使用 post-only），价格更贴近立即成交方向
  - 超时撤单

> 常规执行不使用市价单（不进入 `MARKET` 执行模式），执行模式仅在 `MAKER_ONLY ↔ AGGRESSIVE_LIMIT` 间轮转；但会额外维护交易所端 `STOP_MARKET closePosition` 作为“保护性止损”兜底。

### 4.2 模式轮转规则（可配置）
- 维护计数器：
  - `maker_timeout_count`
  - `aggr_timeout_count`
- 轮转触发（示例，均可配置）：
  - `MAKER_ONLY` 连续超时 `>= maker_timeouts_to_escalate` → 切到 `AGGRESSIVE_LIMIT`
  - `AGGRESSIVE_LIMIT` 成交（或连续超时达到阈值） → 可切回 `MAKER_ONLY`（让执行回到低冲击）
  - 风险兜底触发（接近强平）：
    - 强制切到 `AGGRESSIVE_LIMIT`

### 4.3 状态机（每侧）
- `IDLE` → `PLACE` → `WAIT` → (`FILLED` | `TIMEOUT`) → `CANCEL` → `COOLDOWN` → `IDLE`

---

## 5. 下单细则

### 5.1 安全参数（必须）
- `reduceOnly=True`
- `positionSide="LONG" | "SHORT"`
- LONG 平仓：`side="sell"`
- SHORT 平仓：`side="buy"`
- 订单归属标记：下单时设置 `newClientOrderId`（前缀 `<client_order_prefix>-{run_id}-`，其中 `client_order_prefix` 为固定前缀，`run_id` 每次启动自动生成）。退出时只撤销本次运行前缀挂单，避免误撤手动订单；注意：若进程崩溃/强杀，遗留挂单不会自动清理

### 5.2 定价策略（maker 可配置）
参数：`maker_price_mode`（全局默认 + symbol 覆盖）
- `at_touch`
  - LONG SELL：`price = best_ask`
  - SHORT BUY：`price = best_bid`
- `inside_spread_1tick`
  - LONG SELL：`price = best_ask - tickSize`
  - SHORT BUY：`price = best_bid + tickSize`
- `custom_ticks`（推进 N tick）
  - LONG SELL：`price = best_ask - n_ticks * tickSize`
  - SHORT BUY：`price = best_bid + n_ticks * tickSize`

maker 订单要求：
- `timeInForce="GTX"`（post-only）
- 价格按 `tickSize` 规整
- post-only 安全距离（减少 `-5022 Post Only rejected` 概率）：SELL ≥ `best_bid + maker_safety_ticks*tickSize`，BUY ≤ `best_ask - maker_safety_ticks*tickSize`（默认 `maker_safety_ticks=1`）
- 若被交易所拒单（`code=-5022`，Post-only 被拒）：系统会记录为 `[ORDER_REJECT] 下单被拒 | reason=post_only_reject`（WARNING）用于降噪，且执行引擎不会重复打印"下单失败"。
- 若因 post-only 被拒/自动取消：记一次“无效尝试”，进入冷却后重试或降级为更保守定价（可选策略）

### 5.3 激进限价（AGGRESSIVE_LIMIT）
- 仍为 LIMIT，但更贴近成交方向：
  - LONG SELL：`price = best_bid`
  - SHORT BUY：`price = best_ask`
- 不使用 post-only（允许立即成交）
- 价格按 `tickSize` 规整

### 5.4 市价（不使用）
- 常规执行不使用 `MARKET` 订单，统一采用 `LIMIT`（MAKER_ONLY/AGGRESSIVE_LIMIT）+ TTL 超时撤单 + 重试；“保护性止损”使用交易所端条件单 `STOP_MARKET closePosition` 兜底。

---

## 6. 拆单片大小（数量）与倍数系统

### 6.1 基准片大小
- `base_qty = base_lot_mult * minQty`（默认 base_lot_mult=1）

### 6.2 ROI 档位倍数（多档可配置）
- ROI 定义：该侧仓位投资回报率（按交易所/ccxt 可获得口径实现）
- 档位表：`roi >= threshold -> mult`（取满足条件的最高档位）

### 6.3 加速档位倍数（多档可配置）
- `ret >= threshold -> mult`（多仓）
- `ret <= -threshold -> mult`（空仓）
- 取满足条件的最高档位

### 6.4 倍数合成（用户确认：乘法）+ 双保险
- `final_mult = base_mult * roi_mult * accel_mult`
- 保险 1：`final_mult = min(final_mult, max_mult)`（每 symbol 可覆盖；用户可设很高，但建议更保守）
- 保险 2：`max_order_notional`（强烈建议启用）
  - `order_notional = qty * last_trade_price`
  - 若超限：缩小 `qty` 直至满足
- 最终目标数量：
  - `target_qty = clamp_to_step(min(abs(position_amt), base_qty * final_mult), stepSize)`
  - 若规整后为 0：认为“不可交易余量”，执行完成

---

## 7. 完成条件（不留尘埃仓位）

业界实践：用“可交易余量”定义完成，避免最小下单限制导致永远清不干净。

- 若 `abs(position_amt)` 按 `stepSize` 规整后为 0 → 完成
- 或 `abs(position_amt) < minQty` → 视为完成（不可再下单）
- 若 `abs(position_amt)` 介于 `[minQty, 2*minQty)`：下一片直接清掉（按 stepSize 规整）

---

## 8. 风险控制（兜底层）

### 8.1 强平距离兜底（最小可用版本）
- `dist = abs(mark_price - liquidation_price) / mark_price`
- 若 `dist <= liq_distance_threshold`：
  - Telegram：风险兜底触发
  - 强制模式：至少切到 `AGGRESSIVE_LIMIT`
  - 不进入 `MARKET` 执行模式（常规执行仍采用 LIMIT；另有保护性止损兜底）

> 逐仓/全仓对“策略触发”影响不大，但对风险分布与强平概率有影响，因此兜底层必须存在且独立。

### 8.2 数据陈旧/断流保护
- `stale_data_ms` 内若未收到 trade 或 best bid/ask 更新：
  - 暂停下单
- WS 断线：自动重连
- 重连后执行一次 REST 校准（规则/仓位），保证状态一致

### 8.3 速率限制
- `max_orders_per_sec`、`max_cancels_per_sec` 全局限速
- 兜底动作优先级高于普通动作

### 8.4 仓位保护性止损（交易所端条件单）
- 目标：防程序崩溃/休眠/断网等意外，确保“即使本程序不在跑”也有一张 server-side 兜底单能触发平仓。
- 订单：`STOP_MARKET` + `closePosition=true` + `positionSide=LONG/SHORT`
- 触发：`MARK_PRICE`（标记价格触发）
- 配置：
  - `global.risk.protective_stop.enabled`
  - `global.risk.protective_stop.dist_to_liq`（默认 0.01）
  - `symbols.<symbol>.risk.protective_stop_dist_to_liq`（按 symbol 覆盖）
  - 外部止损/止盈接管（避免与手动 stop/tp 冲突）：
    - `global.risk.protective_stop.external_takeover.enabled`
    - `global.risk.protective_stop.external_takeover.rest_verify_interval_s`
    - `global.risk.protective_stop.external_takeover.max_hold_s`
- stopPrice 计算（按 liquidation_price 反推触发点）：
  - LONG：`stopPrice = liquidation_price / (1 - D)`（SELL stop）
  - SHORT：`stopPrice = liquidation_price / (1 + D)`（BUY stop）

---

## 9. 系统架构

### 9.1 模块
- `ConfigManager`：全局默认 + symbol 覆盖，支持热更新（可选）
- `ExchangeAdapter`（ccxt）：fetch markets/positions/balance，下单撤单封装
- `WSClient`：订阅 trade + best bid/ask
- `SignalEngine`：条件判断、滑动窗口 ret、倍数计算
- `ExecutionEngine`：per-side 状态机、模式轮转、下单撤单
- `RiskManager`：强平距离兜底、数据陈旧保护、限速
- `Logger`：按天滚动
- `Notifier`：Telegram

---

## 10. 配置设计（YAML 示例）

```yaml
global:
  ws:
    stale_data_ms: 1500

  execution:
    # 时序
    order_ttl_ms: 800
    repost_cooldown_ms: 100
    min_signal_interval_ms: 200

    # 模式轮转
    maker_timeouts_to_escalate: 2
    aggr_fills_to_deescalate: 1
    aggr_timeouts_to_deescalate: 2

    # maker 定价策略（可配置）
    maker_price_mode: "inside_spread_1tick"  # at_touch | inside_spread_1tick | custom_ticks
    maker_n_ticks: 1                         # custom_ticks 才使用

    # 双保险
    max_mult: 50
    max_order_notional: 200                  # USDT

  accel:
    window_ms: 2000
    tiers:  # LONG/SHORT 共用，方向由代码自动处理
      - { ret: 0.0010, mult: 2 }
      - { ret: 0.0020, mult: 4 }

  roi:
    tiers:
      - { roi: 0.10, mult: 3 }
      - { roi: 0.20, mult: 6 }

  risk:
    liq_distance_threshold: 0.015
    panic_close:
      enabled: false
      ttl_percent: 0.5
      tiers:
        - { dist_to_liq: 0.012, slice_ratio: 0.1, maker_timeouts_to_escalate: 2 }
        - { dist_to_liq: 0.008, slice_ratio: 0.25, maker_timeouts_to_escalate: 1 }
    protective_stop:
      enabled: true
      dist_to_liq: 0.01

  rate_limit:
    max_orders_per_sec: 5
    max_cancels_per_sec: 8

  telegram:
    enabled: true
    events:
      on_fill: true
      on_reconnect: true
      on_risk_trigger: true

symbols:
  BTC/USDT:USDT:
    execution:
      order_ttl_ms: 1200
      maker_price_mode: "at_touch"
      max_mult: 80
      max_order_notional: 500
    accel:
      tiers:
        - { ret: 0.0010, mult: 2 }
        - { ret: 0.0025, mult: 6 }
    roi:
      tiers:
        - { roi: 0.10, mult: 3 }
        - { roi: 0.30, mult: 10 }
    risk:  # symbol 可覆盖 risk.* 全部字段
      protective_stop_dist_to_liq: 0.008
```

> Telegram 凭证（敏感信息）通过环境变量提供：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。

---

## 11. 伪代码（简化）

```python
def build_maker_price(side, best_bid, best_ask, tick, mode, n_ticks):
    if mode == "at_touch":
        return best_ask if side == "LONG" else best_bid
    if mode == "inside_spread_1tick":
        return best_ask - tick if side == "LONG" else best_bid + tick
    if mode == "custom_ticks":
        return best_ask - n_ticks*tick if side == "LONG" else best_bid + n_ticks*tick
    raise ValueError("unknown mode")

def build_aggressive_price(side, best_bid, best_ask):
    return best_bid if side == "LONG" else best_ask

def choose_mode(side_state, cfg, risk_flag):
    if risk_flag:
        return "AGGRESSIVE_LIMIT"
    if side_state.mode == "MAKER_ONLY" and side_state.maker_timeout_count >= cfg.maker_timeouts_to_escalate:
        return "AGGRESSIVE_LIMIT"
    if side_state.mode == "AGGRESSIVE_LIMIT" and (
        side_state.aggr_fill_count >= cfg.aggr_fills_to_deescalate
        or side_state.aggr_timeout_count >= cfg.aggr_timeouts_to_deescalate
    ):
        return "MAKER_ONLY"
    return side_state.mode

def compute_qty(pos_amt_abs, minQty, stepSize, last_trade, mult, cfg):
    mult = min(mult, cfg.max_mult)
    qty = min(pos_amt_abs, minQty * mult)
    qty = clamp_to_step(qty, stepSize)
    if cfg.max_order_notional is not None:
        while qty > 0 and qty * last_trade > cfg.max_order_notional:
            qty = clamp_to_step(qty - stepSize, stepSize)
    return qty

def tick_side(side):
    # 省略：节流、陈旧数据检测、仓位获取、信号判断
    if not exit_condition_met(side): return

    risk_flag = risk_liq_close(side)
    mode = choose_mode(side_state, cfg, risk_flag)

    mult = base_mult * roi_mult(side) * accel_mult(side)
    qty = compute_qty(abs(pos.amt), minQty, stepSize, last_trade_price, mult, cfg)
    if qty <= 0: return

    if mode == "MAKER_ONLY":
        price = build_maker_price(side, best_bid, best_ask, tickSize, cfg.maker_price_mode, cfg.maker_n_ticks)
        place_post_only_limit_reduce_only(side, qty, clamp_to_tick(price))
    elif mode == "AGGRESSIVE_LIMIT":
        price = build_aggressive_price(side, best_bid, best_ask)
        place_limit_reduce_only(side, qty, clamp_to_tick(price))

    wait_until_ttl_then_cancel_if_needed()
```

---

## 12. 日志与通知

- 日志：文本，按天滚动（可选支持 JSON）
- Telegram：
  - 成交（含 symbol、side、mode、qty、avg_price、reason）
  - WS 断线与重连成功
  - 风险兜底触发（含 dist_to_liq）

---

## 13. 测试计划
- 单元测试：数量/价格规整、档位匹配、滑动窗口 ret、完成条件、模式轮转
- 回放仿真：trade + best bid/ask
- 小额实盘验证：reduceOnly/positionSide/post-only 行为、断线重连、模式轮转是否符合预期

---
