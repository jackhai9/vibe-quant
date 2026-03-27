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
