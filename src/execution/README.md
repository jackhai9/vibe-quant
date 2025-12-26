<!-- Input: ExitSignal 与执行配置 -->
<!-- Output: OrderIntent 下单意图与状态变更（含成交率/TTL 反馈） -->
<!-- Pos: src/execution 模块说明与索引 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/execution 目录说明

执行状态机与下单逻辑。<br>
支持 maker/aggressive 模式轮转与成交率反馈（含 TTL 覆盖）。<br>
管理普通订单撤单、冷却与 TTL（含成交角色/盈亏/手续费兜底）。

## 文件清单

- `engine.py`：执行引擎与状态机实现
- `__init__.py`：模块导出
