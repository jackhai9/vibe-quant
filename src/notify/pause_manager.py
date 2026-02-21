# Input: pause/resume 命令（全局或 per-symbol）
# Output: 暂停状态查询接口
# Pos: 执行暂停/恢复的状态管理中枢
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
暂停状态管理器

职责：
- 管理全局暂停 + per-symbol 暂停状态
- 提供 is_paused() 零开销查询
- 暂停时触发撤单回调

输入：
- /pause, /resume 命令

输出：
- 暂停状态查询、操作结果
"""

import asyncio
from datetime import datetime
from typing import Optional, Set, Callable, Awaitable, Dict, Any

from src.utils.logger import get_logger


class PauseManager:
    """执行暂停状态管理器"""

    def __init__(
        self,
        on_pause_callback: Optional[Callable[[Optional[str]], Awaitable[None]]] = None,
    ):
        """
        Args:
            on_pause_callback: 暂停时的回调（用于触发撤单），参数为 symbol（None 表示全局）
        """
        self._global_paused: bool = False
        self._paused_symbols: Set[str] = set()
        self._lock = asyncio.Lock()
        self._on_pause = on_pause_callback
        self._global_paused_at: Optional[datetime] = None
        self._symbol_paused_at: Dict[str, datetime] = {}

    def is_paused(self, symbol: Optional[str] = None) -> bool:
        """
        检查是否处于暂停状态（纯内存读，无锁）。

        Args:
            symbol: 交易对。None 表示仅检查全局。

        Returns:
            True 如果该 symbol（或全局）已暂停
        """
        if self._global_paused:
            return True
        if symbol and symbol in self._paused_symbols:
            return True
        return False

    async def pause(self, symbol: Optional[str] = None) -> str:
        """
        暂停执行。

        Args:
            symbol: 指定 symbol 暂停。None 表示全局暂停。

        Returns:
            操作结果描述
        """
        logger = get_logger()
        should_callback = False

        async with self._lock:
            now = datetime.now()
            if symbol is None:
                if self._global_paused:
                    return "全局已处于暂停状态"
                self._global_paused = True
                self._global_paused_at = now
                should_callback = True
                logger.info("全局暂停已启用")
            else:
                if self._global_paused:
                    return f"全局已暂停，无需单独暂停 {symbol}"
                if symbol in self._paused_symbols:
                    return f"{symbol} 已处于暂停状态"
                self._paused_symbols.add(symbol)
                self._symbol_paused_at[symbol] = now
                should_callback = True
                logger.info(f"暂停已启用: {symbol}")

        # 在 lock 外执行回调（避免死锁）
        callback_ok = True
        if should_callback and self._on_pause:
            try:
                await self._on_pause(symbol)
            except Exception as e:
                logger.error(f"暂停回调执行失败: {e}")
                callback_ok = False

        if symbol is None:
            cancel_msg = "所有挂单已撤销" if callback_ok else "挂单撤销失败，请手动检查"
            return f"已暂停全局执行，{cancel_msg}\n注意：暂停期间不执行强平兜底，保护性止损仍在交易所端生效"
        short = symbol.split(":")[0]
        cancel_msg = "相关挂单已撤销" if callback_ok else "挂单撤销失败，请手动检查"
        return f"已暂停 {short}，{cancel_msg}"

    async def resume(self, symbol: Optional[str] = None) -> str:
        """
        恢复执行。

        Args:
            symbol: 指定 symbol 恢复。None 表示全局恢复（同时清除所有 per-symbol 暂停）。

        Returns:
            操作结果描述
        """
        logger = get_logger()

        async with self._lock:
            if symbol is None:
                if not self._global_paused and not self._paused_symbols:
                    return "当前未处于暂停状态"
                self._global_paused = False
                self._global_paused_at = None
                self._paused_symbols.clear()
                self._symbol_paused_at.clear()
                logger.info("全局暂停已解除")
                return "已恢复全局执行"
            else:
                if symbol not in self._paused_symbols:
                    if self._global_paused:
                        return "当前为全局暂停，请使用 /resume 恢复全局"
                    return f"{symbol} 未处于暂停状态"
                self._paused_symbols.discard(symbol)
                self._symbol_paused_at.pop(symbol, None)
                short = symbol.split(":")[0]
                logger.info(f"暂停已解除: {symbol}")
                return f"已恢复 {short} 执行"

    def get_status(self) -> Dict[str, Any]:
        """返回当前暂停状态摘要。"""
        return {
            "global_paused": self._global_paused,
            "global_paused_at": self._global_paused_at,
            "paused_symbols": dict(self._symbol_paused_at),
        }
