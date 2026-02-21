# Input: pause/resume 命令（全局或 per-symbol，可选时长参数）
# Output: 暂停状态查询接口（含定时恢复信息）
# Pos: 执行暂停/恢复的状态管理中枢
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
暂停状态管理器

职责：
- 管理全局暂停 + per-symbol 暂停状态
- 支持定时暂停（到期自动恢复）
- 提供 is_paused() 零开销查询
- 暂停时触发撤单回调

输入：
- /pause, /resume 命令（含可选时长）

输出：
- 暂停状态查询、操作结果（含预计恢复时间）
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Set, Callable, Awaitable, Dict, Any

from src.utils.logger import get_logger


class PauseManager:
    """执行暂停状态管理器"""

    def __init__(
        self,
        on_pause_callback: Optional[Callable[[Optional[str]], Awaitable[None]]] = None,
        on_auto_resume_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """
        Args:
            on_pause_callback: 暂停时的回调（用于触发撤单），参数为 symbol（None 表示全局）
            on_auto_resume_callback: 定时恢复完成后的回调（用于通知），参数为 resume 结果消息
        """
        self._global_paused: bool = False
        self._paused_symbols: Set[str] = set()
        self._lock = asyncio.Lock()
        self._on_pause = on_pause_callback
        self._on_auto_resume = on_auto_resume_callback
        self._global_paused_at: Optional[datetime] = None
        self._symbol_paused_at: Dict[str, datetime] = {}
        # 定时恢复：key=symbol（None 表示全局）
        self._resume_tasks: Dict[Optional[str], asyncio.Task[None]] = {}
        self._auto_resume_at: Dict[Optional[str], datetime] = {}

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

    async def pause(self, symbol: Optional[str] = None, duration_s: Optional[float] = None) -> str:
        """
        暂停执行。

        Args:
            symbol: 指定 symbol 暂停。None 表示全局暂停。
            duration_s: 暂停时长（秒）。None 表示无限期。

        Returns:
            操作结果描述
        """
        logger = get_logger()
        should_callback = False

        async with self._lock:
            now = datetime.now()
            if symbol is None:
                if self._global_paused:
                    # 已暂停时传入 duration：更新定时器
                    if duration_s is not None:
                        ok = self._try_schedule_resume(None, duration_s, now)
                        if ok:
                            return f"全局已处于暂停状态，已更新定时恢复: {self._format_duration(duration_s)}"
                        return "全局已处于暂停状态，定时恢复设置失败，需手动 /resume"
                    return "全局已处于暂停状态"
                self._global_paused = True
                self._global_paused_at = now
                should_callback = True
                timer_ok = True
                if duration_s is not None:
                    timer_ok = self._try_schedule_resume(None, duration_s, now)
                logger.info("全局暂停已启用")
            else:
                if self._global_paused:
                    return f"全局已暂停，无需单独暂停 {symbol}"
                if symbol in self._paused_symbols:
                    if duration_s is not None:
                        ok = self._try_schedule_resume(symbol, duration_s, now)
                        short = symbol.split(":")[0]
                        if ok:
                            return f"{short} 已处于暂停状态，已更新定时恢复: {self._format_duration(duration_s)}"
                        return f"{short} 已处于暂停状态，定时恢复设置失败，需手动 /resume"
                    return f"{symbol} 已处于暂停状态"
                self._paused_symbols.add(symbol)
                self._symbol_paused_at[symbol] = now
                should_callback = True
                timer_ok = True
                if duration_s is not None:
                    timer_ok = self._try_schedule_resume(symbol, duration_s, now)
                logger.info(f"暂停已启用: {symbol}")

        # 在 lock 外执行回调（避免死锁）
        callback_ok = True
        if should_callback and self._on_pause:
            try:
                await self._on_pause(symbol)
            except Exception as e:
                logger.error(f"暂停回调执行失败: {e}")
                callback_ok = False

        duration_hint = ""
        if duration_s is not None and timer_ok:
            duration_hint = f"，{self._format_duration(duration_s)}后自动恢复"
        elif duration_s is not None:
            duration_hint = "，定时恢复设置失败，需手动 /resume"
        if symbol is None:
            cancel_msg = "所有挂单已撤销" if callback_ok else "挂单撤销失败，请手动检查"
            return f"已暂停全局执行{duration_hint}，{cancel_msg}\n注意：暂停期间不执行强平兜底，保护性止损仍在交易所端生效"
        short = symbol.split(":")[0]
        cancel_msg = "相关挂单已撤销" if callback_ok else "挂单撤销失败，请手动检查"
        return f"已暂停 {short}{duration_hint}，{cancel_msg}"

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
                # 取消所有定时恢复任务
                self._cancel_all_resume_tasks()
                logger.info("全局暂停已解除")
                return "已恢复全局执行"
            else:
                if symbol not in self._paused_symbols:
                    if self._global_paused:
                        return "当前为全局暂停，请使用 /resume 恢复全局"
                    return f"{symbol} 未处于暂停状态"
                self._paused_symbols.discard(symbol)
                self._symbol_paused_at.pop(symbol, None)
                # 取消该 symbol 的定时恢复任务
                self._cancel_resume_task(symbol)
                short = symbol.split(":")[0]
                logger.info(f"暂停已解除: {symbol}")
                return f"已恢复 {short} 执行"

    def get_status(self) -> Dict[str, Any]:
        """返回当前暂停状态摘要。"""
        return {
            "global_paused": self._global_paused,
            "global_paused_at": self._global_paused_at,
            "global_resume_at": self._auto_resume_at.get(None),
            "paused_symbols": dict(self._symbol_paused_at),
            "symbol_resume_at": {
                sym: self._auto_resume_at[sym]
                for sym in self._paused_symbols
                if sym in self._auto_resume_at
            },
        }

    def cancel_all_timers(self) -> None:
        """shutdown 时清理所有定时恢复任务。"""
        self._cancel_all_resume_tasks()

    # ---- 内部方法 ----

    def _schedule_resume(self, symbol: Optional[str], duration_s: float, now: datetime) -> None:
        """创建定时恢复任务，取消旧任务。"""
        self._cancel_resume_task(symbol)
        self._auto_resume_at[symbol] = now + timedelta(seconds=duration_s)
        task = asyncio.create_task(self._delayed_resume(symbol, duration_s))
        self._resume_tasks[symbol] = task

    def _try_schedule_resume(
        self, symbol: Optional[str], duration_s: float, now: datetime
    ) -> bool:
        """
        尝试创建定时恢复任务。

        失败时（非法值、事件循环已关闭等）降级为无定时恢复，不抛异常。

        Returns:
            True 表示定时器创建成功
        """
        try:
            self._schedule_resume(symbol, duration_s, now)
            return True
        except Exception as e:
            get_logger().warning(f"定时恢复任务创建失败，暂停仍生效但需手动恢复: {e}")
            self._auto_resume_at.pop(symbol, None)
            return False

    async def _delayed_resume(self, symbol: Optional[str], delay_s: float) -> None:
        """等待指定时长后自动恢复。"""
        logger = get_logger()
        try:
            await asyncio.sleep(delay_s)
            # 先从 _resume_tasks 中移除自身，避免 resume() 内部取消正在运行的自己
            self._resume_tasks.pop(symbol, None)
            self._auto_resume_at.pop(symbol, None)
            result = await self.resume(symbol)
            logger.info(f"定时恢复完成: {result}")
            if self._on_auto_resume:
                try:
                    await self._on_auto_resume(result)
                except Exception as e:
                    logger.error(f"定时恢复通知回调失败: {e}")
        except asyncio.CancelledError:
            pass

    def _cancel_resume_task(self, symbol: Optional[str]) -> None:
        """取消指定 key 的定时恢复任务。"""
        task = self._resume_tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
        self._auto_resume_at.pop(symbol, None)

    def _cancel_all_resume_tasks(self) -> None:
        """取消所有定时恢复任务。"""
        for task in self._resume_tasks.values():
            if not task.done():
                task.cancel()
        self._resume_tasks.clear()
        self._auto_resume_at.clear()

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """将秒数格式化为人可读的时长字符串。"""
        if seconds >= 3600:
            h = seconds / 3600
            return f"{h:g}h" if h == int(h) else f"{h:.1f}h"
        if seconds >= 60:
            m = seconds / 60
            return f"{m:g}m" if m == int(m) else f"{m:.1f}m"
        return f"{seconds:g}s" if seconds == int(seconds) else f"{seconds:.1f}s"
