<!-- Input: MVP 目标与范围 -->
<!-- Output: MVP 边界与验收 -->
<!-- Pos: memory-bank/mvp-scope -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->

# MVP 范围定义

> 本文档定义了最小可行产品（MVP）的精确边界，供团队评审确认。
> 创建日期：2024-12-16
> 更新日期：2025-12-19（全部功能已完成）
> 对照文档：design-document.md 第 1、3、4、7 章

---

## MVP 核心链路

```
WS 行情 → 信号判断 → 下单/撤单 → 仓位收敛
```

---

## MVP 包含（必须实现）

### 1. 配置系统
- [x] YAML 配置加载
- [x] global 默认值 + symbol 覆盖
- [x] API 密钥从环境变量读取（`BINANCE_API_KEY`、`BINANCE_API_SECRET`）
- [x] 配置项：stale_data_ms、order_ttl_ms、repost_cooldown_ms、min_signal_interval_ms
- [x] 配置项：maker_price_mode、maker_n_ticks、base_lot_mult
- [x] 配置项：max_mult、max_order_notional

### 2. 交易所适配（ccxt）
- [x] 拉取 markets 规则：tickSize、stepSize、minQty、minNotional
- [x] 读取 Hedge 模式 LONG/SHORT 仓位
- [x] 下单：LIMIT post-only (GTX)，reduce-only 语义约束（`positionSide + side + qty<=position`），positionSide=LONG/SHORT
- [x] 撤单

### 3. WebSocket 数据
- [x] 市场数据：bookTicker（best bid/ask）+ aggTrade（last trade）+ markPrice@1s
- [x] User Data Stream：ORDER_TRADE_UPDATE + ACCOUNT_UPDATE（订单/仓位推送）
- [x] listenKey 每 30 分钟续期
- [x] 数据陈旧检测（stale_data_ms）
- [x] 断线自动重连（指数退避：1s, 2x, max 30s, 无限重试）
- [x] 重连后 REST 校准（positions + markets）

### 4. 信号层
- [x] 维护 previous_trade_price 与 last_trade_price
- [x] LONG 平仓条件（设计文档 3.1）：
  - `long_primary = last > prev AND best_bid >= last`
  - `long_bid_improve = (not primary) AND best_bid >= last AND best_bid > prev`
- [x] SHORT 平仓条件（设计文档 3.2）：
  - `short_primary = last < prev AND best_ask <= last`
  - `short_ask_improve = (not primary) AND best_ask <= last AND best_ask < prev`
- [x] 触发节流：min_signal_interval_ms

### 5. 执行层（仅 MAKER_ONLY）
- [x] 执行模式固定为 MAKER_ONLY
- [x] maker 定价策略：at_touch / inside_spread_1tick / custom_ticks
- [x] 价格按 tickSize 规整
- [x] 数量按 stepSize 规整，满足 minQty 和 minNotional
- [x] 状态机：IDLE → PLACE → WAIT → (FILLED|TIMEOUT) → CANCEL → COOLDOWN → IDLE
- [x] TTL 超时撤单
- [x] 部分成交处理（重置 timeout_count）
- [x] 双保险：max_mult、max_order_notional 限制

### 6. 仓位收敛（设计文档第 7 章）
- [x] 完成条件：abs(position_amt) 按 stepSize 规整后为 0，或 < minQty
- [x] 不留尘埃仓位

### 7. 日志系统
- [x] 按天滚动
- [x] 关键字段：timestamp、symbol、side、mode、state、reason、best_bid/ask、last_trade、order_id

### 8. 优雅退出
- [x] SIGINT/SIGTERM 处理
- [x] 立即撤销所有挂单后退出

---

## 后续阶段（已全部完成）

### 阶段 7：执行模式轮转 ✅
- [x] AGGRESSIVE_LIMIT 模式
- [x] maker_timeout_count / aggr_timeout_count 计数器
- [x] 模式切换逻辑

### 阶段 8：倍数档位系统 ✅
- [x] 滑动窗口 ret 计算（accel_mult）
- [x] ROI 档位倍数（roi_mult）
- [x] 倍数乘法合成

### 阶段 9：风控兜底 ✅
- [x] 强平距离 dist_to_liq 计算
- [x] 风险触发模式切换
- [x] 全局限速（max_orders_per_sec、max_cancels_per_sec）
- [x] 多级强制平仓（panic_close）
- [x] 保护性止损（STOP_MARKET closePosition）

### 阶段 10：通知 ✅
- [x] Telegram 通知（成交/重连/风险触发/开仓告警）

### 阶段 11：部署 ✅
- [x] systemd 部署模板
- [x] 多 symbol 并发（combined streams + 独立状态机）

---

## 设计文档交叉验证

| 设计文档章节 | 覆盖情况 |
|-------------|----------|
| 第 1 章 需求摘要 | ✅ 全部功能需求已覆盖 |
| 第 3 章 平仓触发条件 | ✅ 3.1/3.2 原始条件 + 3.3 加速条件 |
| 第 4 章 执行模式轮转 | ✅ MAKER_ONLY ↔ AGGRESSIVE_LIMIT |
| 第 7 章 完成条件 | ✅ 完全覆盖 |
| 第 8 章 风险控制 | ✅ 强平距离 + panic_close + 保护性止损 |

---

## 验收标准

1. **配置测试**：global + symbol 覆盖正确合并 ✅
2. **WS 测试**：连续运行 10 分钟，行情持续更新 ✅
3. **信号测试**：构造事件序列，验证触发条件正确 ✅
4. **下单测试**：reduce-only 语义约束 + positionSide 正确 ⏳ 待实盘验证
5. **收敛测试**：小仓位运行至仓位归零或 < minQty ⏳ 待实盘验证
6. **重连测试**：断网后自动重连并恢复 ⏳ 待实盘验证
7. **退出测试**：Ctrl+C 后挂单被撤销 ⏳ 待实盘验证

---

## 评审确认

- [x] 开发者自审通过
- [ ] 小额实盘验证通过

---
