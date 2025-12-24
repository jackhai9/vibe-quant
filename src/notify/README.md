<!-- Input: 通知事件与凭证 -->
<!-- Output: 消息推送结果 -->
<!-- Pos: src/notify 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/notify 目录说明

通知渠道封装。<br>
当前仅支持 Telegram Bot。<br>
发送串行化并遵守限流等待。

## 文件清单

- `telegram.py`：Telegram 通知实现
- `__init__.py`：模块导出
