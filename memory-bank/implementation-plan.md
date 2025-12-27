<!-- Input: 实施任务与阶段 -->
<!-- Output: 可执行计划与里程碑 -->
<!-- Pos: memory-bank/implementation-plan -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->

# 实施计划：Binance U 本位永续 Hedge 模式 Reduce-Only 小单平仓执行器（无代码版指令清单）

> 约束：本计划只包含清晰、具体的分步指令；**不包含任何代码**。
> 每一步都提供"如何验证正确性"的测试。
> 先完成基础功能（可运行、可下单、可撤单、可安全减仓），再添加完整功能（模式轮转、倍数档位、风控、告警、可观测、部署）。

---

## 关键决策确认（2024-12 澄清）

以下决策已确认，贯穿整个实施过程：

| # | 问题 | 决定 |
|---|------|------|
| 1 | API 密钥管理 | 从环境变量读取（`BINANCE_API_KEY`、`BINANCE_API_SECRET`） |
| 2 | 订单状态获取 | WebSocket User Data Stream（低延迟，实时推送成交/撤单事件） |
| 3 | 部分成交对计数器影响 | 部分成交重置 `timeout_count`（有成交说明价格合理，继续 maker） |
| 4 | `base_lot_mult` | 可配置，放在 `global.execution.base_lot_mult`，默认值 1，支持 symbol 覆盖 |
| 5 | 多 symbol 架构 | 共享 WS 连接（combined streams）+ 每个 symbol+side 独立 asyncio Task |
| 6 | WS 重连策略 | 指数退避：初始 1s，倍数 2x，最大 30s，无限重试 |
| 7 | `stale_data_ms` 判定 | 任一数据流（bookTicker 或 aggTrade）更新即重置 |
| 8 | `minNotional` 处理 | 需要检查；不满足时增大 qty 直至满足 |
| 9 | 程序入口 | symbol 列表完全从 config.yaml 读取，无需命令行指定 |
| 10 | 优雅退出 | 收到 SIGINT/SIGTERM 时立即撤销所有挂单，然后退出 |

### WS 重连配置参考
```yaml
ws:
  reconnect:
    initial_delay_ms: 1000
    max_delay_ms: 30000
    multiplier: 2
    max_retries: null  # 无限重试
```

### 多 symbol 架构说明
- **市场数据 WS**：共享一个连接，使用 combined streams（`/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade/...`）
- **User Data Stream**：共享一个（账户级别）
- **ExecutionEngine**：每个 `symbol + positionSide` 独立 asyncio Task
- **RateLimiter**：全局共享（账户级别限速）

---

## 阶段 0：项目准备与"可验证的最小闭环"定义

### Step 0.1：确认运行目标与最小闭环范围（MVP）
**指令**
- 将 MVP 定义为：单 symbol、单侧（先 LONG 或先 SHORT）执行 `MAKER_ONLY` 的 reduce-only 平仓（reduce-only 语义约束：`positionSide + side + qty<=position`）；支持 WS 获取 best bid/ask + last trade；满足触发条件就挂单，TTL 到就撤单；仓位归零判定正确；全程日志可追踪。
- 明确“不做”的内容（MVP 阶段暂缓）：模式轮转、加速倍数、ROI 倍数、强平距离兜底、Telegram、全局限速、多 symbol 并发。

**测试（验收标准）**
- 用一份书面清单列出 “MVP 包含/不包含” 并在团队内评审通过（至少 1 位同事/你自己二次阅读无歧义）。
- 对照设计文档第 1、3、4、7 章，确认 MVP 覆盖了最核心链路：WS → 信号 → 下单/撤单 → 仓位收敛。

---

## 阶段 1：基础工程骨架（可运行、可观察、可重启）

### Step 1.1：建立目录结构与模块边界（不写实现细节）
**指令**
- 按设计文档“系统架构/模块”建立空文件与空模块（Config、WS、ExchangeAdapter、SignalEngine、ExecutionEngine、RiskManager、Logger/Notifier）。
- 定义每个模块的输入/输出数据结构（用文档描述即可）：例如 MarketEvent、PositionState、ExitSignal、OrderIntent、OrderResult。
- 明确“模块间不互相直接访问内部状态”，只通过定义的数据结构传递。

**测试**
- 静态检查：打开目录结构，能在 1 分钟内指出每个需求落在哪个模块（比如“WS 重连在 ws 模块”，“reduce-only 语义约束在执行/下单链路”）。
- 评审测试：让另一个人只看目录与接口描述，复述一遍事件流（WS→Signal→Engine→Exchange），能复述正确即通过。

### Step 1.2：配置文件与配置覆盖规则（global + symbols）
**指令**
- 实现配置加载（YAML 或等价），必须支持：
  - global 默认值
  - 按 symbol 覆盖（symbols.<symbol>.*）
- 把以下配置项纳入（至少先占位），与设计文档保持一致：
  - WS：stale_data_ms、reconnect（initial_delay_ms、max_delay_ms、multiplier）
  - 执行：order_ttl_ms、repost_cooldown_ms、min_signal_interval_ms、**base_lot_mult**（默认 1）
  - maker 定价策略：maker_price_mode、maker_n_ticks
  - 双保险：max_mult、max_order_notional
  - accel/roi 档位（先允许为空）
  - 风控阈值（先允许为空）
- API 密钥从环境变量读取：`BINANCE_API_KEY`、`BINANCE_API_SECRET`

**测试**
- 配置合并测试：准备两份配置（只有 global、以及带 symbol 覆盖），验证合并后的最终参数符合“symbol 覆盖优先”。
- 配置缺省测试：移除某些可选字段，系统仍能启动并使用默认值（例如 maker_price_mode 有默认）。

### Step 1.3：日志系统（按天滚动）先上线
**指令**
- 建立按天滚动日志，保证每条关键事件都有固定字段：timestamp、symbol、side、mode、state、reason、best_bid/ask、last_trade、order_id（如有）。
- 要求：启动、WS 连接、收到行情、触发信号、下单、撤单、成交、异常、重连都必须写日志。

**测试**
- 启动测试：运行程序至少 2 分钟，日志文件生成且持续追加。
- 可读性测试：随机抽取 20 行日志，能还原出“发生了什么动作”（至少能区分：下单/撤单/成交/重连）。

---

## 阶段 2：交易所规则与仓位读取（安全下单的前提）

### Step 2.1：用 ccxt 拉取 markets 并提取交易规则
**指令**
- 实现一次性拉取交易规则（tickSize、stepSize、minQty、minNotional）。
- 将规则缓存到内存，并在 symbol 切换时可更新。
- 明确"规整函数"的行为：
  - 价格按 tickSize 规整
  - 数量按 stepSize 规整
  - 数量不得低于 minQty
  - **minNotional 检查**：若 `qty * price < minNotional`，增大 qty 直至满足（按 stepSize 向上取整）

**测试**
- 规则存在性测试：对目标 symbol，日志打印出 tickSize/stepSize/minQty/minNotional（数值非空且合理）。
- 规整逻辑测试：选 5 组随机价格/数量（包含边界：略小于 minQty、非 stepSize 倍数、不满足 minNotional），人工核对规整结果符合预期。

### Step 2.2：读取 Hedge 模式下 LONG/SHORT 仓位
**指令**
- 实现持仓读取，能够区分 LONG 与 SHORT 两侧的 positionAmt。
- 明确“仓位完成条件”函数：
  - 若按 stepSize 规整后为 0 或 abs(position_amt) < minQty → 视为完成
  - 否则可继续减仓

**测试**
- 空仓测试：当账户无该 symbol 仓位时，系统日志必须明确显示“已完成/无仓位，不下单”。
- 小仓测试：人为制造一个接近 minQty 的小仓位（或用模拟数据模式），验证系统按规则判定“可交易/不可交易余量”的分界正确。

---

## 阶段 3：WebSocket 行情（best bid/ask + last trade）与数据新鲜度

### Step 3.1：接入 Binance 官方 WS（bookTicker + aggTrade）
**指令**
- 建立 WS 连接并订阅：
  - top-of-book：best bid/ask
  - trades：last trade（推荐 aggTrade）
- 将收到的事件统一转为 MarketEvent（包含 symbol、时间戳、best_bid/ask、last_trade_price）。

**测试**
- 连接稳定性测试：连续运行 10 分钟不中断，日志中持续有行情更新。
- 数据完整性测试：日志中 best_bid < best_ask 恒成立（偶发异常需记录并丢弃该条数据）。

### Step 3.2：实现数据陈旧（stale）检测
**指令**
- 对每个 symbol 维护"最近一次收到行情更新时间戳"。
- **任一数据流更新即重置**：bookTicker 或 aggTrade 任一更新都重置 stale 计时器。
- 若超过 stale_data_ms 未更新，ExecutionEngine 必须停止下单并记录日志原因。

**测试**
- 人工断网/暂停 WS（或切断 WS 线程）测试：stale 触发后不应再出现下单/撤单动作日志。
- 恢复网络后测试：WS 恢复更新后，系统能恢复正常评估信号并继续（不要求立即下单，但必须解除 stale 状态）。

### Step 3.3：接入 User Data Stream（订单状态推送）
**指令**
- 建立 User Data Stream 连接（需先通过 REST 获取 listenKey）。
- 监听 `ORDER_TRADE_UPDATE` 事件，获取订单成交/部分成交/撤单状态。
- listenKey 需每 30 分钟续期一次（Binance 要求）。

**测试**
- 成交事件测试：下单后观察日志，确认收到 `ORDER_TRADE_UPDATE` 事件且状态正确（FILLED/PARTIALLY_FILLED/CANCELED）。
- listenKey 续期测试：运行超过 30 分钟，确认 User Data Stream 持续可用。

---

## 阶段 4：信号层（触发条件）先实现原始两类条件

### Step 4.1：维护 previous_trade_price 与 last_trade_price
**指令**
- 为每个 symbol 维护上一次成交价与最近成交价，保证更新顺序正确。
- 在没有足够 trade 数据时（例如还没有 previous），信号层必须返回“不可判定”并记录原因（避免误触发）。

**测试**
- 冷启动测试：程序刚启动前几条 trade 到来时，不应立刻触发信号（直到具备 previous + last）。
- 连续更新测试：观察日志，previous 在每次更新后都等于上一条 last（顺序无错）。

### Step 4.2：实现 LONG/SHORT 的 exit_condition_met
**指令**
- 完整实现设计文档第 3.1/3.2 的原始条件。
- 输出 ExitSignal 时必须包含 reason（例如 long_primary / long_bid_improve / short_primary / short_ask_improve）。

**测试**
- 逻辑覆盖测试：用离线“事件序列样例”（人工构造 6 组 market event 参数即可，不需要代码）逐条推演，确认每种 reason 都能被触发一次，且不会互相冲突。
- 误触发测试：构造明显不满足条件的序列，确认不会产生 ExitSignal。

---

## 阶段 5：执行层（MVP）：MAKER_ONLY + reduce-only 语义约束 + TTL 撤单

### Step 5.1：订单意图（OrderIntent）与安全参数固定
**指令**
- 对 Hedge 模式强制绑定：
  - LONG 平仓：side=SELL + positionSide=LONG + `qty<=position_amt(LONG)`
  - SHORT 平仓：side=BUY + positionSide=SHORT + `qty<=abs(position_amt(SHORT))`
- maker 订单固定使用 post-only（GTX 或等价方式），并记录当前 execution_mode=MAKER_ONLY。

**测试**
- 参数正确性测试：在日志中打印每笔下单的关键参数（不含密钥），人工核对 positionSide/side/qty<=position 每次都正确。
- 反向开仓防护测试：在已有 LONG 仓位时触发平仓，不应导致 SHORT 仓位增加（通过下单后持仓读取验证）。

### Step 5.2：maker 定价策略（maker_price_mode）落地
**指令**
- 实现 maker 定价的三种模式（at_touch / inside_spread_1tick / custom_ticks）。
- 对价格做 tickSize 规整；若规整导致价格越过合理区间（例如 inside_spread 变成 >= best_ask），要记录并回退到更保守定价（例如 at_touch）。

**测试**
- 三模式一致性测试：切换配置后，日志中的挂单价应符合各模式定义（人工核对 10 次下单样本）。
- 边界测试：当点差极小（接近 1 tick）时，不应出现“价格算出来在价差外导致拒单后无限重试”的情况（观察 2 分钟，拒单/取消次数应受控）。

### Step 5.3：TTL 超时撤单与冷却
**指令**
- 下单后进入 WAIT，通过 User Data Stream 监听订单状态，直到：
  - 完全成交：结束该笔，回到 IDLE
  - TTL 到：撤单，进入 COOLDOWN（repost_cooldown_ms），再回到 IDLE
- 对部分成交：
  - 记录已成交数量
  - **部分成交重置 timeout_count**（有成交说明价格合理）
  - TTL 到仍撤掉剩余，下一轮继续以剩余仓位为准

**测试**
- 撤单路径测试：将 TTL 设置较短并选择不易成交的挂单价，验证每次 TTL 到都会出现撤单日志。
- 部分成交测试：制造容易部分成交的环境（小单但价位接近成交），确认日志中能反映部分成交并在 TTL 后撤剩余，最终仓位逐步降低。

### Step 5.4：仓位收敛结束判定（不留尘埃）
**指令**
- 每次下单/撤单/成交后都重新读取仓位，并用“完成条件函数”判断是否停止。
- 达到完成条件后：停止该侧执行，日志写明“DONE：不可交易余量”。

**测试**
- 收敛测试：以一个可控小仓位运行，直到系统停止；最终仓位读取应为 0 或 < minQty（符合定义）。
- 不死循环测试：在剩余仓位 < minQty 时，系统不应继续尝试下单（观察至少 1 分钟无下单日志）。

---

## 阶段 6：基础健壮性（WS 重连 + 状态校准）——仍属于基础功能

### Step 6.1：WS 断线自动重连
**指令**
- 断线检测：捕获 WS 关闭/异常；进入重连循环。
- **指数退避策略**：
  - 初始延迟：1 秒
  - 倍数：2x（1s → 2s → 4s → 8s → 16s → 30s）
  - 最大延迟：30 秒
  - 最大重试：无限（交易系统必须持续运行）
- 重连成功后重置延迟为初始值。
- 重连成功必须记录日志（并作为后续 Telegram 事件候选）。

**测试**
- 断线注入测试：手动断网/杀 WS 连接，确认系统能自动重连并继续接收行情。
- 重连后不下单错误测试：重连瞬间可能出现数据空窗，确认 stale 机制能阻止乱下单。

### Step 6.2：重连后 REST 校准（positions + rules）
**指令**
- 每次 WS 重连成功后，执行一次校准：
  - 重新拉取一次 positions（确认仓位真实值）
  - 确保 markets 规则存在（tick/step/minQty）
- 校准期间暂停下单，校准完成后再恢复。

**测试**
- 校准一致性测试：在重连前后记录仓位日志，重连后仓位应与交易所一致（允许微小延迟，但不能出现"凭旧状态继续下单"）。
- 暂停测试：校准期间不应出现下单/撤单日志。

### Step 6.3：优雅退出（Graceful Shutdown）
**指令**
- 注册 SIGINT/SIGTERM 信号处理器。
- 收到信号后：
  1. 停止接受新的信号触发
  2. 立即撤销所有当前挂单（并发撤单）
  3. 等待撤单确认（超时 5 秒）
  4. 关闭 WS 连接
  5. 退出进程

**测试**
- 优雅退出测试：在有挂单时按 Ctrl+C，确认挂单被撤销后才退出（观察日志）。
- 无挂单退出测试：无挂单时按 Ctrl+C，应快速退出。

---

# 以上为"基础功能（MVP+健壮性）"完成点
完成上述阶段后，你应该已经拥有：
- 可运行的 WS 行情接入（best bid/ask + last trade）
- User Data Stream 订单状态推送
- 原始两类触发条件
- maker-only reduce-only 语义约束 拆单减仓（TTL 撤单 + 冷却）
- 正确的 Hedge 仓位识别与不留尘埃收敛
- WS 断线重连（指数退避）+ 校准
- 优雅退出（撤销挂单后退出）
- 可追踪日志

---

## 阶段 7：完整功能追加 1 —— 执行模式轮转（maker → 激进限价）

### Step 7.1：加入 execution_mode 与超时计数器
**指令**
- 为每个 symbol+side 维护：
  - execution_mode（初始 MAKER_ONLY）
  - maker_timeout_count、aggr_timeout_count、aggr_fill_count
- 把模式轮转阈值加入配置并生效：
  - maker_timeouts_to_escalate
  - aggr_fills_to_deescalate / aggr_timeouts_to_deescalate

**测试**
- 轮转触发测试：人为设置 maker 难以成交、TTL 较短，观察 maker 连续超时后切到 AGGRESSIVE_LIMIT 的日志。
- 轮转回退测试：AGGRESSIVE_LIMIT 成交一次后应按配置回到 MAKER_ONLY，并记录原因。

### Step 7.2：实现 AGGRESSIVE_LIMIT 定价与下单
**指令**
- AGGRESSIVE_LIMIT 使用 LIMIT 非 post-only：
  - LONG 平仓：price = best_bid
  - SHORT 平仓：price = best_ask
- 不依赖下发 `reduceOnly`（交易所限制），reduce-only 语义由 `positionSide + side + qty<=position` 约束保证，且 positionSide 必须正确。

**测试**
- 成交效率测试：与 maker-only 对比，AGGRESSIVE_LIMIT 下单应显著更易成交（在同等市场条件下）。
- 安全参数测试：日志抽样核对 positionSide/side/qty<=position 在 AGGRESSIVE_LIMIT 下仍无误。

### Step 7.3：MARKET（不实现）
**指令**
- 不实现 `MARKET` 订单，避免在极端行情下出现不可控滑点；统一采用 `AGGRESSIVE_LIMIT` + 短 TTL 超时撤单 + 重试来保证成交。
- 删除 `allow_market` / `market_enable_liq_threshold` 配置与相关分支（避免配置误导与代码分叉）。

**测试**
- 静态检查：仓库中不应再出现 `allow_market` 配置字段。
- 行为检查：风险触发时最多升级到 `AGGRESSIVE_LIMIT`，不会出现 `MARKET` 下单。

---

## 阶段 8：完整功能追加 2 —— 加速倍数（滑动窗口）与 ROI 倍数档位

### Step 8.1：滑动窗口 ret（2 秒）与档位匹配
**指令**
- 为每个 symbol 维护滑动窗口价格序列（基于 last_trade_price）。
- 按配置档位计算 accel_mult（多档可配置，取最高满足档）。
- 加速只影响 qty 片大小，不影响模式选择（除非你明确配置“加速也可触发升级”为后续扩展）。

**测试**
- 档位命中测试：人工构造一组价格序列（口算 ret），验证系统选择了正确档位倍数（通过日志输出 accel_mult）。
- 噪声鲁棒测试：在无明显趋势时 ret 在阈值附近抖动，不应导致倍数频繁剧烈跳变（可加可选的“倍数最小保持时间”作为后续优化项）。

### Step 8.2：ROI 档位倍数与口径确认
**指令**
- 明确 ROI 的实现口径（以交易所/ccxt 可取字段为准），并把该口径写入 README（避免未来误解）。
- 按配置档位计算 roi_mult（取最高满足档）。

**测试**
- 口径一致性测试：在同一持仓下多次读取 ROI，值应稳定且与交易所界面趋势一致（允许小差异但不能反号）。
- 档位命中测试：人为设置阈值很低，确认 roi_mult 能被触发并在日志中体现。

### Step 8.3：倍数乘法合成 + 双保险生效
**指令**
- final_mult = base * roi_mult * accel_mult
- 应用 max_mult 截断
- 应用 max_order_notional 缩量（强烈建议保持开启）

**测试**
- 极端倍数测试：把档位倍数设置很大，确认最终 qty 仍被 max_mult / max_order_notional 限制住，不会出现“单笔异常巨大”。
- 回归测试：在正常倍数下 qty 不应被过度削减（日志中能看到限制是否触发）。

---

## 阶段 9：完整功能追加 3 —— 风控兜底（强平距离）+ 全局限速

### Step 9.1：强平距离 dist_to_liq 计算与触发
**指令**
- 读取 mark_price 与 liquidation_price，计算 dist。
- dist <= liq_distance_threshold：
  - 记录风险触发日志
  - 强制模式至少切换到 AGGRESSIVE_LIMIT
  - 不使用 `MARKET` 订单

**测试**
- 触发路径测试（可以用模拟数据或纸面演练）：给出 dist 数值，确认进入正确模式与正确 reason。
- 不误触发测试：dist 大于阈值时不应进入风险分支。

### Step 9.2：全局限速（orders/cancels 每秒上限）
**指令**
- 实现全局 rate limiter，超过上限时延迟或丢弃低优先级动作（普通 maker 优先让位于风险兜底动作）。
- 记录限速触发日志（否则排障困难）。

**测试**
- 压力测试：将 TTL 调很短并让信号高频触发，验证系统不会超过预设的 orders/cancels 速率（通过日志统计）。
- 优先级测试：当风险触发时，即使限速紧张，也应优先执行兜底动作。

### Step 9.3：仓位保护性止损（交易所端条件单）
**指令**
- 目标：为每个“有持仓”的 `symbol + positionSide` 永远维护 1 张“保护性止损单”，用于防程序崩溃/休眠/断网等意外。
- 订单类型：`STOP_MARKET`，并使用 `closePosition=true`（关闭该侧全部仓位）。
- 触发类型：使用 `MARK_PRICE`（标记价格触发），避免被短时成交价刺破误触发。
- `stopPrice` 计算（按 `liquidation_price` 反推）：
  - `dist_to_liq = abs(mark_price - liquidation_price) / mark_price`
  - 保护阈值 `D = protective_stop.dist_to_liq`（默认 0.01）
  - LONG：`stopPrice = liquidation_price / (1 - D)`（SELL stop）
  - SHORT：`stopPrice = liquidation_price / (1 + D)`（BUY stop）
- 可配置：
  - `global.risk.protective_stop.enabled`
  - `global.risk.protective_stop.dist_to_liq`
  - `symbols.<symbol>.risk.protective_stop_dist_to_liq`（按 symbol 覆盖）
- 订单归属：
  - 使用稳定的 `newClientOrderId`（跨 run 不变），便于进程重启后发现/续管，避免重复挂单。
- 同步策略：
  - 启动后：拉取 raw openOrders（必要时回退 ccxt openOrders）并合并 openAlgoOrders，确保保护止损存在且 stopPrice 正确
  - WS 重连校准后：同上
  - 收到 `ACCOUNT_UPDATE` 且仓位变化：触发一次 debounce 同步
  - 仓位归零：撤销该侧保护止损单（避免误触发导致开仓）

**测试**
- 单元测试：在 mock exchange 下验证：
  - 无保护单时会创建
  - 无仓位时会撤销
  - stopPrice 规整（LONG 向上、SHORT 向下）正确
- 手工验证（小额）：下单后在 Binance 界面看到对应 `STOP_MARKET closePosition` 条件单；强杀进程后该单仍存在。

---

## 阶段 10：完整功能追加 4 —— Telegram（成交/重连/风险触发）

### Step 10.1：Telegram 通知通道打通
**指令**
- 仅发送三类事件（与你确认一致）：
  - 成交
  - WS 断线重连成功
  - 风险兜底触发
- 发送失败要重试（有限次数）并写日志，但不能阻塞主执行链路。

**测试**
- 冒烟测试：触发一次成交/重连/风险事件，Telegram 必须收到对应消息。
- 稳定性测试：断网导致发送失败时，系统仍持续运行，日志记录失败与重试。

---

## 阶段 11：发布与运维（可选但推荐）

### Step 11.1：运行方式与重启策略
**指令**
- 选择 systemd 或 Docker 作为生产运行方式（建议先 systemd）。
- 配置“异常退出自动重启”，并将日志目录持久化。

**测试**
- 重启测试：手动终止进程，系统应自动重启并恢复 WS、校准仓位后继续运行。
- 配置热更新测试（若实现）：修改 symbol 覆盖参数后，系统在不中断进程的情况下应用新参数，并写日志说明“配置已更新”。

---

## 交付清单（每个里程碑的"可验收输出物"）

- MVP 交付：
  - 配置加载与覆盖 OK（含 base_lot_mult、API 密钥从环境变量）
  - WS 行情接入 OK（best bid/ask + last trade）
  - User Data Stream OK（订单状态推送、listenKey 续期）
  - 信号（原始两类）OK
  - maker-only reduce-only 语义约束 下单/TTL 撤单/冷却 OK（部分成交重置计数器）
  - Hedge 仓位识别与收敛结束 OK（含 minNotional 检查）
  - WS 重连（指数退避）+ 校准 OK
  - 优雅退出 OK（撤销挂单后退出）
  - 日志滚动 OK

- 完整功能交付：
  - 模式轮转 OK（maker ↔ 激进限价 ↔（可选）市价）
  - accel/roi 倍数档位 OK
  - 风控兜底 + 限速 OK
  - Telegram OK
  - 多 symbol 并发 OK（共享 WS + 独立状态机）
  - 部署运维 OK

---
