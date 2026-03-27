# Input: config path, env vars, OS signals, account positions, Telegram Bot commands
# Output: application lifecycle, async tasks, runtime symbol orchestration, account-event position refresh, pause/resume control, reduce-only block verification wiring, and liq-distance risk log/mode coordination
# Pos: application entrypoint and orchestrator
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
vibe-quant: Binance U 本位永续 Hedge 模式 Reduce-Only 小单平仓执行器

入口模块

职责：
- 加载配置
- 初始化各模块
- 启动事件循环
- 协调各模块交互
- 处理优雅退出
"""

import asyncio
import math
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Optional, List, Sequence, Awaitable, Any, Coroutine

# 加载 .env 文件（必须在其他导入之前）
from dotenv import load_dotenv
load_dotenv()

from src.config.loader import ConfigLoader
from src.config.models import MergedSymbolConfig
from src.exchange.adapter import ExchangeAdapter
from src.ws.market import MarketWSClient
from src.ws.user_data import UserDataWSClient
from src.signal.engine import SignalEngine, PressureSignalConfig
from src.stats import MarketDataRecorder, PressureStatsCollector
from src.execution.engine import ExecutionEngine
from src.risk.manager import RiskManager
from src.risk.protective_stop import ProtectiveStopManager
from src.notify.telegram import TelegramNotifier
from src.notify.pause_manager import PauseManager
from src.notify.bot import TelegramBot
from src.models import (
    AccountUpdateEvent,
    MarketEvent,
    OrderUpdate,
    AlgoOrderUpdate,
    OrderIntent,
    OrderResult,
    OrderStatus,
    MarketState,
    Position,
    PositionUpdate,
    LeverageUpdate,
    PositionSide,
    SymbolRules,
    ExecutionState,
    ExecutionMode,
    ExitSignal,
    SignalReason,
    StrategyMode,
    SignalExecutionPreference,
    TimeInForce,
    ReduceOnlyBlockInfo,
    RiskFlag,
)
from src.utils.logger import (
    setup_logger,
    get_logger,
    log_startup,
    log_shutdown,
    log_position_update,
    log_event,
    log_error,
)
from src.utils.helpers import current_time_ms, format_decimal, format_decimal_fixed


CLIENT_ORDER_PREFIX = "vq"
PROTECTIVE_STOP_PREFIX = f"{CLIENT_ORDER_PREFIX}-ps-"

_STOP_ORDER_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"}

@dataclass
class ExternalTakeoverState:
    active: bool = False
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    last_verify_ms: int = 0
    last_verify_present: Optional[bool] = None
    pending_release: bool = False


# pressure 信号 reason 显式集合（用于统计收集器的 reason 匹配）
_PRESSURE_ACTIVE_REASONS: frozenset[str] = frozenset({
    SignalReason.LONG_BID_PRESSURE_ACTIVE.value,
    SignalReason.SHORT_ASK_PRESSURE_ACTIVE.value,
})
_PRESSURE_PASSIVE_REASONS: frozenset[str] = frozenset({
    SignalReason.LONG_ASK_PRESSURE_PASSIVE.value,
    SignalReason.SHORT_BID_PRESSURE_PASSIVE.value,
})
_PRESSURE_REASONS: frozenset[str] = _PRESSURE_ACTIVE_REASONS | _PRESSURE_PASSIVE_REASONS
_PressureTriggerSignature = tuple[str, Decimal, Decimal, Decimal, Optional[Decimal]]


class Application:
    """应用主类"""

    def __init__(self, config_path: Path):
        """
        初始化应用

        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self.config_loader: Optional[ConfigLoader] = None
        self.exchange: Optional[ExchangeAdapter] = None
        self.market_ws: Optional[MarketWSClient] = None
        self.user_data_ws: Optional[UserDataWSClient] = None
        self.signal_engine: Optional[SignalEngine] = None
        self.execution_engines: Dict[str, ExecutionEngine] = {}  # per symbol
        self.risk_manager: Optional[RiskManager] = None
        self.protective_stop_manager: Optional[ProtectiveStopManager] = None
        self.telegram_notifier: Optional[TelegramNotifier] = None
        self._telegram_tasks: set[asyncio.Task[Any]] = set()
        self._side_tasks: set[asyncio.Task[Any]] = set()
        self._market_recorder: Optional[MarketDataRecorder] = None

        # Telegram Bot 命令控制
        self.pause_manager: PauseManager = PauseManager()
        self.telegram_bot: Optional[TelegramBot] = None
        self._telegram_bot_task: Optional[asyncio.Task[None]] = None
        self._protective_stop_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._protective_stop_task_reasons: Dict[str, str] = {}
        self._protective_stop_task_executing: Dict[str, asyncio.Event] = {}  # 标记任务是否已进入执行阶段
        self._protective_stop_pending_reason: Dict[str, str] = {}  # 脏标记: 执行中收到新请求时暂存

        self._running = False
        self._started_at: Optional[datetime] = None
        self._shutdown_event = asyncio.Event()
        self._positions: Dict[str, Dict[PositionSide, Position]] = {}  # symbol -> side -> Position
        self._symbol_leverage: Dict[str, int] = {}  # symbol -> leverage
        self._rules: Dict[str, SymbolRules] = {}  # symbol -> rules
        self._symbol_configs: Dict[str, MergedSymbolConfig] = {}  # symbol -> config
        self._active_symbols: set[str] = set()  # 运行时管理的 symbols

        # 主循环任务
        self._main_loop_task: Optional[asyncio.Task[None]] = None
        self._timeout_check_task: Optional[asyncio.Task[None]] = None
        self._fill_rate_log_task: Optional[asyncio.Task[None]] = None
        self._pressure_stats_task: Optional[asyncio.Task[None]] = None
        self._position_refresh_loop_task: Optional[asyncio.Task[None]] = None
        self._market_ws_task: Optional[asyncio.Task[None]] = None
        self._user_data_ws_task: Optional[asyncio.Task[None]] = None
        self._calibration_task: Optional[asyncio.Task[None]] = None
        self._calibration_lock = asyncio.Lock()
        self._calibrating = False
        self._positions_refresh_lock = asyncio.Lock()
        self._positions_refresh_task: Optional[asyncio.Task[Any]] = None
        self._positions_refresh_dirty = False
        self._positions_refresh_pending_reason: Optional[str] = None
        self._margin_refresh_debounce_s: float = 2.0
        self._position_refresh_interval_s: int = 300

        self._shutdown_started = False

        # 订单归属标记：用于"本次运行"隔离撤单范围（避免误撤手动/其他实例订单）
        self._run_id: Optional[str] = None
        self._client_order_id_prefix: Optional[str] = None

        # 仓位更新同步（用于 Telegram 仓位 before->after 以及开仓告警）
        self._positions_ready = False
        self._position_update_events: dict[tuple[str, PositionSide], asyncio.Event] = {}
        self._position_revision: dict[tuple[str, PositionSide], int] = {}
        self._position_last_change: dict[tuple[str, PositionSide], tuple[Decimal, Decimal]] = {}
        self._no_position_logged: set[tuple[str, PositionSide]] = set()
        self._fill_rate_last_log_ms: dict[tuple[str, PositionSide], int] = {}
        self._pressure_stats: Optional[PressureStatsCollector] = None
        self._pressure_fill_recorded_orders: set[tuple[str, str]] = set()
        self._pressure_trigger_signatures: dict[tuple[str, PositionSide], _PressureTriggerSignature] = {}
        self._panic_last_tier: dict[tuple[str, PositionSide], Decimal] = {}
        self._external_takeover: dict[tuple[str, PositionSide], ExternalTakeoverState] = {}
        self._symbol_init_lock = asyncio.Lock()

    def _get_external_takeover_cfg_ms(self, symbol: str) -> tuple[bool, int, int]:
        cfg = self._symbol_configs.get(symbol)
        if not cfg:
            return False, 0, 0
        enabled = bool(cfg.protective_stop_external_takeover_enabled)
        verify_ms = int(cfg.protective_stop_external_takeover_rest_verify_interval_s) * 1000
        max_hold_ms = int(cfg.protective_stop_external_takeover_max_hold_s) * 1000
        return enabled, verify_ms, max_hold_ms

    def _external_takeover_mark_seen(self, symbol: str, position_side: PositionSide, *, now_ms: int) -> None:
        self._external_takeover_set(symbol, position_side, now_ms=now_ms, source="unknown")

    def _external_takeover_mark_terminal(self, symbol: str, position_side: PositionSide, *, now_ms: int) -> None:
        self._external_takeover_request_release(symbol, position_side, now_ms=now_ms, source="unknown")

    def _external_takeover_request_release(
        self,
        symbol: str,
        position_side: PositionSide,
        *,
        now_ms: int,
        source: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """
        请求释放外部接管（不立即释放）。

        说明：同侧可能同时存在多个外部 stop/tp；WS 收到某一张终态不代表"外部接管结束"。
        此处只把状态标记为 pending，并触发一次 verify（raw openOrders）后再决定是否 release。
        """
        enabled, _verify_ms, _max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
        if not enabled:
            return
        key = (symbol, position_side)
        st = self._external_takeover.get(key)
        if st is None:
            st = ExternalTakeoverState()
            self._external_takeover[key] = st
        if not st.active:
            return
        st.pending_release = True
        st.last_seen_ms = now_ms
        # 不打印日志：接管状态未变化（仍 active）
        # 触发 verify：由 _sync_protective_stop 在 present=False 时执行真正的 release
        self._schedule_protective_stop_sync(symbol, reason="external_takeover_verify")

    def _external_takeover_set(
        self,
        symbol: str,
        position_side: PositionSide,
        *,
        now_ms: int,
        source: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_type: Optional[str] = None,
    ) -> None:
        enabled, _verify_ms, _max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
        if not enabled:
            return
        key = (symbol, position_side)
        st = self._external_takeover.get(key)
        if st is None:
            st = ExternalTakeoverState()
            self._external_takeover[key] = st
        was_active = st.active
        if not was_active:
            st.active = True
            st.first_seen_ms = now_ms
            st.last_verify_present = None
            st.pending_release = False
            log_event(
                "risk",
                symbol=symbol,
                side=position_side.value,
                reason="external_takeover_set",
                source=source,
                order_id=order_id,
                client_order_id=client_order_id,
                order_type=order_type,
                level="debug",
            )
        st.last_seen_ms = now_ms

    def _external_takeover_release(
        self,
        symbol: str,
        position_side: PositionSide,
        *,
        now_ms: int,
        source: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_type: Optional[str] = None,
        status: Optional[str] = None,
        reason: str = "external_takeover_release",
    ) -> bool:
        enabled, _verify_ms, _max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
        if not enabled:
            return False
        key = (symbol, position_side)
        st = self._external_takeover.get(key)
        if st is None:
            st = ExternalTakeoverState()
            self._external_takeover[key] = st
        released = False
        if st.active:
            st.active = False
            st.last_verify_present = None
            st.pending_release = False
            log_event(
                "risk",
                symbol=symbol,
                side=position_side.value,
                reason=reason,
                source=source,
                status=status,
                order_id=order_id,
                client_order_id=client_order_id,
                order_type=order_type,
            )
            released = True
        st.last_seen_ms = now_ms
        return released

    def _external_takeover_is_active(self, symbol: str, position_side: PositionSide, *, now_ms: int) -> bool:
        enabled, verify_ms, max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
        if not enabled:
            return False
        st = self._external_takeover.get((symbol, position_side))
        if not st or not st.active:
            return False
        # 兜底：若锁存持续过久且很久没看到 WS 心跳，则依赖 REST 校验释放（由 _check_all_timeouts 触发 sync）
        _ = verify_ms, max_hold_ms, now_ms
        return True

    def _external_takeover_should_verify(self, symbol: str, position_side: PositionSide, *, now_ms: int) -> bool:
        enabled, verify_ms, max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
        if not enabled:
            return False
        st = self._external_takeover.get((symbol, position_side))
        if not st or not st.active:
            return False
        if st.last_verify_ms == 0 or now_ms - st.last_verify_ms >= verify_ms:
            return True
        # 锁存过久：额外确保会触发校验
        if st.first_seen_ms and now_ms - st.first_seen_ms >= max_hold_ms and now_ms - st.last_verify_ms >= min(verify_ms, 5000):
            return True
        return False

    def _external_takeover_note_verified(self, symbol: str, position_side: PositionSide, *, now_ms: int) -> None:
        st = self._external_takeover.get((symbol, position_side))
        if st:
            st.last_verify_ms = now_ms

    def _init_run_identity(self) -> None:
        """初始化本次运行的标识（用于 newClientOrderId 前缀）。"""
        run_id = uuid.uuid4().hex[:10]
        prefix = f"{CLIENT_ORDER_PREFIX}-{run_id}-"
        if len(prefix) >= 36:
            raise ValueError(f"newClientOrderId 前缀过长: len(prefix)={len(prefix)} prefix={prefix}")
        self._run_id = run_id
        self._client_order_id_prefix = prefix
        get_logger().info(f"运行标识: run_id={run_id} client_order_id_prefix={prefix}")

    def _next_client_order_id(self) -> str:
        """
        生成 clientOrderId（Binance: newClientOrderId）

        约定：所有本程序订单都使用本次运行前缀 `<client_order_prefix>-{run_id}-`
        （其中 `client_order_prefix` 为常量 `CLIENT_ORDER_PREFIX`），用于退出时"只清理本次运行挂单"。
        """
        prefix = self._client_order_id_prefix
        if not prefix:
            raise RuntimeError("运行标识未初始化，无法生成 newClientOrderId")
        suffix_len = 36 - len(prefix)
        if suffix_len <= 0:
            raise ValueError(f"newClientOrderId 前缀过长: len(prefix)={len(prefix)} prefix={prefix}")
        return f"{prefix}{uuid.uuid4().hex[:suffix_len]}"

    def _on_background_task_done(self, task: asyncio.Task[Any], name: str) -> None:
        """后台任务退出回调：记录异常并触发关闭。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_error(f"后台任务状态异常: {name} - {e}")
            self.request_shutdown()
            return

        if exc:
            log_error(f"后台任务异常退出: {name} - {exc}")
            self.request_shutdown()

    def _on_telegram_task_done(self, task: asyncio.Task[Any], name: str) -> None:
        """Telegram 任务退出回调：只记录异常，不影响主程序。"""
        self._telegram_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_error(f"Telegram 任务状态异常: {name} - {e}")
            return

        if exc:
            log_error(f"Telegram 任务异常: {name} - {exc}")

    def _schedule_telegram(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """异步调度 Telegram 通知（不得阻塞主链路）。"""
        if not self._running:
            return
        if not self.telegram_notifier or not self.telegram_notifier.enabled:
            return
        task = asyncio.create_task(coro)
        self._telegram_tasks.add(task)
        task.add_done_callback(lambda t, n=name: self._on_telegram_task_done(t, n))

    def _on_protective_stop_task_done(self, task: asyncio.Task[Any], symbol: str, name: str) -> None:
        """保护止损同步任务回调：只记录异常，不影响主程序。"""
        # 只清理自己（避免误清后续已替换的任务）
        if self._protective_stop_tasks.get(symbol) is task:
            self._protective_stop_tasks.pop(symbol, None)
            self._protective_stop_task_reasons.pop(symbol, None)
            self._protective_stop_task_executing.pop(symbol, None)
            self._protective_stop_pending_reason.pop(symbol, None)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_error(f"保护止损任务状态异常: {name} - {e}", symbol=symbol)
            return

        if exc:
            log_error(f"保护止损任务异常: {name} - {exc}", symbol=symbol)

    def _on_positions_refresh_task_done(self, task: asyncio.Task[Any], name: str) -> None:
        """全量仓位刷新任务回调。"""
        if self._positions_refresh_task is task:
            self._positions_refresh_task = None
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_error(f"仓位刷新任务状态异常: {name} - {e}")
            return
        if exc:
            log_error(f"仓位刷新任务异常: {name} - {exc}")

    def _schedule_positions_refresh(self, *, reason: str, debounce_s: float) -> None:
        """
        异步调度全量仓位刷新（A：事件触发；B：低频兜底）。

        语义：
        - 已有刷新任务在跑时只打脏标记，任务结束后再跑一轮；
        - 多次触发合并，避免无意义重复 REST。
        """
        if not self._running or not self.exchange:
            return

        existing = self._positions_refresh_task
        if existing and not existing.done():
            self._positions_refresh_dirty = True
            self._positions_refresh_pending_reason = reason
            return

        self._positions_refresh_dirty = False
        self._positions_refresh_pending_reason = None

        async def _runner(initial_reason: str, initial_debounce_s: float) -> None:
            current_reason = initial_reason
            current_debounce = max(initial_debounce_s, 0.0)
            while self._running:
                if current_debounce > 0:
                    await asyncio.sleep(current_debounce)
                await self._refresh_all_positions_and_sync(reason=current_reason)
                if not self._positions_refresh_dirty:
                    break
                current_reason = self._positions_refresh_pending_reason or "coalesced"
                self._positions_refresh_dirty = False
                self._positions_refresh_pending_reason = None
                current_debounce = 0.0

        task = asyncio.create_task(_runner(reason, debounce_s))
        self._positions_refresh_task = task
        task.add_done_callback(lambda t, n=f"positions_refresh:{reason}": self._on_positions_refresh_task_done(t, n))

    async def _refresh_all_positions_and_sync(self, *, reason: str) -> None:
        """执行一次全量仓位刷新，并立即同步保护止损。"""
        if not self._running or not self.exchange:
            return
        if self._calibrating:
            return

        async with self._positions_refresh_lock:
            if not self._running or self._calibrating:
                return
            before_symbols = set(self._active_symbols)
            try:
                await self._fetch_all_positions()
                if set(self._active_symbols) != before_symbols:
                    await self._rebuild_market_ws(reason=f"positions_refresh:{reason}")
                await self._sync_protective_stops_all(reason=f"positions_refresh:{reason}")
                log_event(
                    "position_refresh",
                    level="debug",
                    reason=reason,
                    active_symbols=len(self._active_symbols),
                )
            except Exception as e:
                log_error(f"全量仓位刷新失败: {e}", reason=reason)

    def _schedule_protective_stop_sync(self, symbol: str, reason: str) -> None:
        """异步调度保护止损同步（合并短时间内的多次触发）。

        两阶段策略:
        - debounce sleep 阶段: 新调度安全取消旧任务(合并触发)
        - REST 执行阶段: 不取消, 设置脏标记让任务完成后自行 re-run
        """
        if not self._running:
            return
        if not self.exchange or not self.protective_stop_manager:
            return

        prev = self._protective_stop_tasks.get(symbol)
        prev_reason = self._protective_stop_task_reasons.get(symbol)
        prev_executing = self._protective_stop_task_executing.get(symbol)
        # verify 任务只需要"至少跑一次"，若已经有 verify 在跑/排队/pending 就不重复调度（避免刷屏）
        if reason == "external_takeover_verify":
            if prev and not prev.done() and prev_reason == "external_takeover_verify":
                return
            if self._protective_stop_pending_reason.get(symbol) == "external_takeover_verify":
                return

        prev_is_executing = prev_executing is not None and prev_executing.is_set()

        if prev and not prev.done():
            if prev_is_executing:
                # REST 执行中: 不取消, 设脏标记让任务完成后自行 re-run
                self._protective_stop_pending_reason[symbol] = reason
                return
            else:
                # debounce sleep 中: 安全取消
                prev.cancel()

        debounce_s = self._protective_stop_debounce_s(reason)
        past_debounce = asyncio.Event()

        async def _runner() -> None:
            current_reason = reason
            current_debounce = debounce_s
            while True:
                try:
                    # debounce: 合并短时间内的多次触发（按 reason 分级）
                    if current_debounce > 0:
                        await asyncio.sleep(current_debounce)
                    past_debounce.set()
                    await self._sync_protective_stop(symbol=symbol, reason=current_reason)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    log_error(f"保护止损同步失败: {e}", symbol=symbol)
                # sync 完成后检查脏标记（past_debounce 仍 set, 新请求会继续走脏标记路径）
                pending = self._protective_stop_pending_reason.pop(symbol, None)
                if pending is None or not self._running:
                    break
                current_reason = pending
                current_debounce = self._protective_stop_debounce_s(pending)
                self._protective_stop_task_reasons[symbol] = pending
                # 进入下一轮 debounce 前清除执行标记, 使新调度可以取消 debounce sleep
                past_debounce.clear()

        task = asyncio.create_task(_runner())
        self._protective_stop_tasks[symbol] = task
        self._protective_stop_task_reasons[symbol] = reason
        self._protective_stop_task_executing[symbol] = past_debounce
        task.add_done_callback(lambda t, s=symbol, n=f"protective_stop_sync:{symbol}": self._on_protective_stop_task_done(t, s, n))

    @staticmethod
    def _protective_stop_debounce_s(reason: str) -> float:
        """
        保护止损同步的分级 debounce。

        - position_update: 平仓频繁，延迟高一点降 REST 压力
        - startup/calibration: 立即同步，尽快确保保护单存在
        - 其他（our_algo/order_update/external_* 等）：保持较快响应
        """
        if reason.startswith("position_update"):
            return 1.0
        if reason.startswith("startup") or reason.startswith("calibration"):
            return 0.0
        return 0.2

    async def _sync_protective_stop(self, *, symbol: str, reason: str) -> None:
        """执行保护止损同步（会访问交易所 openOrders）。"""
        if not self.exchange or not self.protective_stop_manager:
            return
        cfg = self._symbol_configs.get(symbol)
        rules = self._rules.get(symbol)
        if not cfg or not rules:
            return

        needs_resync = False
        try:
            now_ms = current_time_ms()
            external_latch = {
                PositionSide.LONG: self._external_takeover_is_active(symbol, PositionSide.LONG, now_ms=now_ms),
                PositionSide.SHORT: self._external_takeover_is_active(symbol, PositionSide.SHORT, now_ms=now_ms),
            }
            rest_external = await self.protective_stop_manager.sync_symbol(
                symbol=symbol,
                rules=rules,
                positions=self._positions.get(symbol, {}),
                enabled=cfg.protective_stop_enabled,
                dist_to_liq=cfg.protective_stop_dist_to_liq,
                external_stop_latch_by_side=external_latch,
                sync_reason=reason,
            )
            enabled, verify_ms, _max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
            if enabled:
                for side in (PositionSide.LONG, PositionSide.SHORT):
                    self._external_takeover_note_verified(symbol, side, now_ms=now_ms)
                    present = rest_external.get(side, False)
                    if present:
                        self._external_takeover_set(symbol, side, now_ms=now_ms, source="rest")
                        st_present = self._external_takeover.get((symbol, side))
                        if st_present:
                            st_present.pending_release = False
                    else:
                        st = self._external_takeover.get((symbol, side))
                        if st and st.active and st.pending_release:
                            if self._external_takeover_release(
                                symbol,
                                side,
                                now_ms=now_ms,
                                source="rest_verify",
                                reason="external_takeover_release",
                            ):
                                needs_resync = True
                        elif st and st.active and (now_ms - st.last_seen_ms) >= verify_ms:
                            if self._external_takeover_release(
                                symbol,
                                side,
                                now_ms=now_ms,
                                source="rest",
                                reason="external_takeover_release_by_rest",
                            ):
                                needs_resync = True

                    # verify 事件：只在 verify sync 场景打印（避免刷屏）
                    st2 = self._external_takeover.get((symbol, side))
                    if reason.startswith("external_takeover_verify") and st2 and st2.active:
                        if st2.last_verify_present is None or st2.last_verify_present != present:
                            st2.last_verify_present = present
                            log_event(
                                "risk",
                                symbol=symbol,
                                side=side.value,
                                reason="external_takeover_verify",
                                external_present=present,
                                level="debug",
                            )
        except Exception as e:
            log_error(f"保护止损同步异常: {e}", symbol=symbol, reason=reason)
            return

        if needs_resync:
            await self._sync_protective_stop(symbol=symbol, reason="external_takeover_release")

    async def _sync_protective_stops_all(self, *, reason: str) -> None:
        for symbol in list(self._active_symbols):
            await self._sync_protective_stop(symbol=symbol, reason=reason)

    def _record_pressure_fill_once(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        order_id: str,
        reason: str,
        ts_ms: Optional[int] = None,
    ) -> None:
        """按 order_id 去重记录 pressure 订单的首次成交。"""
        if not self._pressure_stats or reason not in _PRESSURE_REASONS or not order_id:
            return

        key = (symbol, order_id)
        if key in self._pressure_fill_recorded_orders:
            return
        self._pressure_fill_recorded_orders.add(key)

        self._pressure_stats.record_outcome(
            symbol=symbol,
            side=position_side.value,
            is_active=(reason in _PRESSURE_ACTIVE_REASONS),
            is_filled=True,
            ts_ms=ts_ms if ts_ms is not None else current_time_ms(),
        )
        if self.signal_engine and reason in _PRESSURE_ACTIVE_REASONS:
            self.signal_engine.record_pressure_active_fill(
                symbol,
                position_side,
                ts_ms=ts_ms if ts_ms is not None else current_time_ms(),
            )

    def _clear_pressure_trigger_signature(self, symbol: str, position_side: PositionSide) -> None:
        self._pressure_trigger_signatures.pop((symbol, position_side), None)

    def _record_pressure_trigger_edge(self, signal: ExitSignal, *, ts_ms: int) -> None:
        """仅在 pressure signal 快照发生变化时记录 trigger。"""
        if not self._pressure_stats or signal.strategy_mode != StrategyMode.ORDERBOOK_PRESSURE:
            return

        key = (signal.symbol, signal.position_side)
        signature: _PressureTriggerSignature = (
            signal.reason.value,
            signal.best_bid,
            signal.best_ask,
            signal.last_trade_price,
            signal.price_override,
        )
        if self._pressure_trigger_signatures.get(key) == signature:
            return

        self._pressure_trigger_signatures[key] = signature
        self._pressure_stats.record_trigger(
            symbol=signal.symbol,
            side=signal.position_side.value,
            is_active=(signal.execution_preference == SignalExecutionPreference.AGGRESSIVE),
            mid_price=(signal.best_bid + signal.best_ask) / 2,
            ts_ms=ts_ms,
        )

    def _on_engine_fill(
        self,
        symbol: str,
        position_side: PositionSide,
        order_id: str,
        mode: ExecutionMode,
        filled_qty: Decimal,
        avg_price: Decimal,
        reason: str,
        role: Optional[str],
        pnl: Optional[Decimal],
        fee: Optional[Decimal],
        fee_asset: Optional[str],
    ) -> None:
        """ExecutionEngine 完全成交回调：用于去重记录 stats 首次成交并触发 Telegram。"""
        self._record_pressure_fill_once(
            symbol=symbol,
            position_side=position_side,
            order_id=order_id,
            reason=reason,
        )

        if not self.config_loader or not self.telegram_notifier:
            return
        telegram_cfg = self.config_loader.config.global_.telegram
        if not telegram_cfg.enabled or not telegram_cfg.events.on_fill:
            return

        self._schedule_telegram(
            self._notify_fill_telegram(
                symbol=symbol,
                position_side=position_side,
                mode=mode,
                filled_qty=filled_qty,
                avg_price=avg_price,
                reason=reason,
                role=role,
                pnl=pnl,
                fee=fee,
                fee_asset=fee_asset,
            ),
            name=f"fill:{symbol}:{position_side.value}",
        )

    def _format_realized_pnl(self, pnl: Optional[Decimal]) -> Optional[str]:
        if pnl is None:
            return None
        formatted = format_decimal_fixed(pnl, precision=4)
        if formatted is None:
            return None
        return f"{formatted} USDT"

    def _format_fee(self, fee: Optional[Decimal], fee_asset: Optional[str]) -> Optional[str]:
        if fee is None or not fee_asset:
            return None
        formatted = format_decimal_fixed(fee, precision=4)
        if formatted is None:
            return None
        return f"{formatted} {fee_asset}"

    async def _wait_for_position_change(
        self,
        symbol: str,
        position_side: PositionSide,
        start_revision: int,
        timeout_s: float,
    ) -> bool:
        """等待指定 symbol+side 的下一次仓位变更（用于填充 Telegram 的 before->after）。"""
        key = (symbol, position_side)
        if self._position_revision.get(key, 0) > start_revision:
            return True

        event = self._position_update_events.setdefault(key, asyncio.Event())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while self._position_revision.get(key, 0) <= start_revision:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                if self._position_revision.get(key, 0) > start_revision:
                    event.clear()
                    return True
                return False
            else:
                event.clear()

        return True

    async def _notify_fill_telegram(
        self,
        symbol: str,
        position_side: PositionSide,
        mode: ExecutionMode,
        filled_qty: Decimal,
        avg_price: Decimal,
        reason: str,
        role: Optional[str],
        pnl: Optional[Decimal],
        fee: Optional[Decimal],
        fee_asset: Optional[str],
    ) -> None:
        """带仓位 before->after 的成交通知（尽量等一次 ACCOUNT_UPDATE）。"""
        if not self.config_loader or not self.telegram_notifier:
            return

        telegram_cfg = self.config_loader.config.global_.telegram
        if not telegram_cfg.enabled or not telegram_cfg.events.on_fill:
            return

        key = (symbol, position_side)
        start_rev = self._position_revision.get(key, 0)

        before_amt: Optional[Decimal] = None
        after_amt: Optional[Decimal] = None

        got_change = await self._wait_for_position_change(
            symbol=symbol,
            position_side=position_side,
            start_revision=start_rev,
            timeout_s=1.5,
        )
        if got_change:
            change = self._position_last_change.get(key)
            if change:
                before_amt, after_amt = change
                if abs(after_amt) > abs(before_amt):
                    before_amt = None
                    after_amt = None

        if before_amt is None or after_amt is None:
            pos = self._positions.get(symbol, {}).get(position_side)
            cached_amt = pos.position_amt if pos else Decimal("0")
            delta = -filled_qty if position_side == PositionSide.LONG else filled_qty

            before_amt = cached_amt
            after_amt = cached_amt + delta
            if position_side == PositionSide.LONG and after_amt < Decimal("0"):
                after_amt = Decimal("0")
            if position_side == PositionSide.SHORT and after_amt > Decimal("0"):
                after_amt = Decimal("0")

        await self.telegram_notifier.notify_fill(
            symbol=symbol,
            side=position_side.value,
            mode=mode.value,
            qty=str(filled_qty),
            avg_price=str(avg_price),
            reason=reason,
            position_before=str(abs(before_amt)),
            position_after=str(abs(after_amt)),
            role=role,
            pnl=self._format_realized_pnl(pnl),
            fee=self._format_fee(fee, fee_asset),
        )

    async def _gather_with_timeout(
        self,
        awaitables: Sequence[Awaitable[Any]],
        timeout_s: float,
        name: str,
    ) -> None:
        """带超时等待一组协程，避免 shutdown 卡死。"""
        if not awaitables:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*awaitables, return_exceptions=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            get_logger().warning(f"{name} 超时({timeout_s:.1f}s)，跳过等待")

    async def _cancel_run_prefix_orders_for_side(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        reason: str,
    ) -> None:
        """
        撤销"本次运行"在该 symbol+side 的遗留挂单。

        目的：当仓位被外部力量（如保护止损）归零时，避免挂单继续触发导致反向开仓。
        """
        exchange = self.exchange
        prefix = self._client_order_id_prefix
        if not exchange or not prefix:
            return

        try:
            orders = await exchange.fetch_open_orders(symbol)
        except Exception as e:
            log_error(f"获取挂单失败: {e}", symbol=symbol)
            return

        cancelled = 0
        for order in orders:
            if not isinstance(order, dict):
                continue
            client_order_id = order.get("clientOrderId")
            if not client_order_id:
                info = order.get("info")
                if isinstance(info, dict):
                    client_order_id = info.get("clientOrderId")
            if not client_order_id or not str(client_order_id).startswith(prefix):
                continue

            info = order.get("info")
            ps = None
            if isinstance(info, dict):
                ps = info.get("positionSide")
            if ps and str(ps).upper() != position_side.value:
                continue

            order_id = order.get("id")
            if not order_id:
                continue
            try:
                await exchange.cancel_any_order(str(symbol), str(order_id))
                cancelled += 1
            except Exception as e:
                log_error(f"撤销挂单失败: {e}", symbol=str(symbol), order_id=str(order_id))

        if cancelled > 0:
            log_event(
                "order_cleanup",
                event_cn="挂单清理",
                symbol=symbol,
                side=position_side.value,
                reason=reason,
                cancelled=cancelled,
            )

    async def initialize(self) -> None:
        """初始化所有模块"""
        logger = get_logger()

        # 1. 加载配置
        logger.info("加载配置...")
        self.config_loader = ConfigLoader(self.config_path)
        self.config_loader.load()  # 加载配置文件
        configured_symbols = self.config_loader.get_symbols()

        # 设置日志
        # 支持通过环境变量指定日志目录（便于 systemd/Docker 持久化日志）
        log_dir = Path(os.environ.get("VQ_LOG_DIR", "logs"))
        setup_logger(log_dir, level="INFO", file_level="DEBUG", console=True)
        log_startup(configured_symbols)

        logger.info(f"配置加载完成，symbols(覆盖): {configured_symbols}")

        # 2. 初始化交易所适配器
        logger.info("初始化交易所适配器...")
        global_config = self.config_loader.config.global_
        pstop_global_cfg = global_config.risk.protective_stop
        self._margin_refresh_debounce_s = float(pstop_global_cfg.margin_refresh_debounce_s)
        self._position_refresh_interval_s = int(pstop_global_cfg.position_refresh_interval_s)

        # Telegram 通知（可选）
        telegram_cfg = global_config.telegram
        if telegram_cfg.bot.enabled and not telegram_cfg.enabled:
            raise ValueError(
                "telegram.bot.enabled=true 需要 telegram.enabled=true"
            )
        if telegram_cfg.enabled:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if not token:
                raise ValueError("环境变量 TELEGRAM_BOT_TOKEN 未设置（telegram.enabled=true）")
            if not chat_id:
                raise ValueError("环境变量 TELEGRAM_CHAT_ID 未设置（telegram.enabled=true）")

            self.telegram_notifier = TelegramNotifier(
                token=token,
                chat_id=chat_id,
                enabled=telegram_cfg.enabled,
                max_retries=3,
                proxy=global_config.proxy,
                timeout_s=10.0,
            )
            logger.info(
                f"Telegram 通知配置: enabled={self.telegram_notifier.enabled} "
                f"on_fill={telegram_cfg.events.on_fill} "
                f"on_reconnect={telegram_cfg.events.on_reconnect} "
                f"on_risk_trigger={telegram_cfg.events.on_risk_trigger}"
            )

            # Telegram Bot 命令接收（可选）
            bot_cfg = telegram_cfg.bot
            if bot_cfg.enabled:
                allowed_ids: set[str] = set(bot_cfg.allowed_chat_ids)
                if not allowed_ids:
                    allowed_ids = {chat_id}
                self.telegram_bot = TelegramBot(
                    token=token,
                    allowed_chat_ids=allowed_ids,
                    polling_timeout_s=bot_cfg.polling_timeout_s,
                    proxy=global_config.proxy,
                )
                self._register_bot_commands()
                logger.info(
                    f"Telegram Bot 命令配置: enabled=true "
                    f"polling_timeout={bot_cfg.polling_timeout_s}s "
                    f"allowed_chat_ids={sorted(allowed_ids)}"
                )

        # 初始化 PauseManager（撤单回调 + 定时恢复通知回调）
        self.pause_manager = PauseManager(
            on_pause_callback=self._on_pause_triggered,
            on_auto_resume_callback=self._on_auto_resume,
        )

        # 风控与全局限速（账户级）
        self.risk_manager = RiskManager(
            liq_distance_threshold=global_config.risk.liq_distance_threshold,
            stale_data_ms=global_config.ws.stale_data_ms,
            max_orders_per_sec=global_config.rate_limit.max_orders_per_sec,
            max_cancels_per_sec=global_config.rate_limit.max_cancels_per_sec,
        )

        # 初始化本次运行标识：用于隔离撤单范围（避免误撤手动/其他实例订单）
        self._init_run_identity()

        self.exchange = ExchangeAdapter(
            api_key=self.config_loader.api_key,
            api_secret=self.config_loader.api_secret,
            testnet=global_config.testnet,
            proxy=global_config.proxy,
        )
        await self.exchange.initialize()

        # 仓位保护性止损（交易所端条件单）
        self.protective_stop_manager = ProtectiveStopManager(
            self.exchange,
            client_order_id_prefix=PROTECTIVE_STOP_PREFIX,
            risk_levels=global_config.risk.levels,
            allow_loosen_on_liq_improve=pstop_global_cfg.allow_loosen_on_liq_improve,
            liq_improve_threshold=pstop_global_cfg.liq_improve_threshold,
            loosen_cooldown_s=pstop_global_cfg.loosen_cooldown_s,
        )

        # 加载交易规则（全量，symbol 运行时按需初始化）
        await self.exchange.load_markets()

        # 3. 初始化信号引擎（symbol 运行时按需配置）
        logger.info("初始化信号引擎...")
        self.signal_engine = SignalEngine(
            min_signal_interval_ms=global_config.execution.min_signal_interval_ms,
        )
        self._pressure_stats = PressureStatsCollector(
            regime_window_ms=global_config.stats.pressure_regime_window_ms,
            regime_samples=global_config.stats.pressure_regime_samples,
        )
        self._market_recorder = MarketDataRecorder(log_dir=log_dir)
        await self._market_recorder.start()

        # 4. 初始化 WebSocket 客户端（User Data 始终连接，Market WS 运行时按需创建）
        logger.info("初始化 WebSocket 客户端...")
        ws_config = global_config.ws

        self.user_data_ws = UserDataWSClient(
            api_key=self.config_loader.api_key,
            api_secret=self.config_loader.api_secret,
            on_order_update=self._on_order_update,
            on_algo_order_update=self._on_algo_order_update,
            on_position_update=self._on_position_update,
            on_account_update_event=self._on_account_update_event,
            on_leverage_update=self._on_leverage_update,
            on_reconnect=self._on_ws_reconnect,
            testnet=global_config.testnet,
            proxy=global_config.proxy,
            initial_delay_ms=ws_config.reconnect.initial_delay_ms,
            max_delay_ms=ws_config.reconnect.max_delay_ms,
            multiplier=ws_config.reconnect.multiplier,
        )

        logger.info("初始化完成")

    async def _place_order(self, intent: OrderIntent) -> OrderResult:
        """下单回调"""
        if not self.exchange:
            return OrderResult(success=False, error_message="Exchange not initialized")

        now_ms = current_time_ms()
        if not intent.is_risk and self.risk_manager and not self.risk_manager.can_place_order(current_ms=now_ms):
            log_event(
                "rate_limit",
                symbol=intent.symbol,
                reason="place_order",
                max_orders_per_sec=self.risk_manager.max_orders_per_sec,
            )
            return OrderResult(success=False, status=OrderStatus.REJECTED, error_message="rate_limited: place_order")

        if not intent.client_order_id:
            intent.client_order_id = self._next_client_order_id()

        # 确保满足 min_notional（补量）；reduce-only 不增量，仅记录并放行
        if intent.price and intent.price > Decimal("0"):
            if intent.reduce_only:
                rules = self._rules.get(intent.symbol)
                if rules:
                    notional = intent.qty * intent.price
                    if notional < rules.min_notional:
                        log_event(
                            "min_notional_allow_reduce_only",
                            level="debug",
                            symbol=intent.symbol,
                            qty=intent.qty,
                            price=intent.price,
                            notional=notional,
                            min_notional=rules.min_notional,
                        )
            else:
                adjusted_qty = self.exchange.ensure_min_notional(intent.symbol, intent.qty, intent.price)
                if adjusted_qty != intent.qty:
                    log_event(
                        "min_notional_adjust",
                        level="debug",
                        symbol=intent.symbol,
                        original_qty=intent.qty,
                        adjusted_qty=adjusted_qty,
                        price=intent.price,
                    )
                    intent.qty = adjusted_qty

        return await self.exchange.place_order(intent)

    async def _inspect_reduce_only_block(
        self,
        symbol: str,
        position_side: PositionSide,
    ) -> Optional[ReduceOnlyBlockInfo]:
        """复核 `-4118` 是否由同侧普通平仓挂单占满剩余仓位导致。"""
        if not self.exchange:
            return None
        return await self.exchange.inspect_reduce_only_block(
            symbol,
            position_side,
            client_order_id_prefix=self._client_order_id_prefix,
        )

    async def _maybe_retry_post_only_reject(
        self,
        *,
        engine: ExecutionEngine,
        intent: OrderIntent,
        result: OrderResult,
        rules: SymbolRules,
        market_state: MarketState,
    ) -> tuple[OrderIntent, OrderResult, bool]:
        """Post-only 被拒后，立即切换为 AGGRESSIVE_LIMIT 并重试一次。"""
        if result.error_code != "-5022":
            return intent, result, False
        if intent.time_in_force != TimeInForce.GTX:
            return intent, result, False

        state = engine.get_state(intent.symbol, intent.position_side)
        if state.mode != ExecutionMode.MAKER_ONLY:
            return intent, result, False
        if state.state not in (ExecutionState.PLACING, ExecutionState.IDLE, ExecutionState.COOLDOWN):
            return intent, result, False
        if (
            state.current_order_strategy_mode == StrategyMode.ORDERBOOK_PRESSURE
            and state.current_order_execution_preference == SignalExecutionPreference.PASSIVE
        ):
            return intent, result, False

        engine.set_mode(intent.symbol, intent.position_side, ExecutionMode.AGGRESSIVE_LIMIT, reason="post_only_reject_retry")
        state.current_order_mode = ExecutionMode.AGGRESSIVE_LIMIT

        price = engine.build_aggressive_limit_price(
            position_side=intent.position_side,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            tick_size=rules.tick_size,
        )
        retry_intent = OrderIntent(
            symbol=intent.symbol,
            side=intent.side,
            position_side=intent.position_side,
            qty=intent.qty,
            price=price,
            time_in_force=TimeInForce.GTC,
            reduce_only=intent.reduce_only,
            close_position=intent.close_position,
            order_type=intent.order_type,
            is_risk=intent.is_risk,
        )
        log_event(
            "order_retry",
            symbol=intent.symbol,
            side=intent.position_side.value,
            reason="post_only_reject_retry",
            from_mode="MAKER_ONLY",
            to_mode="AGGRESSIVE_LIMIT",
        )

        retry_result = await self._place_order(retry_intent)
        return retry_intent, retry_result, True

    async def _cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        """撤单回调"""
        if not self.exchange:
            return OrderResult(success=False, error_message="Exchange not initialized")

        # 风控兜底订单：撤单不受软限速约束
        is_risk_cancel = False
        engine = self.execution_engines.get(symbol)
        if engine:
            for side in (PositionSide.LONG, PositionSide.SHORT):
                state = engine.get_state(symbol, side)
                if state.current_order_id == order_id and state.current_order_is_risk:
                    is_risk_cancel = True
                    break

        now_ms = current_time_ms()
        if (not is_risk_cancel) and self.risk_manager and not self.risk_manager.can_cancel_order(current_ms=now_ms):
            log_event(
                "rate_limit",
                symbol=symbol,
                reason="cancel_order",
                order_id=order_id,
                max_cancels_per_sec=self.risk_manager.max_cancels_per_sec,
            )
            return OrderResult(success=False, status=OrderStatus.REJECTED, error_message="rate_limited: cancel_order")

        return await self.exchange.cancel_order(symbol, order_id)

    def _on_market_event(self, event: MarketEvent) -> None:
        """处理市场事件回调"""
        if self._market_recorder:
            self._market_recorder.record(event)

        if not self.signal_engine:
            return

        # 更新信号引擎的市场状态
        self.signal_engine.update_market(event)

        # bookTicker：采样 mid-price 用于 pressure 统计
        if (
            event.event_type == "book_ticker"
            and self._pressure_stats
            and event.best_bid is not None
            and event.best_ask is not None
            and event.best_bid > 0
            and event.best_ask > 0
        ):
            mid = (event.best_bid + event.best_ask) / 2
            self._pressure_stats.record_price(event.symbol, mid, event.timestamp_ms)

        # markPrice：同步到仓位缓存（用于 dist_to_liq 风控）
        if event.event_type == "mark_price" and event.mark_price is not None:
            symbol_positions = self._positions.get(event.symbol)
            if not symbol_positions:
                return
            for side, pos in list(symbol_positions.items()):
                symbol_positions[side] = Position(
                    symbol=pos.symbol,
                    position_side=pos.position_side,
                    position_amt=pos.position_amt,
                    entry_price=pos.entry_price,
                    unrealized_pnl=pos.unrealized_pnl,
                    leverage=pos.leverage,
                    mark_price=event.mark_price,
                    liquidation_price=pos.liquidation_price,
                )

    def _on_ws_reconnect(self, stream_type: str) -> None:
        """WS 重连成功回调：触发一次 REST 校准（positions + markets），并在校准期间暂停下单。"""
        if not self._running:
            return

        if self.config_loader and self.telegram_notifier:
            telegram_cfg = self.config_loader.config.global_.telegram
            if telegram_cfg.enabled and telegram_cfg.events.on_reconnect:
                self._schedule_telegram(
                    self.telegram_notifier.notify_reconnect(stream_type),
                    name=f"reconnect:{stream_type}",
                )

        if self._calibration_task and not self._calibration_task.done():
            return

        self._calibration_task = asyncio.create_task(self._calibrate_after_reconnect(stream_type))
        self._calibration_task.add_done_callback(
            lambda t, n="calibration": self._on_background_task_done(t, n)
        )

    @staticmethod
    def _should_refresh_on_account_event(event: AccountUpdateEvent) -> bool:
        """
        判断账户事件是否需要触发全量仓位刷新。

        触发条件：
        - 事件原因为保证金/资产划转相关（A）；
        - 或存在余额变化，且该事件不含仓位变化（覆盖未知划转 reason 变体）；
        - ORDER 事件即使有余额变化也不触发（避免每笔成交触发 REST）。
        """
        reason = (event.reason or "").upper()
        if reason in {"MARGIN_TRANSFER", "ASSET_TRANSFER", "MARGIN_TYPE_CHANGE"}:
            return True
        if not event.has_balance_delta:
            return False
        if reason == "ORDER":
            return False
        if not event.has_position_delta:
            return True
        return not reason

    def _on_account_update_event(self, event: AccountUpdateEvent) -> None:
        """处理 ACCOUNT_UPDATE 的账户级事件（用于触发仓位刷新）。"""
        if not self._running:
            return
        if not self._should_refresh_on_account_event(event):
            return

        reason = f"account_update:{event.reason or 'UNKNOWN'}"
        self._schedule_positions_refresh(
            reason=reason,
            debounce_s=self._margin_refresh_debounce_s,
        )

    async def _calibrate_after_reconnect(self, stream_type: str) -> None:
        """重连后 REST 校准：重新加载 markets/rules，并刷新仓位。"""
        if not self.exchange or not self.config_loader:
            return

        async with self._calibration_lock:
            if not self._running or self._shutdown_event.is_set():
                return

            self._calibrating = True
            log_event("calibration", reason=f"start stream={stream_type}")

            try:
                await self.exchange.load_markets()

                symbols = list(self._active_symbols)
                for symbol in symbols:
                    rules = self.exchange.get_rules(symbol)
                    if rules:
                        self._rules[symbol] = rules

                await self._fetch_all_positions()
                await self._rebuild_market_ws(reason=f"calibration:{stream_type}")
                await self._sync_protective_stops_all(reason=f"calibration:{stream_type}")
            except Exception as e:
                log_error(f"校准失败: {e}")
            finally:
                self._calibrating = False
                log_event("calibration", reason=f"done stream={stream_type}")

    def _on_order_update(self, update: OrderUpdate) -> None:
        """处理订单更新回调"""
        # 在事件循环中调度处理
        if self._running:
            asyncio.create_task(self._handle_order_update(update))

    def _on_algo_order_update(self, update: AlgoOrderUpdate) -> None:
        """处理 Algo 条件单更新回调（ALGO_UPDATE）。"""
        if not self._running:
            return

        # 只跟踪配置中已启用的 symbols
        if update.symbol not in self.execution_engines:
            return

        # 1. 我们自己的保护止损单：用前缀匹配，无条件触发 sync（不依赖 cp）
        if self.protective_stop_manager:
            is_own_algo = False
            if update.client_algo_id and update.client_algo_id.startswith(PROTECTIVE_STOP_PREFIX):
                is_own_algo = True
            elif update.algo_id and self.protective_stop_manager.is_own_algo_order(update.symbol, update.algo_id):
                is_own_algo = True

            if is_own_algo:
                self.protective_stop_manager.on_algo_order_update(update)
                self._schedule_protective_stop_sync(
                    update.symbol,
                    reason=f"our_algo:{update.status}",
                )
                return

        # 2. 外部 stop/tp 条件单：closePosition(cp)=True 或 reduceOnly(R)=True 均视为外部接管
        if update.order_type and update.order_type.upper() in _STOP_ORDER_TYPES and (
            update.close_position is True or update.reduce_only is True
        ):
            now_ms = current_time_ms()
            if update.position_side == PositionSide.LONG:
                sides = [PositionSide.LONG]
            elif update.position_side == PositionSide.SHORT:
                sides = [PositionSide.SHORT]
            else:
                # ps 不明确时（例如 BOTH），保守认为两边都可能被外部条件单占用
                sides = [PositionSide.LONG, PositionSide.SHORT]
            terminal = update.status.upper() in {"CANCELED", "EXPIRED", "FINISHED", "REJECTED", "FILLED", "TRIGGERED"}
            for side in sides:
                if terminal:
                    self._external_takeover_request_release(
                        update.symbol,
                        side,
                        now_ms=now_ms,
                        source="ws_algo",
                        order_id=update.algo_id,
                        client_order_id=update.client_algo_id,
                        order_type=update.order_type,
                        status=update.status,
                    )
                else:
                    self._external_takeover_set(
                        update.symbol,
                        side,
                        now_ms=now_ms,
                        source="ws_algo",
                        order_id=update.algo_id,
                        client_order_id=update.client_algo_id,
                        order_type=update.order_type,
                    )
            self._schedule_protective_stop_sync(
                update.symbol,
                reason=f"external_algo:{update.status}:{update.order_type}:{update.close_position}:{update.reduce_only}",
            )

    def _log_no_position(
        self,
        symbol: str,
        position_side: PositionSide,
        *,
        cleared: bool = False,
    ) -> None:
        """
        打印无持仓提示日志（去重）。

        Args:
            cleared: True 表示仓位刚归零，False 表示启动时无仓位
        """
        key = (symbol, position_side)
        if key in self._no_position_logged:
            return
        self._no_position_logged.add(key)
        side = position_side.value
        logger = get_logger()
        if cleared:
            # 仓位归零：延迟打印，让 ORDER_FILL 日志先显示（因果顺序更直观）
            async def _delayed_log() -> None:
                await asyncio.sleep(0.1)  # 100ms 延迟
                logger.info(f"{symbol} {side} ✅ 仓位已全部平掉")
                logger.info(f"{symbol} {side} ⏳ 当前无持仓，等待开仓...")
            asyncio.create_task(_delayed_log())
        else:
            # 启动时无仓位：立即打印
            logger.info(f"{symbol} {side} ⏳ 当前无持仓，等待开仓...")

    def _clear_no_position_log(self, symbol: str, position_side: PositionSide) -> None:
        self._no_position_logged.discard((symbol, position_side))

    def _on_position_update(self, update: PositionUpdate) -> None:
        """处理仓位更新回调（ACCOUNT_UPDATE）。"""
        if not self._running:
            return

        symbol = update.symbol
        if symbol not in self._active_symbols:
            if abs(update.position_amt) > Decimal("0"):
                task = asyncio.create_task(self._handle_new_symbol(symbol, update))
                task.add_done_callback(lambda t, n=f"new_symbol:{symbol}": self._on_background_task_done(t, n))
            return

        symbol_positions = self._positions.setdefault(symbol, {})
        prev = symbol_positions.get(update.position_side)
        prev_amt = prev.position_amt if prev else Decimal("0")

        if prev_amt != update.position_amt:
            key = (symbol, update.position_side)
            self._position_revision[key] = self._position_revision.get(key, 0) + 1
            self._position_last_change[key] = (prev_amt, update.position_amt)
            self._position_update_events.setdefault(key, asyncio.Event()).set()

        # 0 仓位：删除缓存，避免"幽灵仓位"
        if abs(update.position_amt) == Decimal("0"):
            symbol_positions.pop(update.position_side, None)
            if abs(prev_amt) > Decimal("0"):
                log_position_update(
                    symbol=symbol,
                    side=update.position_side.value,
                    position_amt=Decimal("0"),
                )
                self._log_no_position(symbol, update.position_side, cleared=True)
                # 仓位归零：尽快撤销该侧遗留挂单，避免后续触发导致反向开仓
                asyncio.create_task(
                    self._cancel_run_prefix_orders_for_side(
                        symbol=symbol,
                        position_side=update.position_side,
                        reason="position_zero",
                    )
                )
            self._schedule_protective_stop_sync(symbol, reason=f"position_update:{update.position_side.value}")
            return

        self._clear_no_position_log(symbol, update.position_side)

        # 开仓/加仓告警（本程序主目标是 reduce-only 平仓，任何加仓都值得提示）
        if (
            self._positions_ready
            and self.config_loader
            and self.telegram_notifier
            and self.telegram_notifier.enabled
        ):
            telegram_cfg = self.config_loader.config.global_.telegram
            if telegram_cfg.enabled and getattr(telegram_cfg.events, "on_open_alert", False):
                if abs(update.position_amt) > abs(prev_amt):
                    self._schedule_telegram(
                        self.telegram_notifier.notify_open_alert(
                            symbol=symbol,
                            side=update.position_side.value,
                            position_before=str(abs(prev_amt)),
                            position_after=str(abs(update.position_amt)),
                        ),
                        name=f"open_alert:{symbol}:{update.position_side.value}",
                    )

        entry_price = (
            update.entry_price
            if update.entry_price is not None
            else (prev.entry_price if prev else Decimal("0"))
        )
        unrealized_pnl = (
            update.unrealized_pnl
            if update.unrealized_pnl is not None
            else (prev.unrealized_pnl if prev else Decimal("0"))
        )

        merged = Position(
            symbol=symbol,
            position_side=update.position_side,
            position_amt=update.position_amt,
            entry_price=entry_price,
            unrealized_pnl=unrealized_pnl,
            leverage=prev.leverage if prev else self._symbol_leverage.get(symbol, 1),
            mark_price=prev.mark_price if prev else None,
            liquidation_price=prev.liquidation_price if prev else None,
        )

        symbol_positions[update.position_side] = merged
        if prev_amt != update.position_amt:
            log_position_update(
                symbol=symbol,
                side=update.position_side.value,
                position_amt=update.position_amt,
            )
            self._schedule_protective_stop_sync(symbol, reason=f"position_update:{update.position_side.value}")

    def _on_leverage_update(self, update: LeverageUpdate) -> None:
        """处理杠杆更新回调（ACCOUNT_CONFIG_UPDATE）。"""
        if not self._running:
            return

        if update.symbol not in self._active_symbols:
            if update.leverage > 0:
                self._symbol_leverage[update.symbol] = update.leverage
            return

        if update.leverage <= 0:
            return

        previous = self._symbol_leverage.get(update.symbol)
        if previous == update.leverage:
            return

        self._symbol_leverage[update.symbol] = update.leverage

        symbol_positions = self._positions.get(update.symbol)
        if symbol_positions:
            for side, pos in list(symbol_positions.items()):
                if pos.leverage != update.leverage:
                    symbol_positions[side] = Position(
                        symbol=pos.symbol,
                        position_side=pos.position_side,
                        position_amt=pos.position_amt,
                        entry_price=pos.entry_price,
                        unrealized_pnl=pos.unrealized_pnl,
                        leverage=update.leverage,
                        mark_price=pos.mark_price,
                        liquidation_price=pos.liquidation_price,
                    )

        log_event(
            "leverage",
            symbol=update.symbol,
            reason="ws_account_config_update",
            leverage=update.leverage,
        )

    async def _handle_order_update(self, update: OrderUpdate) -> None:
        """异步处理订单更新"""
        engine = self.execution_engines.get(update.symbol)
        if engine:
            partial_fill_reason: Optional[str] = None
            if update.status == OrderStatus.PARTIALLY_FILLED and update.filled_qty > Decimal("0"):
                state = engine.get_state(update.symbol, update.position_side)
                if state.current_order_id == update.order_id and state.current_order_reason in _PRESSURE_REASONS:
                    partial_fill_reason = state.current_order_reason

            await engine.on_order_update(update, current_time_ms())

            if partial_fill_reason is not None:
                self._record_pressure_fill_once(
                    symbol=update.symbol,
                    position_side=update.position_side,
                    order_id=update.order_id,
                    reason=partial_fill_reason,
                    ts_ms=update.timestamp_ms,
                )
        if self.protective_stop_manager:
            await self.protective_stop_manager.on_order_update(update)
            if update.client_order_id.startswith(PROTECTIVE_STOP_PREFIX):
                self._schedule_protective_stop_sync(update.symbol, reason=f"order_update:{update.status.value}")
            elif update.order_type and update.order_type.upper() in _STOP_ORDER_TYPES and (
                update.close_position is True or update.reduce_only is True
            ):
                # 外部 closePosition 或 reduceOnly 条件单状态变化也可能导致"外部接管/释放"
                now_ms = current_time_ms()
                if update.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.FILLED, OrderStatus.REJECTED):
                    self._external_takeover_request_release(
                        update.symbol,
                        update.position_side,
                        now_ms=now_ms,
                        source="ws_order",
                        order_id=update.order_id,
                        client_order_id=update.client_order_id,
                        order_type=update.order_type,
                        status=update.status.value,
                    )
                else:
                    self._external_takeover_set(
                        update.symbol,
                        update.position_side,
                        now_ms=now_ms,
                        source="ws_order",
                        order_id=update.order_id,
                        client_order_id=update.client_order_id,
                        order_type=update.order_type,
                    )
                self._schedule_protective_stop_sync(
                    update.symbol,
                    reason=f"external_stop:{update.status.value}:{update.order_type}:{update.close_position}:{update.reduce_only}",
                )

    async def _fetch_all_positions(self) -> None:
        """获取账户所有仓位（不依赖配置 symbols）。"""
        if not self.exchange:
            return

        positions = await self.exchange.fetch_positions(symbol=None)
        symbols = sorted({pos.symbol for pos in positions})

        leverage_map: Dict[str, int] = {}
        try:
            leverage_map = await self.exchange.fetch_leverage_map(symbols)
        except Exception as e:
            log_error(f"启动杠杆拉取失败: {e}")
        if leverage_map:
            log_event(
                "leverage_snapshot",
                reason="position_risk",
                count=len(leverage_map),
            )

        self._positions = {}

        for pos in positions:
            symbol = pos.symbol
            if symbol not in self._active_symbols:
                await self._ensure_symbol_initialized(symbol)

            leverage_override = leverage_map.get(symbol) or self._symbol_leverage.get(symbol)
            if leverage_override and leverage_override > 0 and pos.leverage != leverage_override:
                pos.leverage = leverage_override
            if pos.leverage > 0:
                self._symbol_leverage[symbol] = pos.leverage

            self._positions.setdefault(symbol, {})[pos.position_side] = pos
            if abs(pos.position_amt) > Decimal("0"):
                self._clear_no_position_log(symbol, pos.position_side)
                log_position_update(
                    symbol=symbol,
                    side=pos.position_side.value,
                    position_amt=pos.position_amt,
                )

    async def _ensure_symbol_initialized(self, symbol: str) -> bool:
        """确保 symbol 已初始化（幂等）。"""
        if not self.config_loader or not self.exchange or not self.signal_engine:
            return False

        async with self._symbol_init_lock:
            if symbol in self._active_symbols:
                return False

            rules = self.exchange.get_rules(symbol)
            if not rules:
                await self.exchange.load_markets()
                rules = self.exchange.get_rules(symbol)
            if not rules:
                log_error("未找到交易规则", symbol=symbol)
                return False

            cfg = self.config_loader.get_symbol_config(symbol)
            self._rules[symbol] = rules
            self._symbol_configs[symbol] = cfg

            pressure_config: PressureSignalConfig | None = None
            if cfg.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE.value and cfg.pressure_exit_enabled:
                threshold_qty = cfg.pressure_exit_threshold_qty
                sustain_ms = cfg.pressure_exit_sustain_ms
                passive_level = cfg.pressure_exit_passive_level
                base_mult = cfg.base_mult
                use_roi_mult = (
                    cfg.pressure_exit_use_roi_mult
                    if cfg.pressure_exit_use_roi_mult is not None
                    else cfg.execution_use_roi_mult
                )
                use_accel_mult = (
                    cfg.pressure_exit_use_accel_mult
                    if cfg.pressure_exit_use_accel_mult is not None
                    else cfg.execution_use_accel_mult
                )
                active_recheck_cooldown_ms = cfg.pressure_exit_active_recheck_cooldown_ms
                active_recheck_cooldown_jitter_pct = cfg.pressure_exit_active_recheck_cooldown_jitter_pct
                active_burst_window_ms = cfg.pressure_exit_active_burst_window_ms
                active_burst_max_attempts = cfg.pressure_exit_active_burst_max_attempts
                active_burst_max_fills = cfg.pressure_exit_active_burst_max_fills
                active_burst_pause_min_ms = cfg.pressure_exit_active_burst_pause_min_ms
                active_burst_pause_max_ms = cfg.pressure_exit_active_burst_pause_max_ms
                passive_ttl_ms = cfg.pressure_exit_passive_ttl_ms
                passive_ttl_jitter_pct = cfg.pressure_exit_passive_ttl_jitter_pct
                qty_jitter_pct = cfg.pressure_exit_qty_jitter_pct
                qty_anti_repeat_lookback = cfg.pressure_exit_qty_anti_repeat_lookback
                if (
                    threshold_qty is None
                    or sustain_ms is None
                    or passive_level is None
                    or base_mult is None
                    or active_recheck_cooldown_ms is None
                    or passive_ttl_ms is None
                ):
                    log_error("盘口量策略配置缺失", symbol=symbol)
                    return False
                pressure_config = PressureSignalConfig(
                    threshold_qty=threshold_qty,
                    sustain_ms=sustain_ms,
                    passive_level=passive_level,
                    base_mult=base_mult,
                    use_roi_mult=bool(use_roi_mult),
                    use_accel_mult=bool(use_accel_mult),
                    active_recheck_cooldown_ms=active_recheck_cooldown_ms,
                    passive_ttl_ms=passive_ttl_ms,
                    active_recheck_cooldown_jitter_pct=(
                        active_recheck_cooldown_jitter_pct
                        if active_recheck_cooldown_jitter_pct is not None
                        else Decimal("0.15")
                    ),
                    passive_ttl_jitter_pct=(
                        passive_ttl_jitter_pct
                        if passive_ttl_jitter_pct is not None
                        else Decimal("0.15")
                    ),
                    active_burst_window_ms=(
                        active_burst_window_ms
                        if active_burst_window_ms is not None
                        else 10000
                    ),
                    active_burst_max_attempts=(
                        active_burst_max_attempts
                        if active_burst_max_attempts is not None
                        else 8
                    ),
                    active_burst_max_fills=(
                        active_burst_max_fills
                        if active_burst_max_fills is not None
                        else 5
                    ),
                    active_burst_pause_min_ms=(
                        active_burst_pause_min_ms
                        if active_burst_pause_min_ms is not None
                        else 2500
                    ),
                    active_burst_pause_max_ms=(
                        active_burst_pause_max_ms
                        if active_burst_pause_max_ms is not None
                        else 6000
                    ),
                    qty_jitter_pct=qty_jitter_pct if qty_jitter_pct is not None else Decimal("0.15"),
                    qty_anti_repeat_lookback=qty_anti_repeat_lookback if qty_anti_repeat_lookback is not None else 3,
                )

            self.signal_engine.configure_symbol(
                symbol,
                strategy_mode=StrategyMode(cfg.strategy_mode),
                pressure_config=pressure_config,
                min_signal_interval_ms=cfg.min_signal_interval_ms,
                use_roi_mult=cfg.execution_use_roi_mult,
                use_accel_mult=cfg.execution_use_accel_mult,
                accel_window_ms=cfg.accel_window_ms,
                accel_tiers=[(t.ret, t.mult) for t in cfg.accel_tiers],
                roi_tiers=[(t.roi, t.mult) for t in cfg.roi_tiers],
            )

            self.execution_engines[symbol] = ExecutionEngine(
                place_order=self._place_order,
                cancel_order=self._cancel_order,
                on_fill=self._on_engine_fill,
                fetch_order_trade_meta=self.exchange.fetch_order_trade_meta,
                inspect_reduce_only_block=self._inspect_reduce_only_block,
                order_ttl_ms=cfg.order_ttl_ms,
                repost_cooldown_ms=cfg.repost_cooldown_ms,
                base_mult=cfg.base_mult,
                maker_price_mode=cfg.maker_price_mode,
                maker_n_ticks=cfg.maker_n_ticks,
                maker_safety_ticks=cfg.maker_safety_ticks,
                maker_timeouts_to_escalate=cfg.maker_timeouts_to_escalate,
                aggr_fills_to_deescalate=cfg.aggr_fills_to_deescalate,
                aggr_timeouts_to_deescalate=cfg.aggr_timeouts_to_deescalate,
                fill_rate_feedback_enabled=cfg.fill_rate_feedback_enabled,
                fill_rate_window_min=cfg.fill_rate_window_min,
                fill_rate_low_threshold=cfg.fill_rate_low_threshold,
                fill_rate_high_threshold=cfg.fill_rate_high_threshold,
                fill_rate_log_windows_min=cfg.fill_rate_log_windows_min,
                max_mult=cfg.max_mult,
                max_order_notional=cfg.max_order_notional,
            )

            self._active_symbols.add(symbol)
            get_logger().info(
                f"{symbol} 规则: tick={rules.tick_size}, step={rules.step_size}, min_qty={rules.min_qty}"
            )
            log_event("symbol_added", symbol=symbol, reason="position_detected")
            return True

    async def _rebuild_market_ws(self, *, reason: str) -> None:
        """按 active_symbols 重建市场 WS 连接（由 Application 管理任务）。"""
        if not self.config_loader:
            return

        symbols = sorted(self._active_symbols)
        if not symbols:
            return

        existing_symbols = set(self.market_ws.symbols) if self.market_ws else set()
        if self.market_ws and set(symbols) == existing_symbols and self.market_ws.is_connected:
            return

        if self._market_ws_task and not self._market_ws_task.done():
            self._market_ws_task.cancel()
            await self._gather_with_timeout([self._market_ws_task], timeout_s=1.0, name="market_ws 任务取消")

        if self.market_ws:
            await self.market_ws.disconnect()

        ws_config = self.config_loader.config.global_.ws
        proxy = self.config_loader.config.global_.proxy
        depth_symbols = [
            symbol
            for symbol in symbols
            if self._symbol_configs.get(symbol)
            and self._symbol_configs[symbol].strategy_mode == StrategyMode.ORDERBOOK_PRESSURE.value
            and self._symbol_configs[symbol].pressure_exit_enabled
        ]

        self.market_ws = MarketWSClient(
            symbols=symbols,
            depth_symbols=depth_symbols,
            on_event=self._on_market_event,
            on_reconnect=self._on_ws_reconnect,
            initial_delay_ms=ws_config.reconnect.initial_delay_ms,
            max_delay_ms=ws_config.reconnect.max_delay_ms,
            multiplier=ws_config.reconnect.multiplier,
            stale_data_ms=ws_config.stale_data_ms,
            proxy=proxy,
        )

        self._market_ws_task = asyncio.create_task(self.market_ws.connect())
        self._market_ws_task.add_done_callback(
            lambda t, n=f"market_ws.connect:{reason}": self._on_background_task_done(t, n)
        )

    async def _handle_new_symbol(self, symbol: str, update: PositionUpdate) -> None:
        """运行时发现新 symbol，初始化并接管。"""
        is_new = await self._ensure_symbol_initialized(symbol)
        if symbol not in self._active_symbols:
            return

        self._positions.setdefault(symbol, {})[update.position_side] = Position(
            symbol=symbol,
            position_side=update.position_side,
            position_amt=update.position_amt,
            entry_price=update.entry_price or Decimal("0"),
            unrealized_pnl=update.unrealized_pnl or Decimal("0"),
            leverage=self._symbol_leverage.get(symbol, 1),
            mark_price=None,
            liquidation_price=None,
        )
        log_position_update(
            symbol=symbol,
            side=update.position_side.value,
            position_amt=update.position_amt,
        )

        await self._refresh_position(symbol)

        if is_new:
            for position_side in (PositionSide.LONG, PositionSide.SHORT):
                task = asyncio.create_task(self._side_loop(symbol, position_side))
                self._side_tasks.add(task)
                task.add_done_callback(
                    lambda t, n=f"side_loop:{symbol}:{position_side.value}": self._on_background_task_done(t, n)
                )

            await self._rebuild_market_ws(reason="new_symbol")
            self._schedule_protective_stop_sync(symbol, reason="new_symbol")
            log_event("symbol_runtime_added", symbol=symbol, reason="position_detected")

    def _log_startup_pos(self) -> None:
        """启动时打印所有 symbol+side 的持仓状态。"""
        logger = get_logger()
        for symbol in sorted(self._active_symbols):
            for position_side in (PositionSide.LONG, PositionSide.SHORT):
                position = self._positions.get(symbol, {}).get(position_side)
                side = position_side.value
                if position and abs(position.position_amt) > Decimal("0"):
                    logger.info(f"{symbol} {side} 📦 当前持仓 {position.position_amt}，准备执行平仓...")
                else:
                    self._log_no_position(symbol, position_side, cleared=False)

    async def run(self) -> None:
        """运行应用"""
        logger = get_logger()
        self._running = True
        self._started_at = datetime.now()

        try:
            # 获取初始仓位
            logger.info("获取初始仓位...")
            await self._fetch_all_positions()
            self._log_startup_pos()
            self._positions_ready = True

            # 连接 WebSocket
            logger.info("连接 WebSocket...")
            if self.user_data_ws:
                self._user_data_ws_task = asyncio.create_task(self.user_data_ws.connect())
                self._user_data_ws_task.add_done_callback(
                    lambda t, n="user_data_ws.connect": self._on_background_task_done(t, n)
                )
            if self._active_symbols:
                await self._rebuild_market_ws(reason="startup")
            else:
                logger.info("当前账户无持仓，等待新仓位...")

            await self._sync_protective_stops_all(reason="startup")

            if self._shutdown_event.is_set():
                return

            # 等待数据就绪
            logger.info("等待数据就绪...")
            await asyncio.sleep(2)

            # 启动主循环任务
            self._main_loop_task = asyncio.create_task(self._main_loop())
            self._timeout_check_task = asyncio.create_task(self._timeout_check_loop())
            self._fill_rate_log_task = asyncio.create_task(self._fill_rate_log_loop())
            self._pressure_stats_task = asyncio.create_task(self._pressure_stats_loop())
            if self.exchange:
                self._position_refresh_loop_task = asyncio.create_task(self._position_refresh_loop())

            # 启动 Telegram Bot（如已配置）
            if self.telegram_bot:
                self._telegram_bot_task = asyncio.create_task(self.telegram_bot.start())
                self._telegram_bot_task.add_done_callback(
                    lambda t: self._on_background_task_done(t, "telegram_bot")
                )

            # 等待关闭信号
            await self._shutdown_event.wait()

        except Exception as e:
            log_error(str(e))
            raise
        finally:
            await self.shutdown()

    async def _main_loop(self) -> None:
        """主事件循环（多 symbol 并发）：为每个 symbol+side 启动独立任务。"""
        symbols = list(self._active_symbols)
        for symbol in symbols:
            for position_side in (PositionSide.LONG, PositionSide.SHORT):
                task = asyncio.create_task(self._side_loop(symbol, position_side))
                self._side_tasks.add(task)
                task.add_done_callback(
                    lambda t, n=f"side_loop:{symbol}:{position_side.value}": self._on_background_task_done(t, n)
                )

        # 等待关闭信号；side tasks 会在 shutdown 时被取消/退出
        await self._shutdown_event.wait()

    async def _evaluate_signals(self) -> None:
        """评估所有仓位的平仓信号"""
        if not self.signal_engine or not self.config_loader:
            return
        if self._calibrating:
            return

        current_ms = current_time_ms()
        symbols = list(self._active_symbols)

        for symbol in symbols:
            # 检查数据是否就绪
            if not self.signal_engine.is_data_ready(symbol):
                self._reset_pressure_dwell_for_symbol(symbol, reason="data_not_ready")
                continue

            # 检查是否有陈旧数据
            if self.market_ws and self.market_ws.is_stale(symbol):
                self._reset_pressure_dwell_for_symbol(symbol, reason="market_stale")
                continue

            if self.signal_engine.is_strategy_data_stale(
                symbol,
                current_ms,
                self.config_loader.config.global_.ws.stale_data_ms,
            ):
                self._reset_pressure_dwell_for_symbol(symbol, reason="strategy_data_stale")
                continue

            # 获取市场状态
            market_state = self.signal_engine.get_market_state(symbol)
            if not market_state:
                continue

            # 获取交易规则
            rules = self._rules.get(symbol)
            if not rules:
                continue

            # 获取执行引擎
            engine = self.execution_engines.get(symbol)
            if not engine:
                continue

            # 评估 LONG 和 SHORT 仓位
            for position_side in [PositionSide.LONG, PositionSide.SHORT]:
                await self._evaluate_side(
                    symbol=symbol,
                    position_side=position_side,
                    engine=engine,
                    rules=rules,
                    market_state=market_state,
                    current_ms=current_ms,
                )

    async def _side_loop(self, symbol: str, position_side: PositionSide) -> None:
        """单个 symbol+side 的信号评估循环（独立任务）。"""
        while self._running:
            try:
                await self._evaluate_symbol_side(symbol, position_side)
                await asyncio.sleep(0.05)  # 50ms 间隔
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"side_loop 错误: {symbol} {position_side.value} - {e}")
                await asyncio.sleep(1)

    async def _evaluate_symbol_side(self, symbol: str, position_side: PositionSide) -> None:
        """评估指定 symbol 的指定方向仓位。"""
        if not self.signal_engine or not self.config_loader:
            return
        if self._calibrating:
            return
        if self.pause_manager.is_paused(symbol):
            return

        position = self._positions.get(symbol, {}).get(position_side)
        if not position or abs(position.position_amt) == Decimal("0"):
            self._reset_pressure_dwell(symbol, position_side, reason="position_flat")
            return

        if self.market_ws and self.market_ws.is_stale(symbol):
            self._reset_pressure_dwell(symbol, position_side, reason="market_stale")
            return

        if self.signal_engine.is_strategy_data_stale(
            symbol,
            current_time_ms(),
            self.config_loader.config.global_.ws.stale_data_ms,
        ):
            self._reset_pressure_dwell(symbol, position_side, reason="strategy_data_stale")
            return

        market_state = self.signal_engine.get_market_state(symbol)
        if not market_state or market_state.best_bid <= Decimal("0") or market_state.best_ask <= Decimal("0"):
            return

        rules = self._rules.get(symbol)
        if not rules:
            return

        engine = self.execution_engines.get(symbol)
        if not engine:
            return

        # 强平兜底：不依赖信号与节流，按 dist_to_liq 分级强制出手
        panic_cfg = self.config_loader.config.global_.risk.panic_close
        dist_to_liq: Optional[Decimal] = None
        if position.mark_price and position.mark_price > Decimal("0") and position.liquidation_price and position.liquidation_price > Decimal("0"):
            dist_to_liq = abs(position.mark_price - position.liquidation_price) / position.mark_price

        selected_tier = None
        if panic_cfg.enabled and dist_to_liq is not None and panic_cfg.tiers:
            for tier in sorted(panic_cfg.tiers, key=lambda t: t.dist_to_liq):
                if dist_to_liq <= tier.dist_to_liq:
                    selected_tier = tier
                    break

        state = engine.get_state(symbol, position_side)
        key = (symbol, position_side)

        if selected_tier is not None and dist_to_liq is not None:
            # 进入/变更档位时记录一次（避免 50ms 循环刷屏）
            prev_tier = self._panic_last_tier.get(key)
            if prev_tier != selected_tier.dist_to_liq:
                self._panic_last_tier[key] = selected_tier.dist_to_liq
                dist_display = format_decimal(dist_to_liq, precision=4)
                tier_display = format_decimal(selected_tier.dist_to_liq, precision=4)
                slice_display = format_decimal(selected_tier.slice_ratio, precision=4)
                risk_levels = self.config_loader.config.global_.risk.levels if self.config_loader else {}
                log_event(
                    "risk",
                    symbol=symbol,
                    side=position_side.value,
                    risk_stage="panic_close",
                    risk_level=risk_levels.get("panic_close"),
                    reason="panic_close",
                    dist_to_liq=dist_display,
                    tier_dist_to_liq=tier_display,
                    slice_ratio=slice_display,
                )
                if self.telegram_notifier:
                    telegram_cfg = self.config_loader.config.global_.telegram
                    if telegram_cfg.enabled and telegram_cfg.events.on_risk_trigger:
                        self._schedule_telegram(
                            self.telegram_notifier.notify_risk_trigger(
                                symbol=symbol,
                                position_side=position_side.value,
                                dist_to_liq=dist_display or str(dist_to_liq),
                            ),
                            name=f"risk_trigger:{symbol}:{position_side.value}",
                        )

            # 配置兜底覆盖项：TTL 固定比例 + maker 连续超时升级阈值
            if not state.risk_active:
                state.risk_active = True
                state.ttl_ms_override = max(
                    1,
                    int(Decimal(engine.order_ttl_ms) * panic_cfg.ttl_percent),
                )
            state.maker_timeouts_to_escalate_override = int(selected_tier.maker_timeouts_to_escalate)

            # 检查冷却期
            engine.check_cooldown(symbol, position_side, current_time_ms())

            intent = await engine.on_panic_close(
                symbol=symbol,
                position_side=position_side,
                position_amt=position.position_amt,
                rules=rules,
                market_state=market_state,
                current_ms=current_time_ms(),
                slice_ratio=selected_tier.slice_ratio,
                reason=f"panic_close@{selected_tier.dist_to_liq}",
            )

            if intent:
                result = await self._place_order(intent)
                intent, result, _ = await self._maybe_retry_post_only_reject(
                    engine=engine,
                    intent=intent,
                    result=result,
                    rules=rules,
                    market_state=market_state,
                )
                await engine.on_order_placed(
                    intent=intent,
                    result=result,
                    current_ms=current_time_ms(),
                )
            return

        # 离开兜底区：回收覆盖项（仅在空闲时清理，避免影响在途订单）
        if state.risk_active and state.state == ExecutionState.IDLE:
            state.risk_active = False
            state.ttl_ms_override = None
            state.maker_timeouts_to_escalate_override = None
            self._panic_last_tier.pop(key, None)

        # 正常路径：需要信号引擎数据就绪
        if not self.signal_engine.is_data_ready(symbol):
            return

        await self._evaluate_side(
            symbol=symbol,
            position_side=position_side,
            engine=engine,
            rules=rules,
            market_state=market_state,
            current_ms=current_time_ms(),
        )

    async def _evaluate_side(
        self,
        symbol: str,
        position_side: PositionSide,
        engine: ExecutionEngine,
        rules: SymbolRules,
        market_state,
        current_ms: int,
    ) -> None:
        """评估单侧仓位"""
        # 获取仓位
        position = self._positions.get(symbol, {}).get(position_side)
        if not position or abs(position.position_amt) == Decimal("0"):
            self._reset_pressure_dwell(symbol, position_side, reason="position_flat")
            return

        # 检查冷却期
        engine.check_cooldown(symbol, position_side, current_ms)

        if self.signal_engine and self.config_loader and self.signal_engine.is_strategy_data_stale(
            symbol,
            current_ms,
            self.config_loader.config.global_.ws.stale_data_ms,
        ):
            self._reset_pressure_dwell(symbol, position_side, reason="strategy_data_stale")
            return

        state = engine.get_state(symbol, position_side)
        risk_flag: RiskFlag | None = None
        dist_display: str | None = None
        risk_stage: str | None = None
        risk_levels = self.config_loader.config.global_.risk.levels if self.config_loader else {}
        if self.risk_manager:
            symbol_cfg = self._symbol_configs.get(symbol)
            symbol_threshold = symbol_cfg.liq_distance_threshold if symbol_cfg else None
            risk_flag = self.risk_manager.check_risk(position, liq_distance_threshold=symbol_threshold)
            if risk_flag.dist_to_liq is not None:
                dist_display = format_decimal(risk_flag.dist_to_liq, precision=4)

            if risk_flag.is_triggered and risk_flag.dist_to_liq is not None:
                risk_stage = risk_flag.reason or "liq_distance"
                if (not state.liq_distance_active) or state.liq_distance_reason != risk_stage:
                    state.liq_distance_active = True
                    state.liq_distance_reason = risk_stage
                    log_event(
                        "risk",
                        symbol=symbol,
                        side=position_side.value,
                        mode=ExecutionMode.AGGRESSIVE_LIMIT.value,
                        risk_stage=risk_stage,
                        risk_level=risk_levels.get(risk_stage),
                        reason=risk_flag.reason,
                        dist_to_liq=dist_display,
                    )
                    if self.config_loader and self.telegram_notifier:
                        telegram_cfg = self.config_loader.config.global_.telegram
                        if telegram_cfg.enabled and telegram_cfg.events.on_risk_trigger:
                            self._schedule_telegram(
                                self.telegram_notifier.notify_risk_trigger(
                                    symbol=symbol,
                                    position_side=position_side.value,
                                    dist_to_liq=dist_display or str(risk_flag.dist_to_liq),
                                ),
                                name=f"risk_trigger:{symbol}:{position_side.value}",
                            )
            elif state.liq_distance_active:
                log_event(
                    "risk",
                    level="info",
                    symbol=symbol,
                    side=position_side.value,
                    risk_stage="liq_distance_recovered",
                    reason="liq_distance_recovered",
                    prev_risk_stage=state.liq_distance_reason,
                    dist_to_liq=dist_display,
                )
                state.liq_distance_active = False
                state.liq_distance_reason = None

        # 评估信号
        signal = self.signal_engine.evaluate(  # type: ignore[union-attr]
            symbol=symbol,
            position_side=position_side,
            position=position,
            current_ms=current_ms,
        )

        if signal and signal.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE:
            self._record_pressure_trigger_edge(signal, ts_ms=current_ms)
        else:
            self._clear_pressure_trigger_signature(symbol, position_side)

        if signal:
            # 风险兜底：接近强平时强制更激进的执行模式（优先级高于普通 maker）
            # orderbook_price：切换执行模式到 AGGRESSIVE_LIMIT
            # orderbook_pressure：仅做日志/告警，不改写信号的主动/被动语义（panic_close 兜底）
            if risk_flag and risk_flag.is_triggered and risk_flag.dist_to_liq is not None:
                target_mode = ExecutionMode.AGGRESSIVE_LIMIT
                if state.mode != target_mode:
                    engine.set_mode(symbol, position_side, target_mode, reason="risk_trigger")

            # 主动信号抢占被动单
            state = engine.get_state(symbol, position_side)
            if (
                signal.execution_preference == SignalExecutionPreference.AGGRESSIVE
                and state.state == ExecutionState.WAITING
                and state.current_order_execution_preference == SignalExecutionPreference.PASSIVE
            ):
                cancelled = await engine.cancel_current_order_for_preempt(
                    symbol=symbol,
                    position_side=position_side,
                    current_ms=current_ms,
                )
                if cancelled:
                    await self._refresh_position(symbol)
                return

            # 成交率反馈：低成交率直接切到激进限价，影响当前信号下单
            if engine.fill_rate_feedback_enabled and signal.strategy_mode == StrategyMode.ORDERBOOK_PRICE:
                engine.refresh_fill_rate(symbol, position_side, current_ms)
                state = engine.get_state(symbol, position_side)
                if state.fill_rate_bucket == "low" and state.mode == ExecutionMode.MAKER_ONLY:
                    engine.set_mode(symbol, position_side, ExecutionMode.AGGRESSIVE_LIMIT, reason="fill_rate_low")

            # improve 信号直接吃单：价格正在朝有利方向移动，跳过 MAKER_ONLY 直接使用 AGGRESSIVE_LIMIT
            if (
                signal.strategy_mode == StrategyMode.ORDERBOOK_PRICE
                and signal.reason in (SignalReason.LONG_BID_IMPROVE, SignalReason.SHORT_ASK_IMPROVE)
            ):
                state = engine.get_state(symbol, position_side)
                if state.mode != ExecutionMode.AGGRESSIVE_LIMIT:
                    engine.set_mode(symbol, position_side, ExecutionMode.AGGRESSIVE_LIMIT, reason="improve_signal")

            # 处理信号
            intent = await engine.on_signal(
                signal=signal,
                position_amt=position.position_amt,
                rules=rules,
                market_state=market_state,
                current_ms=current_ms,
            )

            if intent:
                # 下单
                result = await self._place_order(intent)
                intent, result, _ = await self._maybe_retry_post_only_reject(
                    engine=engine,
                    intent=intent,
                    result=result,
                    rules=rules,
                    market_state=market_state,
                )
                await engine.on_order_placed(
                    intent=intent,
                    result=result,
                    current_ms=current_time_ms(),
                )

                # 更新仓位
                if result.success:
                    if (
                        signal.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE
                        and signal.execution_preference == SignalExecutionPreference.AGGRESSIVE
                        and self.signal_engine
                    ):
                        self.signal_engine.record_pressure_active_attempt(
                            symbol,
                            position_side,
                            ts_ms=current_ms,
                        )
                    if signal.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE and self._pressure_stats:
                        self._pressure_stats.record_attempt(
                            symbol=symbol,
                            side=position_side.value,
                            is_active=(signal.execution_preference == SignalExecutionPreference.AGGRESSIVE),
                            mid_price=(signal.best_bid + signal.best_ask) / 2,
                            ts_ms=current_ms,
                        )
                    await self._refresh_position(symbol)

    def _reset_pressure_dwell(self, symbol: str, position_side: PositionSide, *, reason: str) -> None:
        self._clear_pressure_trigger_signature(symbol, position_side)
        if not self.signal_engine:
            return
        self.signal_engine.reset_pressure_dwell(symbol, position_side, reason=reason)

    def _reset_pressure_dwell_for_symbol(self, symbol: str, *, reason: str) -> None:
        self._reset_pressure_dwell(symbol, PositionSide.LONG, reason=reason)
        self._reset_pressure_dwell(symbol, PositionSide.SHORT, reason=reason)

    async def _refresh_position(self, symbol: str) -> None:
        """刷新单个 symbol 的仓位"""
        if not self.exchange:
            return

        positions = await self.exchange.fetch_positions(symbol)
        # 先清空，再回填：避免 fetch_positions 不返回 0 仓位导致"幽灵仓位"
        self._positions[symbol] = {}

        for pos in positions:
            leverage_override = self._symbol_leverage.get(symbol)
            if leverage_override and leverage_override > 0 and pos.leverage != leverage_override:
                pos.leverage = leverage_override
            self._positions[symbol][pos.position_side] = pos
            if pos.leverage > 0:
                self._symbol_leverage[symbol] = pos.leverage
            if abs(pos.position_amt) > Decimal("0"):
                self._clear_no_position_log(symbol, pos.position_side)

    async def _timeout_check_loop(self) -> None:
        """超时检查循环"""
        while self._running:
            try:
                await self._check_all_timeouts()
                await asyncio.sleep(0.1)  # 100ms 间隔
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"超时检查错误: {e}")
                await asyncio.sleep(1)

    async def _check_all_timeouts(self) -> None:
        """检查所有订单超时"""
        if not self.config_loader:
            return

        current_ms = current_time_ms()
        symbols = list(self._active_symbols)

        for symbol in symbols:
            engine = self.execution_engines.get(symbol)
            if not engine:
                continue

            for position_side in [PositionSide.LONG, PositionSide.SHORT]:
                await engine.check_timeout(symbol, position_side, current_ms)

            # 外部接管锁存兜底：周期性触发一次保护止损同步，用于 REST 校验释放锁存
            enabled, _verify_ms, _max_hold_ms = self._get_external_takeover_cfg_ms(symbol)
            if enabled and any(
                self._external_takeover_should_verify(symbol, side, now_ms=current_ms)
                for side in (PositionSide.LONG, PositionSide.SHORT)
            ):
                self._schedule_protective_stop_sync(symbol, reason="external_takeover_verify")

    async def _fill_rate_log_loop(self) -> None:
        """周期性输出成交率（便于观测与扩展）。"""
        if not self.config_loader:
            return

        intervals_ms: dict[tuple[str, PositionSide], int] = {}
        windows_min_map: dict[tuple[str, PositionSide], list[Decimal]] = {}
        for symbol, cfg in self._symbol_configs.items():
            if cfg.fill_rate_log_windows_min:
                windows_min = [Decimal(str(v)) for v in cfg.fill_rate_log_windows_min if Decimal(str(v)) > 0]
            else:
                windows_min = []
            if not windows_min:
                continue
            min_window_min = min(windows_min)
            interval_ms = int(
                (min_window_min * Decimal("60000") / Decimal("2")).to_integral_value(rounding=ROUND_HALF_UP)
            )
            for side in (PositionSide.LONG, PositionSide.SHORT):
                intervals_ms[(symbol, side)] = interval_ms
                windows_min_map[(symbol, side)] = windows_min

        enabled_intervals = [v for v in intervals_ms.values() if v > 0]
        if not enabled_intervals:
            return

        sleep_ms = min(enabled_intervals)

        while self._running:
            try:
                await asyncio.sleep(sleep_ms / 1000)
                if self.pause_manager.is_paused():
                    continue
                now_ms = current_time_ms()
                for (symbol, side), interval_ms in intervals_ms.items():
                    if interval_ms <= 0:
                        continue
                    last_ms = self._fill_rate_last_log_ms.get((symbol, side), 0)
                    if now_ms - last_ms < interval_ms:
                        continue
                    engine = self.execution_engines.get(symbol)
                    if not engine:
                        continue
                    position = self._positions.get(symbol, {}).get(side)
                    if not position or abs(position.position_amt) == Decimal("0"):
                        continue
                    windows_min = windows_min_map.get((symbol, side), [])
                    metrics = engine.get_fill_rate_windows(symbol, side, now_ms, windows_min)
                    for metric in metrics:
                        log_event(
                            "fill_rate",
                            symbol=symbol,
                            side=side.value,
                            window_min=metric["window_min"],
                            fill_rate=metric["fill_rate"],
                            submits=metric["submits"],
                            fills=metric["fills"],
                        )
                    self._fill_rate_last_log_ms[(symbol, side)] = now_ms
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"成交率日志循环错误: {e}")
                await asyncio.sleep(1)

    async def _pressure_stats_loop(self) -> None:
        """周期性输出 orderbook_pressure 统计指标。"""
        interval_s = 300  # 5 分钟
        while self._running:
            try:
                await asyncio.sleep(interval_s)
                if not self._running or not self._pressure_stats:
                    break
                if self.pause_manager.is_paused():
                    continue
                self._pressure_stats.log_all_windows(current_time_ms())
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"盘口量统计循环错误: {e}")
                await asyncio.sleep(1)

    async def _position_refresh_loop(self) -> None:
        """低频全量仓位刷新（B：兜底）。"""
        while self._running:
            try:
                await asyncio.sleep(self._position_refresh_interval_s)
                if not self._running:
                    break
                self._schedule_positions_refresh(
                    reason="periodic",
                    debounce_s=0.0,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"低频仓位刷新循环错误: {e}")
                await asyncio.sleep(1)

    async def shutdown(self) -> None:
        """优雅关闭"""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        logger = get_logger()
        logger.info("开始优雅关闭...")

        self._running = False
        self._shutdown_event.set()

        # 清理暂停管理器的定时任务
        self.pause_manager.cancel_all_timers()

        # 取消主循环任务
        tasks_to_cancel = [
            t
            for t in [
                self._main_loop_task,
                self._timeout_check_task,
                self._fill_rate_log_task,
                self._pressure_stats_task,
                self._position_refresh_loop_task,
                self._calibration_task,
            ]
            if t
        ]
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
        await self._gather_with_timeout(tasks_to_cancel, timeout_s=2.0, name="主循环任务取消")

        # 取消每个 symbol+side 的执行任务
        side_tasks = list(self._side_tasks)
        for task in side_tasks:
            if not task.done():
                task.cancel()
        await self._gather_with_timeout(side_tasks, timeout_s=2.0, name="SideLoop 任务取消")
        self._side_tasks.clear()

        # 取消保护止损同步任务（不撤保护止损单本身）
        ps_tasks = list(self._protective_stop_tasks.values())
        for task in ps_tasks:
            if not task.done():
                task.cancel()
        await self._gather_with_timeout(ps_tasks, timeout_s=2.0, name="保护止损任务取消")
        self._protective_stop_tasks.clear()
        self._protective_stop_task_executing.clear()
        self._protective_stop_pending_reason.clear()

        # 取消全量仓位刷新任务
        if self._positions_refresh_task and not self._positions_refresh_task.done():
            self._positions_refresh_task.cancel()
            await self._gather_with_timeout([self._positions_refresh_task], timeout_s=2.0, name="仓位刷新任务取消")
        self._positions_refresh_task = None
        self._positions_refresh_dirty = False
        self._positions_refresh_pending_reason = None

        # 撤销本程序挂单（避免误撤手动订单）
        await self._gather_with_timeout([self._cancel_own_orders(reason="shutdown")], timeout_s=8.0, name="撤销挂单")

        # 关闭 WebSocket 连接
        ws_disconnects: List[Awaitable[Any]] = []
        if self.market_ws:
            ws_disconnects.append(self.market_ws.disconnect())
        if self.user_data_ws:
            ws_disconnects.append(self.user_data_ws.disconnect())
        await self._gather_with_timeout(ws_disconnects, timeout_s=5.0, name="WebSocket 断开")

        # 取消 WebSocket 运行任务（用于中断重连 sleep 等）
        ws_tasks_to_cancel = [t for t in [self._market_ws_task, self._user_data_ws_task] if t]
        for task in ws_tasks_to_cancel:
            if not task.done():
                task.cancel()
        await self._gather_with_timeout(ws_tasks_to_cancel, timeout_s=2.0, name="WebSocket 任务取消")

        # 关闭市场数据录制器
        if self._market_recorder:
            await self._gather_with_timeout([self._market_recorder.close()], timeout_s=5.0, name="市场数据录制器关闭")
            self._market_recorder = None

        # 关闭交易所连接
        if self.exchange:
            await self._gather_with_timeout([self.exchange.close()], timeout_s=5.0, name="交易所连接关闭")

        # 关闭 Telegram Bot
        if self.telegram_bot:
            self.telegram_bot.stop()
        if self._telegram_bot_task and not self._telegram_bot_task.done():
            self._telegram_bot_task.cancel()
            await self._gather_with_timeout([self._telegram_bot_task], timeout_s=3.0, name="Telegram Bot 任务取消")
        if self.telegram_bot:
            await self._gather_with_timeout([self.telegram_bot.close()], timeout_s=3.0, name="Telegram Bot 关闭")
            self.telegram_bot = None

        # 关闭 Telegram 通知
        telegram_tasks = list(self._telegram_tasks)
        for task in telegram_tasks:
            if not task.done():
                task.cancel()
        await self._gather_with_timeout(telegram_tasks, timeout_s=2.0, name="Telegram 任务取消")
        self._telegram_tasks.clear()

        if self.telegram_notifier:
            await self._gather_with_timeout([self.telegram_notifier.close()], timeout_s=3.0, name="Telegram 关闭")
            self.telegram_notifier = None

        log_shutdown("graceful")
        logger.info("关闭完成")

    async def _cancel_own_orders(self, reason: str) -> None:
        """撤销本程序挂单（按 clientOrderId 前缀过滤）。"""
        exchange = self.exchange
        if not exchange:
            return

        logger = get_logger()
        prefix = self._client_order_id_prefix
        if not prefix:
            return

        symbols: List[str] = list(self._active_symbols)
        if not symbols and self._symbol_configs:
            symbols = list(self._symbol_configs.keys())
        if not symbols and self.config_loader:
            try:
                symbols = list(self.config_loader.get_symbols())
            except Exception:
                symbols = []

        cancelled = 0
        total_open = 0

        async def _fetch_orders(symbol: Optional[str]) -> List[Dict[str, Any]]:
            try:
                return await exchange.fetch_open_orders(symbol)
            except Exception as e:
                log_error(f"获取挂单失败: {e}", symbol=symbol)
                return []

        # Binance openOrders 不带 symbol 的权重较高：尽量按 symbol 拉取挂单。
        if symbols:
            for symbol in symbols:
                orders = await _fetch_orders(symbol)
                total_open += len(orders)
                for order in orders:
                    client_order_id = order.get("clientOrderId")
                    if not client_order_id:
                        info = order.get("info")
                        if isinstance(info, dict):
                            client_order_id = info.get("clientOrderId")
                    if not client_order_id or not str(client_order_id).startswith(prefix):
                        continue

                    order_id = order.get("id")
                    if not order_id:
                        continue

                    logger.info(
                        f"撤销本程序挂单: {symbol} {order_id} client_order_id={client_order_id} reason={reason}"
                    )
                    try:
                        await exchange.cancel_any_order(str(symbol), str(order_id))
                        cancelled += 1
                    except Exception as e:
                        log_error(
                            f"撤销挂单失败: {symbol} {order_id} - {e}",
                            symbol=str(symbol),
                            order_id=str(order_id),
                        )
        else:
            orders = await _fetch_orders(symbol=None)
            total_open = len(orders)
            for order in orders:
                client_order_id = order.get("clientOrderId")
                if not client_order_id:
                    info = order.get("info")
                    if isinstance(info, dict):
                        client_order_id = info.get("clientOrderId")
                if not client_order_id or not str(client_order_id).startswith(prefix):
                    continue

                order_id = order.get("id")
                symbol = order.get("symbol")
                if not order_id or not symbol:
                    continue

                logger.info(
                    f"撤销本程序挂单: {symbol} {order_id} client_order_id={client_order_id} reason={reason}"
                )
                try:
                    await exchange.cancel_any_order(str(symbol), str(order_id))
                    cancelled += 1
                except Exception as e:
                    log_error(
                        f"撤销挂单失败: {symbol} {order_id} - {e}",
                        symbol=str(symbol),
                        order_id=str(order_id),
                    )

        logger.info(
            f"本程序挂单清理完成: cancelled={cancelled} total_open={total_open} prefix={prefix} reason={reason}"
        )

    # ================================================================
    # Telegram Bot 命令控制
    # ================================================================

    def _register_bot_commands(self) -> None:
        """向 TelegramBot 注册所有命令处理器。"""
        bot = self.telegram_bot
        if not bot:
            return
        bot.register_handler("pause", self._handle_cmd_pause)
        bot.register_handler("resume", self._handle_cmd_resume)
        bot.register_handler("status", self._handle_cmd_status)
        bot.register_handler("help", self._handle_cmd_help)

    @staticmethod
    def _parse_duration(s: str) -> Optional[float]:
        """
        解析时长字符串为秒数。

        支持格式: <数字><单位>，单位支持 s（秒）、m（分）、h（小时）。
        示例: 10s → 10.0, 30m → 1800.0, 2h → 7200.0

        Returns:
            秒数，或 None（无法解析）
        """
        s = s.strip().lower()
        if len(s) < 2:
            return None
        unit = s[-1]
        multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
        if unit not in multipliers:
            return None
        try:
            value = float(s[:-1])
        except ValueError:
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        result = value * multipliers[unit]
        if result > 86400:  # 上限 24h
            return None
        return result

    def _resolve_symbol(self, user_input: str) -> Optional[str]:
        """
        将用户输入的 symbol 解析为内部 ccxt 格式。

        支持：BTC, BTCUSDT, BTC/USDT:USDT, btcusdt（大小写不敏感）。
        仅匹配 _active_symbols 中已有的交易对。
        base 币种匹配要求唯一命中，歧义时返回 None。

        Returns:
            ccxt 格式的 symbol，或 None（无法解析）
        """
        raw = user_input.strip().upper()
        if not raw:
            return None

        # 精确匹配（大小写不敏感）
        for sym in self._active_symbols:
            if sym.upper() == raw:
                return sym

        # 简写匹配：BTCUSDT -> BTC/USDT:USDT
        for sym in self._active_symbols:
            simple = sym.split(":")[0].replace("/", "").upper()
            if simple == raw:
                return sym

        # base 币种匹配：BTC -> BTC/USDT:USDT（要求唯一命中）
        candidates: list[str] = []
        for sym in self._active_symbols:
            base = sym.split("/")[0].upper()
            if base == raw:
                candidates.append(sym)
        if len(candidates) == 1:
            return candidates[0]

        return None

    async def _on_pause_triggered(self, symbol: Optional[str]) -> None:
        """暂停时的撤单回调。"""
        if symbol is None:
            await self._cancel_own_orders(reason="pause:global")
        else:
            await self._cancel_own_orders_for_symbol(symbol, reason=f"pause:{symbol}")

    async def _on_auto_resume(self, message: str) -> None:
        """定时恢复完成后的 Telegram 通知回调。"""
        if self.telegram_notifier and self.telegram_notifier.enabled:
            try:
                await self.telegram_notifier._send_message(message)
            except Exception as e:
                get_logger().warning(f"定时恢复 Telegram 通知失败: {e}")

    async def _cancel_own_orders_for_symbol(self, symbol: str, reason: str) -> None:
        """撤销指定 symbol 的本程序挂单（按 clientOrderId 前缀过滤）。"""
        exchange = self.exchange
        prefix = self._client_order_id_prefix
        if not exchange or not prefix:
            return

        logger = get_logger()
        try:
            orders = await exchange.fetch_open_orders(symbol)
        except Exception as e:
            log_error(f"获取挂单失败: {e}", symbol=symbol)
            return

        cancelled = 0
        for order in orders:
            client_order_id = order.get("clientOrderId")
            if not client_order_id:
                info = order.get("info")
                if isinstance(info, dict):
                    client_order_id = info.get("clientOrderId")
            if not client_order_id or not str(client_order_id).startswith(prefix):
                continue

            order_id = order.get("id")
            if not order_id:
                continue

            logger.info(
                f"撤销本程序挂单: {symbol} {order_id} client_order_id={client_order_id} reason={reason}"
            )
            try:
                await exchange.cancel_any_order(str(symbol), str(order_id))
                cancelled += 1
            except Exception as e:
                log_error(
                    f"撤销挂单失败: {symbol} {order_id} - {e}",
                    symbol=str(symbol),
                    order_id=str(order_id),
                )

        if cancelled > 0:
            log_event(
                "order_cleanup",
                event_cn="暂停撤单",
                symbol=symbol,
                reason=reason,
                cancelled=cancelled,
            )

    async def _handle_cmd_pause(self, args: str) -> str:
        """处理 /pause 命令。支持 /pause, /pause 10s, /pause BTC, /pause BTC 30m"""
        tokens = args.split() if args else []
        symbol: Optional[str] = None
        duration_s: Optional[float] = None

        if tokens:
            # 尝试解析最后一个 token 为时长
            last_dur = self._parse_duration(tokens[-1])
            if last_dur is not None:
                duration_s = last_dur
                tokens = tokens[:-1]

            # 剩余 tokens 拼接为 symbol
            if tokens:
                raw_symbol = " ".join(tokens)
                symbol = self._resolve_symbol(raw_symbol)
                if not symbol:
                    active_list = ", ".join(
                        s.split(":")[0].replace("/", "") for s in sorted(self._active_symbols)
                    )
                    return f"未知交易对: {raw_symbol}\n当前交易对: {active_list or '无'}"

        return await self.pause_manager.pause(symbol, duration_s=duration_s)

    async def _handle_cmd_resume(self, args: str) -> str:
        """处理 /resume 命令。"""
        if args:
            symbol = self._resolve_symbol(args)
            if not symbol:
                active_list = ", ".join(
                    s.split(":")[0].replace("/", "") for s in sorted(self._active_symbols)
                )
                return f"未知交易对: {args}\n当前交易对: {active_list or '无'}"
            return await self.pause_manager.resume(symbol)
        return await self.pause_manager.resume()

    async def _handle_cmd_status(self, args: str) -> str:
        """处理 /status 命令。"""
        _ = args
        lines: list[str] = ["【状态】"]

        # 运行状态
        now = datetime.now()

        def _elapsed(at: Optional[datetime]) -> str:
            if not at:
                return ""
            secs = max(0, int((now - at).total_seconds()))
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"

        uptime = _elapsed(self._started_at)
        lines.append(f"运行中: {'是' if self._running else '否'}" + (f" ({uptime})" if uptime else ""))
        lines.append(f"校准中: {'是' if self._calibrating else '否'}")

        # 暂停状态
        pause_status = self.pause_manager.get_status()

        if pause_status["global_paused"]:
            at = pause_status["global_paused_at"]
            ts = at.strftime("%H:%M:%S") if at else "?"
            elapsed = _elapsed(at)
            resume_at = pause_status.get("global_resume_at")
            if resume_at:
                remaining = max(0, (resume_at - now).total_seconds())
                m, s = divmod(int(remaining), 60)
                h, m = divmod(m, 60)
                remain_str = f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
                lines.append(f"暂停: 全局 (自 {ts}, 已暂停 {elapsed}, 剩余 {remain_str})")
                if remaining > 60:
                    lines.append("⚠️ 暂停期间不执行强平兜底，保护性止损仍在交易所端生效")
            else:
                lines.append(f"暂停: 全局 (自 {ts}, 已暂停 {elapsed})")
                lines.append("⚠️ 暂停期间不执行强平兜底，保护性止损仍在交易所端生效")
        elif pause_status["paused_symbols"]:
            symbol_resume = pause_status.get("symbol_resume_at", {})
            for sym, at in pause_status["paused_symbols"].items():
                short = sym.split(":")[0]
                ts = at.strftime("%H:%M:%S") if at else "?"
                elapsed = _elapsed(at)
                resume_at = symbol_resume.get(sym)
                if resume_at:
                    remaining = max(0, (resume_at - now).total_seconds())
                    m, s = divmod(int(remaining), 60)
                    h, m = divmod(m, 60)
                    remain_str = f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
                    lines.append(f"暂停: {short} (自 {ts}, 已暂停 {elapsed}, 剩余 {remain_str})")
                else:
                    lines.append(f"暂停: {short} (自 {ts}, 已暂停 {elapsed})")
        else:
            lines.append("暂停: 无")

        # 活跃 symbols + 持仓
        if self._active_symbols:
            lines.append(f"\n活跃交易对: {len(self._active_symbols)}")
            for sym in sorted(self._active_symbols):
                short = sym.split(":")[0]
                positions = self._positions.get(sym, {})
                parts: list[str] = []
                for ps in (PositionSide.LONG, PositionSide.SHORT):
                    pos = positions.get(ps)
                    if pos and abs(pos.position_amt) > Decimal("0"):
                        engine = self.execution_engines.get(sym)
                        state = engine.get_state(sym, ps) if engine else None
                        mode = state.mode.value if state else "?"
                        st = state.state.value if state else "?"
                        parts.append(f"{ps.value}={format_decimal(pos.position_amt)} [{mode}/{st}]")
                if parts:
                    lines.append(f"  {short}: {', '.join(parts)}")
                else:
                    lines.append(f"  {short}: 无仓位")
        else:
            lines.append("\n活跃交易对: 0")

        return "\n".join(lines)

    async def _handle_cmd_help(self, args: str) -> str:
        """处理 /help 命令。"""
        _ = args
        return (
            "【命令列表】\n"
            "/pause - 全局暂停（撤所有挂单）\n"
            "/pause <时长> - 全局定时暂停（如 10s, 30m, 2h）\n"
            "/pause <SYMBOL> - 暂停指定交易对\n"
            "/pause <SYMBOL> <时长> - 定时暂停指定交易对\n"
            "/resume - 全局恢复\n"
            "/resume <SYMBOL> - 恢复指定交易对\n"
            "/status - 查看运行状态\n"
            "/help - 显示此帮助\n"
            "\n"
            "SYMBOL 支持: BTC / BTCUSDT / BTC/USDT:USDT（大小写不敏感）\n"
            "时长格式: <数字><单位>，如 10s / 30m / 2h"
        )

    def request_shutdown(self) -> None:
        """请求关闭"""
        self._shutdown_event.set()


async def main(config_path: Path) -> None:
    """
    主函数

    Args:
        config_path: 配置文件路径
    """
    app = Application(config_path)

    # 设置信号处理器
    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        get_logger().info("收到关闭信号")
        app.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await app.initialize()
        await app.run()
    except Exception as e:
        log_error(f"应用错误: {e}")
        raise


if __name__ == "__main__":
    config_file = Path("config/config.yaml")
    if len(sys.argv) > 1:
        config_file = Path(sys.argv[1])

    asyncio.run(main(config_file))
