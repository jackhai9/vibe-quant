<!-- Input: trigger/attempt/outcome 事件、bookTicker 价格 -->
<!-- Output: 窗口化统计日志 -->
<!-- Pos: src/stats 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/stats 目录说明

orderbook_pressure 策略旁路统计。<br>
收集主动/被动 trigger、成功下单次数、订单首次成交、mid-price 采样，按窗口（1m/5m/15m）聚合输出结构化日志。<br>
用于探索主动触发频率、被动成交率、价格走势三者的相关性。<br>
纯内存 deque 环形缓冲区，不侵入核心交易路径，进程重启后清零。

## 文件清单

- `pressure_stats.py`：`PressureStatsCollector` — trigger/成功下单/首次成交/价格事件记录、窗口聚合、日志输出
- `__init__.py`：模块导出
