<!-- Input: WebSocket 订阅参数与连接配置 -->
<!-- Output: 行情/订单/仓位事件流与重连回调 -->
<!-- Pos: src/ws 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/ws 目录说明

WebSocket 数据流客户端。<br>
市场与用户数据分离订阅。<br>
重连容错与解析输出标准事件（含成交角色/盈亏）。

## 文件清单

- `market.py`：市场数据 WS（bookTicker/aggTrade/markPrice@1s）
- `user_data.py`：用户数据 WS（订单/仓位/杠杆更新）
- `__init__.py`：模块导出
