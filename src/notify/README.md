<!-- Input: 通知事件与凭证 -->
<!-- Output: 消息推送结果、命令接收与暂停控制 -->
<!-- Pos: src/notify 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/notify 目录说明

通知渠道封装与运行时控制。<br>
支持 Telegram Bot 单向通知和双向命令控制。<br>
发送串行化并遵守限流等待（含成交角色/盈亏/手续费）。

## 文件清单

- `telegram.py`：Telegram 通知实现（单向发送）
- `bot.py`：Telegram Bot 命令接收器（getUpdates long polling，双向交互）
- `pause_manager.py`：暂停状态管理器（全局/per-symbol 暂停控制）
- `__init__.py`：模块导出
