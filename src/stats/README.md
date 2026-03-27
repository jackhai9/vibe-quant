<!-- Input: trigger/attempt/outcome 事件、原始 MarketEvent 与相关性分析口径 -->
<!-- Output: 窗口化统计日志、录制说明与判读规则 -->
<!-- Pos: src/stats 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/stats 目录说明

统计与录制模块。<br>
`pressure_stats.py` 收集 orderbook_pressure 的 trigger/attempt/fill 与 mid-price 采样，按窗口输出结构化日志，并基于配置指定的 rolling 样本输出经验性 `PRESSURE_REGIME` 状态（默认 `5m × 12`）。<br>
`market_recorder.py` 以非阻塞方式录制 `bookTicker + depth10 + aggTrade` 原始事件到 JSONL，供后续离线回放调参。<br>
两者都不阻塞核心交易路径；前者纯内存，后者通过 queue + writer task 后台落盘。

## 文件清单

- `pressure_stats.py`：`PressureStatsCollector` — trigger/成功下单/首次成交/价格事件记录、窗口聚合、rolling regime 状态评估与日志输出
- `market_recorder.py`：`MarketDataRecorder` — 原始市场数据录制、日切压缩、保留清理
- `__init__.py`：模块导出

## 运行说明

- 录制文件默认写入 `logs/market_data_YYYY-MM-DD.jsonl`
- 若设置 `VQ_LOG_DIR`，则与普通运行日志一起写入该目录
- 日切后的历史文件会压缩为 `market_data_YYYY-MM-DD.jsonl.gz`
- 当前活跃文件可用 `tail -n 5 logs/market_data_$(date +%F).jsonl` 查看
- 历史压缩文件可用 `gzip -dc logs/market_data_YYYY-MM-DD.jsonl.gz | head` 查看

## 常用命令

如果未设置 `VQ_LOG_DIR`，可直接使用下面这些命令：

```bash
ls -lh logs/market_data_*
```

```bash
tail -n 5 logs/market_data_$(date +%F).jsonl
```

```bash
rg '"type":"book_ticker"' logs/market_data_$(date +%F).jsonl | head
```

```bash
rg '"type":"depth"' logs/market_data_$(date +%F).jsonl | head
```

```bash
rg '"type":"agg_trade"' logs/market_data_$(date +%F).jsonl | head
```

查看压缩后的历史文件：

```bash
gzip -dc logs/market_data_2026-03-27.jsonl.gz | head
```

如果设置了 `VQ_LOG_DIR`，把上面的 `logs/` 替换成对应目录即可。

## JSONL 样例

bookTicker：

```json
{"ts":1711357200000,"type":"book_ticker","sym":"DASH/USDT:USDT","bid":"25.50","bid_qty":"100","ask":"25.51","ask_qty":"80"}
```

depth10：

```json
{"ts":1711357200000,"type":"depth","sym":"DASH/USDT:USDT","bids":[["25.50","100"]],"asks":[["25.51","80"]]}
```

aggTrade：

```json
{"ts":1711357200000,"type":"agg_trade","sym":"DASH/USDT:USDT","p":"25.50","q":"10.5","m":true}
```

## 在线 regime 判读

以下内容用于辅助理解 `orderbook_pressure` 的在线统计与 `PRESSURE_REGIME` 日志，属于当前样本上的经验性状态机，不代表现网已经直接按此状态执行交易决策。

### 适用范围

- 观察对象：`[PRESSURE_STATS]` 与 `[PRESSURE_REGIME]` 日志
- 当前主样本：`DASH LONG`
- 当前样本口径：带 `active_triggers / passive_triggers / attempts / fills` 的新口径日志
- 当前样本范围：`2026-03-27 11:26:03` 到 `2026-03-27 21:27:22`
- 当前样本量：`DASH LONG 5m = 113`，`DASH SHORT 5m = 43`
- 当前结论定位：工作假设，后续应随样本扩大持续复核

### 最近分析检查点

- 来源文件：`logs/vibe-quant_2026-03-27.log`
- 已纳入统计的最后一条新口径日志时间：`2026-03-27 21:27:22`
- 下次增量统计默认从 `2026-03-27 21:27:22` 之后继续
- 对 `same-window` 统计，不必每次从头重算
- 对 `lead-lag` 统计，增量续算时保留上一条 `5m` 样本即可，用来和新进入的第一条 `5m` 样本组成 `t -> t+1`

### 当前经验总结

- 旧的“`5m passive_fill_rate` 是最强正向确认项”只在早期样本中成立，后续已经出现明显漂移
- 截至当前检查点，`DASH LONG 5m` 中相对更稳定的信号是：
  - `active_attempts / active_triggers` 与 `price_chg` 仍保持弱正相关
  - `passive_triggers` 与 `price_chg` 继续偏负相关
  - `passive_fill_rate` 不再适合作为唯一核心依据，只保留为辅助项
- `DASH SHORT` 当前样本量仍偏小，不按 `DASH LONG` 的规则直接外推

### `1m / 5m / 15m` 的使用分工

- 当前“规则是否失效/漂移”的主判断窗口是 `5m`
- `1m` 也会持续观察，但主要用于 early warning 和短时异动；它对盘口微抖和成交噪音更敏感，不适合单独拿来定义规则失效
- `15m` 也会持续观察，但主要作为滞后确认；它更容易混入前一段 regime 的惯性，不适合做第一判断
- 因此，当前文档里提到的“经验规则开始漂移”“规则失效段”“规则恢复段”，默认都是优先基于 `5m` same-window 结果得出的结论
- `1m` 和 `15m` 的角色是：
  - `1m`：看是否出现更早的背离或突变
  - `5m`：做 primary regime judgment
  - `15m`：确认这种变化是否持续，而不是瞬时噪音

### `PRESSURE_REGIME` 状态机

在线状态机使用 `global.stats.pressure_regime_window_ms` 指定统计窗口，使用 `global.stats.pressure_regime_samples` 指定至少积累多少个窗口样本后开始判定；默认是最近 `12` 个 `5m` 样本。

核心观测量：

- `active_attempts_corr`
- `active_triggers_corr`
- `passive_triggers_corr`
- `passive_fill_rate_corr`（辅助项，不再作为首要依据）

状态定义：

- `effective`
  - 最近一段窗口里，`active_attempts / active_triggers` 与 `price_chg` 保持正向，`passive_triggers` 保持负向，说明原经验规则仍然成立
- `degrading`
  - 原经验关系开始走弱，但还没完全翻转，更像 regime 切换前的衰减段
- `failed`
  - 最近窗口里原经验关系已经不成立，说明当前 microstructure 规则失效，应把它视为 regime shift 警报，而不是继续沿用旧规则
- `recovering`
  - 在 `failed` 之后，经验关系重新转好，但还没稳定回到 `effective`

### 使用边界

- `PRESSURE_REGIME` 是在线 regime 预警，不是价格方向预测器
- 它更适合回答“当前这套 `orderbook_pressure` 经验规则还能不能信”
- `failed` 更像“市场结构在变”，不是“下一根一定下跌”
- `recovering` 更像“旧关系开始回来”，不是“立即恢复成顺风行情”
- 这套状态机不应替代后续基于 `market_data_*.jsonl` 的离线回放分析

## 下一步分析计划

### 当前已经完成的分析

- 当前已完成的是 same-window 分析
- 具体口径：`window=1m/5m/15m` 的统计字段，与同一条日志里的 `price_chg` 做相关性观察
- 这回答的是“当前窗口里的 pressure 质量与同窗价格变化是否同向”
- 这不回答“未来下一个窗口会不会继续涨/跌”
- 当前也已完成第一版 exploratory lead-lag 检查
- lead-lag 口径：`当前 5m 指标(t) -> 下一条 5m 日志的 price_chg(t+1)`
- 当前 lead-lag 样本仍偏小，结论只作为方向性参考，不作为定版规则

### 下一步要继续做的分析

- 继续扩大 lead-lag 样本
- 目标：验证当前窗口的 pressure 指标，是否稳定地对下一窗口的 `price_chg` 有预测性
- 主窗口仍优先看 `5m`

建议口径：

- 特征（当前窗口，记作 `t`）
  - `5m passive_fill_rate`
  - `5m active_triggers`
  - `5m active_attempts`
  - `5m passive_triggers`
- 目标（下一窗口，记作 `t+1`）
  - `next_5m price_chg`
  - 可同时保留二值标签：`next_5m_up = price_chg > 0`

### 分析顺序

1. 持续更新 `5m feature(t) -> 5m price_chg(t+1)`
2. 分开看 `same-window` 与 `lead-lag` 的排序是否稳定
3. 再做分组比较
   - `passive_fill_rate > 0` vs `= 0`
   - `active_triggers > 0` vs `= 0`
4. 最后才考虑把结论用于更新经验性规则

### 样本门槛

- 当前已在不足 `100` 个 `5m` 样本时做了第一版 exploratory lead-lag 检查，只用于验证方法和方向
- `5m` 新口径样本达到 `100` 个后，可以开始看更稳定的 lead-lag 结果
- `5m` 新口径样本达到 `300` 个后，才值得考虑更新当前经验性规则
- 如果中间停机较多、symbol 切换较多，优先拉长到 `3-5` 天再判断

### 后续离线回放的定位

- `[PRESSURE_STATS]` 的 lead-lag 分析：验证统计字段是否有预测性
- `market_data_*.jsonl` 离线回放：验证 `orderbook_pressure` 参数怎么调更合理
- 两者互补，不互相替代

### 当前结论的更新条件

出现以下任一情况时，应重新计算并更新“当前经验性判读规则”：

- `same-window` 下 `5m passive_fill_rate` 不再是最强正向指标
- `lead-lag` 下 `5m active_triggers / active_attempts` 不再表现为更强的延续性指标
- `5m passive_triggers` 不再表现为稳定负向参考项
- `DASH LONG` 之外的样本加入后，结论与当前规则冲突
