<!-- Input: pytest 测试用例 -->
<!-- Output: 模块行为与回归验证（含成交率反馈与 min_notional 边界） -->
<!-- Pos: tests 文件夹级说明与索引 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# tests 目录说明

pytest 测试集（含撤单分层、成交率反馈与保护止损回归）。<br>
覆盖配置、信号、执行、风控、WS 解析与通知格式。<br>
运行前无需外部依赖。

## 文件清单

- `test_config.py`：配置加载与合并测试（含 accel mult_percent）
- `test_exchange.py`：交易所适配器测试
- `test_execution.py`：执行引擎测试
- `test_logger.py`：日志系统测试
- `test_main_shutdown.py`：优雅退出测试
- `test_min_notional_reduce_only.py`：reduce-only minNotional 放行下单测试
- `test_notify_telegram.py`：Telegram 通知测试（含 429 冷却等待）
- `test_order_cleanup.py`：退出撤单隔离测试
- `test_post_only_retry.py`：post-only 拒单重试测试
- `test_protective_stop.py`：保护性止损测试
- `test_risk_manager.py`：风控与限速测试
- `test_signal.py`：信号引擎测试
- `test_ws_market.py`：市场 WS 测试
- `test_ws_user_data.py`：用户数据 WS 测试
- `__init__.py`：测试包初始化
