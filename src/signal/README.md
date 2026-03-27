<!-- Input: 行情状态与仓位 -->
<!-- Output: ExitSignal 信号、signal 日志判读与 pressure active burst pacing 说明 -->
<!-- Pos: src/signal 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/signal 目录说明

信号评估与节流。<br>
按 symbol 在 `orderbook_price` / `orderbook_pressure` 两条互斥路径间评估退出条件。<br>
`orderbook_price` 基于 trade 动能 + bookTicker 判断，生成公共 accel/ROI sizing context，并默认通过 `execution.use_roi_mult` / `execution.use_accel_mult` 启用对应倍数。<br>
`orderbook_pressure` 基于 bookTicker 顶档量 + depth10 档位，生成带 `price/ttl/cooldown/qty_policy` 覆盖的信号，并附带固定片大小 jitter/anti-repeat、可选公共 ROI/accel modifiers 与 active burst pacing 元数据。<br>
active burst 命中 pause 时会输出 `INFO` 级结构化日志，方便直接在 console 观察；pause 期间的跳过说明保留在 `DEBUG` 文件日志中。<br>
`[SIGNAL]` INFO 日志按“语义变化 + 5 秒心跳”去重：`orderbook_price` 观察 `reason + roi_mult + accel_mult`，`orderbook_pressure` 观察 `reason + execution_preference + price_override + roi_mult + accel_mult`；盘口微抖保留在 DEBUG/原始市场数据里，不再直接刷满 console。<br>
输出 ExitSignal 给执行引擎。

## 文件清单

- `engine.py`：信号判断、倍数计算、盘口量 dwell 与来源 freshness 维护，以及 pressure TTL/cooldown jitter 和 active burst pacing
- `__init__.py`：模块导出
