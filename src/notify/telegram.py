# Input: token, chat_id, events, rate limit state
# Output: serialized Telegram delivery with retry_after handling
# Pos: Telegram notifier with fill details
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
Telegram 通知模块

职责：
- 发送成交通知
- 发送 WS 重连通知
- 发送风险兜底触发通知
- 发送失败时重试（不阻塞主链路）

输入：
- 关键事件

输出：
- Telegram 消息
"""

import asyncio
from typing import Optional, Any, Dict

import aiohttp

from src.utils.logger import get_logger


class TelegramNotifier:
    """Telegram 通知器"""

    def __init__(
        self,
        token: str,
        chat_id: str,
        enabled: bool = True,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        timeout_s: float = 10.0,
    ):
        """
        初始化 Telegram 通知器

        Args:
            token: Bot Token
            chat_id: 聊天 ID
            enabled: 是否启用
            max_retries: 最大重试次数
            proxy: HTTP 代理地址（可选）
            timeout_s: 单次请求超时（秒）
        """
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(enabled)
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy
        self.timeout_s = float(timeout_s)

        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._min_interval_s = 1.0
        self._next_send_time = 0.0
        self._cooldown_until = 0.0

        if self.enabled and (not self.token or not self.chat_id):
            get_logger().warning("Telegram 已启用但 token/chat_id 为空，将自动禁用 Telegram 通知")
            self.enabled = False

    async def notify_fill(
        self,
        symbol: str,
        side: str,
        mode: str,
        qty: str,
        avg_price: str,
        reason: str,
        position_before: str,
        position_after: str,
        role: Optional[str] = None,
        pnl: Optional[str] = None,
    ) -> None:
        """
        发送成交通知

        Args:
            symbol: 交易对
            side: 方向
            mode: 执行模式
            qty: 成交数量
            avg_price: 成交均价
            reason: 触发原因
            position_before: 成交前仓位
            position_after: 成交后仓位
            role: 成交角色（maker/taker）
            pnl: 已实现盈亏（格式化字符串）
        """
        short_symbol = symbol.split(":")[0]
        action = "平多" if side == "LONG" else "平空"
        mode_cn = {
            "MAKER_ONLY": "挂单模式",
            "AGGRESSIVE_LIMIT": "激进模式",
        }.get(mode, mode)

        role_str = ""
        if role:
            role_str = f"\n  角色: {role}"
        pnl_str = ""
        if pnl:
            pnl_str = f"\n  盈亏: {pnl}"

        text = (
            f"【已成交】{action}\n"
            f"  交易对: {short_symbol}\n"
            f"  成交: {qty} @ {avg_price}\n"
            f"  执行: {mode_cn}{role_str}{pnl_str}\n"
            f"  原因: {reason}\n"
            f"  仓位: {position_before} -> {position_after}"
        )
        await self._send_message(text)

    async def notify_open_alert(
        self,
        symbol: str,
        side: str,
        position_before: str,
        position_after: str,
    ) -> None:
        """
        发送开仓/加仓告警

        Args:
            symbol: 交易对
            side: 仓位方向（LONG/SHORT）
            position_before: 变更前仓位
            position_after: 变更后仓位
        """
        short_symbol = symbol.split(":")[0]
        action = "开多" if side == "LONG" else "开空"

        text = (
            f"【告警】{action}\n"
            f"  交易对: {short_symbol}\n"
            f"  仓位: {position_before} -> {position_after}"
        )
        await self._send_message(text)

    async def notify_reconnect(self, stream_type: str) -> None:
        """
        发送 WS 重连通知

        Args:
            stream_type: 数据流类型
        """
        text = f"【重连】{stream_type}\n  状态: 已重连"
        await self._send_message(text)

    async def notify_risk_trigger(
        self,
        symbol: str,
        position_side: str,
        dist_to_liq: str,
    ) -> None:
        """
        发送风险兜底触发通知

        Args:
            symbol: 交易对
            position_side: 仓位方向
            dist_to_liq: 强平距离
        """
        short_symbol = symbol.split(":")[0]
        action = "平多" if position_side == "LONG" else "平空"
        text = (
            f"【风险】{action}\n"
            f"  交易对: {short_symbol}\n"
            f"  强平距离: {dist_to_liq}"
        )
        await self._send_message(text)

    async def close(self) -> None:
        """关闭底层 HTTP session。"""
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _send_message(self, text: str) -> bool:
        """
        发送消息（带重试）

        Args:
            text: 消息内容

        Returns:
            True 如果发送成功
        """
        if not self.enabled:
            return True

        if not self.token or not self.chat_id:
            return False

        await self._ensure_session()

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        logger = get_logger()
        last_error: Optional[str] = None
        attempt = 0
        loop = asyncio.get_running_loop()

        async with self._send_lock:
            while attempt < self.max_retries:
                await self._wait_for_send_slot()
                try:
                    assert self._session is not None
                    async with self._session.post(url, json=payload, proxy=self.proxy) as resp:
                        data = await resp.json(content_type=None)
                        ok = bool(data.get("ok")) if isinstance(data, dict) else False

                        if resp.status == 200 and ok:
                            return True

                        if resp.status == 429:
                            retry_after = _extract_retry_after(data) or 5.0
                            self._cooldown_until = max(self._cooldown_until, loop.time() + retry_after)
                            logger.warning(
                                f"Telegram 触发限流，等待 retry_after={retry_after}s"
                            )
                            continue

                        last_error = f"status={resp.status} resp={data}"
                        attempt += 1
                        logger.warning(
                            f"Telegram 发送失败 attempt={attempt}/{self.max_retries}: {last_error}"
                        )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    last_error = f"TimeoutError after {self.timeout_s:.1f}s (proxy={self.proxy or 'none'})"
                    attempt += 1
                    logger.warning(
                        f"Telegram 发送超时 attempt={attempt}/{self.max_retries}: {last_error}"
                    )
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e!r} (proxy={self.proxy or 'none'})"
                    attempt += 1
                    logger.warning(
                        f"Telegram 发送异常 attempt={attempt}/{self.max_retries}: {last_error}"
                    )
                finally:
                    self._next_send_time = max(self._next_send_time, loop.time() + self._min_interval_s)

                if attempt < self.max_retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 5))

            logger.error(f"Telegram 最终发送失败: {last_error or 'unknown_error'}")
            return False

    async def _wait_for_send_slot(self) -> None:
        """等待可用发送窗口（限速 + 限流冷却）。"""
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait_until = max(self._next_send_time, self._cooldown_until)
        if wait_until > now:
            await asyncio.sleep(wait_until - now)

    async def _ensure_session(self) -> None:
        if self._session and not self._session.closed:
            return

        async with self._session_lock:
            if self._session and not self._session.closed:
                return

            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)


def _extract_retry_after(data: Any) -> Optional[float]:
    """从 Telegram 429 响应中提取 retry_after 秒数。"""
    if not isinstance(data, dict):
        return None
    params = data.get("parameters")
    if not isinstance(params, dict):
        return None
    retry_after = params.get("retry_after")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None
