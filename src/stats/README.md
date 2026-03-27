<!-- Input: trigger/attempt/outcome 事件与原始 MarketEvent -->
<!-- Output: 窗口化统计日志 -->
<!-- Pos: src/stats 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/stats 目录说明

统计与录制模块。<br>
`pressure_stats.py` 收集 orderbook_pressure 的 trigger/attempt/fill 与 mid-price 采样，按窗口输出结构化日志。<br>
`market_recorder.py` 以非阻塞方式录制 `bookTicker + depth10 + aggTrade` 原始事件到 JSONL，供后续离线回放调参。<br>
两者都不阻塞核心交易路径；前者纯内存，后者通过 queue + writer task 后台落盘。

## 文件清单

- `pressure_stats.py`：`PressureStatsCollector` — trigger/成功下单/首次成交/价格事件记录、窗口聚合、日志输出
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

## 当前经验性判读规则

以下内容用于辅助理解 `orderbook_pressure` 统计日志，属于当前样本上的经验性结论，不代表现网已经自动按此规则执行。

### 适用范围

- 观察对象：`[PRESSURE_STATS]` 日志
- 当前主样本：`DASH LONG`
- 当前样本口径：带 `active_triggers / passive_triggers / attempts / fills` 的新口径日志
- 当前样本起点：`2026-03-27 11:26:03` 之后
- 当前结论定位：工作假设，后续应随样本扩大持续复核

### 当前优先观察窗口

- 在线判读优先看 `window=5m`
- `1m` 变化快、噪音大，适合看短时切换，不适合作为唯一依据
- `15m` 滞后明显，容易混入前一段 regime 的惯性，不适合做即时执行判断

### 当前指标优先级

经验上，当前样本中的解释力排序为：

`5m passive_fill_rate` > `5m active_triggers / active_attempts` > `5m passive_triggers`

其中：

- `passive_fill_rate`：被动挂单是否真的至少成交过，优先代表“这套被动逻辑当前有没有 edge”
- `active_triggers / active_attempts`：主动 pressure 是否有真实跟进，代表“市场有没有顺着这个方向推进”
- `passive_triggers`：只表示被动 pressure 形态出现得多，不等于价格更有利；当前样本里更接近反向参考项

### 当前判读方式

- `5m passive_fill_rate > 0` 且 `5m active_triggers > 0`
  - 当前最强的一档，说明被动单能成交，且主动 pressure 也在推进，可以继续信 `orderbook_pressure`
- `5m passive_fill_rate > 0` 但 `5m active_triggers = 0`
  - 仍可参考，但信心次一级，更像“被动还有 edge，主动没明显跟进”
- `5m passive_fill_rate = 0` 且 `5m passive_triggers` 很高
  - 不要把“被动 pressure 很多”误判成利好；当前样本里这更像拥挤或磨损
- `5m passive_fill_rate = 0` 且 `5m active_triggers = 0`
  - 当前更接近“pressure 没有 edge”，不应继续死等这一路径自行改善

### 当前使用边界

- 这套规则用于辅助执行判断，不用于预测方向
- 这套规则更适合回答“现在还值不值得继续依赖 `orderbook_pressure` 平仓”
- 这套规则不应替代后续基于 `market_data_*.jsonl` 的离线回放分析
- 当线上样本显著增加后，应重新统计相关性，并按新数据修正本节内容

## 下一步分析计划

### 当前已经完成的分析

- 当前已完成的是 same-window 分析
- 具体口径：`window=1m/5m/15m` 的统计字段，与同一条日志里的 `price_chg` 做相关性观察
- 这回答的是“当前窗口里的 pressure 质量与同窗价格变化是否同向”
- 这不回答“未来下一个窗口会不会继续涨/跌”

### 下一步要做的分析

- 下一步优先做 lead-lag 分析
- 目标：验证当前窗口的 pressure 指标，是否对下一窗口的 `price_chg` 有预测性
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

1. 先做 `5m feature(t) -> 5m price_chg(t+1)`
2. 再做分组比较
   - `passive_fill_rate > 0` vs `= 0`
   - `active_triggers > 0` vs `= 0`
3. 再看排序是否稳定
   - `passive_fill_rate`
   - `active_triggers / active_attempts`
   - `passive_triggers`
4. 最后才考虑把结论用于更新经验性规则

### 样本门槛

- `5m` 新口径样本达到 `100` 个后，可以开始看第一版 lead-lag 结果
- `5m` 新口径样本达到 `300` 个后，才值得考虑更新当前经验性规则
- 如果中间停机较多、symbol 切换较多，优先拉长到 `3-5` 天再判断

### 后续离线回放的定位

- `[PRESSURE_STATS]` 的 lead-lag 分析：验证统计字段是否有预测性
- `market_data_*.jsonl` 离线回放：验证 `orderbook_pressure` 参数怎么调更合理
- 两者互补，不互相替代

### 当前结论的更新条件

出现以下任一情况时，应重新计算并更新“当前经验性判读规则”：

- `5m passive_fill_rate` 不再是最强正向指标
- `5m active_triggers / active_attempts` 的方向或排序发生明显变化
- `5m passive_triggers` 不再表现为稳定负向参考项
- `DASH LONG` 之外的样本加入后，结论与当前规则冲突
