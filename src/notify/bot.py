# Input: Telegram Bot API getUpdates, 命令处理器注册
# Output: 命令响应消息
# Pos: Telegram 命令控制入口（long polling）
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
Telegram Bot 命令接收器

职责：
- getUpdates long polling 接收用户命令
- 路由命令到注册的 handler
- 仅响应 allowed_chat_ids 中的消息

输入：
- Telegram Bot API 消息

输出：
- 命令响应消息
"""

import asyncio
from typing import Optional, Set, Dict, Any, Callable, Awaitable

import aiohttp

from src.utils.logger import get_logger


class TelegramBot:
    """Telegram Bot 命令接收器（getUpdates long polling）"""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: Set[str],
        proxy: Optional[str] = None,
        polling_timeout_s: int = 30,
        request_timeout_s: float = 35.0,
    ):
        """
        Args:
            token: Bot Token
            allowed_chat_ids: 允许发送命令的 chat_id 集合
            proxy: HTTP 代理地址
            polling_timeout_s: getUpdates long polling 超时（秒）
            request_timeout_s: HTTP 请求超时（秒），应大于 polling_timeout_s
        """
        self.token = token
        self.allowed_chat_ids = allowed_chat_ids
        self.proxy = proxy
        self.polling_timeout_s = polling_timeout_s
        self.request_timeout_s = request_timeout_s

        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._offset: int = 0
        self._handlers: Dict[str, Callable[[str], Awaitable[str]]] = {}

    def register_handler(self, command: str, handler: Callable[[str], Awaitable[str]]) -> None:
        """
        注册命令处理器。

        Args:
            command: 命令名（不含 /），如 "pause"
            handler: 异步处理函数，接收 args 字符串，返回响应文本
        """
        self._handlers[command] = handler

    async def start(self) -> None:
        """启动 polling 循环（作为后台任务运行）。"""
        self._running = True
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        self._session = aiohttp.ClientSession(timeout=timeout)
        logger = get_logger()

        # 刷掉启动前积压的历史消息，避免回放旧命令
        await self._flush_pending_updates()

        logger.info("Telegram Bot 命令接收器已启动")

        try:
            while self._running:
                try:
                    updates = await self._get_updates()
                    for update in updates:
                        await self._process_update(update)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Telegram Bot polling 异常: {type(e).__name__}: {e}")
                    await asyncio.sleep(5)
        finally:
            await self._close_session()

    def stop(self) -> None:
        """停止 polling。"""
        self._running = False

    async def close(self) -> None:
        """关闭 session。"""
        self.stop()
        await self._close_session()

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _flush_pending_updates(self) -> None:
        """启动时跳过所有积压消息，将 offset 推进到最新。"""
        if not self._session:
            return
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params: Dict[str, Any] = {"offset": -1, "timeout": 0}
        try:
            async with self._session.get(url, params=params, proxy=self.proxy) as resp:
                data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                result = data.get("result", [])
                if result:
                    self._offset = result[-1]["update_id"] + 1
                    get_logger().info(f"Telegram Bot 跳过 {self._offset} 之前的积压消息")
        except Exception as e:
            get_logger().warning(f"Telegram Bot flush 异常: {e}")

    async def _get_updates(self) -> list[Dict[str, Any]]:
        """调用 getUpdates API（long polling）。"""
        if not self._session:
            return []

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params: Dict[str, Any] = {
            "offset": self._offset,
            "timeout": self.polling_timeout_s,
            "allowed_updates": '["message"]',
        }

        async with self._session.get(url, params=params, proxy=self.proxy) as resp:
            data = await resp.json(content_type=None)

        if not isinstance(data, dict) or not data.get("ok"):
            return []

        result = data.get("result", [])
        if result:
            self._offset = result[-1]["update_id"] + 1
        return result

    async def _process_update(self, update: Dict[str, Any]) -> None:
        """处理单个 update。"""
        logger = get_logger()
        message = update.get("message")
        if not message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            logger.debug(f"Telegram Bot 忽略未授权 chat_id={chat_id}")
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        # 解析："/pause@botname BTCUSDT" -> command="pause", args="BTCUSDT"
        parts = text.split(maxsplit=1)
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        handler = self._handlers.get(command)
        if handler:
            try:
                response_text = await handler(args)
            except Exception as e:
                logger.error(f"Telegram Bot 命令处理异常: /{command} {args} - {e}")
                response_text = f"命令执行异常: {e}"
        else:
            response_text = f"未知命令: /{command}\n使用 /help 查看可用命令"

        await self._send_reply(chat_id, response_text)

    async def _send_reply(self, chat_id: str, text: str) -> None:
        """发送命令响应。"""
        if not self._session:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            async with self._session.post(url, json=payload, proxy=self.proxy) as resp:
                if resp.status != 200:
                    data = await resp.json(content_type=None)
                    get_logger().warning(f"Telegram Bot 回复失败: status={resp.status} resp={data}")
        except Exception as e:
            get_logger().warning(f"Telegram Bot 回复异常: {e}")
