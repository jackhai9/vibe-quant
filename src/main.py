# Input: config path, env vars, OS signals
# Output: application lifecycle and async tasks
# Pos: application entrypoint
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
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
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
from src.signal.engine import SignalEngine
from src.execution.engine import ExecutionEngine
from src.risk.manager import RiskManager
from src.risk.protective_stop import ProtectiveStopManager
from src.notify.telegram import TelegramNotifier
from src.models import (
    MarketEvent,
    OrderUpdate,
    AlgoOrderUpdate,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Position,
    PositionUpdate,
    LeverageUpdate,
    PositionSide,
    SymbolRules,
    ExecutionState,
    ExecutionMode,
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
from src.utils.helpers import current_time_ms, format_decimal


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
        self._protective_stop_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._protective_stop_task_reasons: Dict[str, str] = {}

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._positions: Dict[str, Dict[PositionSide, Position]] = {}  # symbol -> side -> Position
        self._symbol_leverage: Dict[str, int] = {}  # symbol -> leverage
        self._rules: Dict[str, SymbolRules] = {}  # symbol -> rules
        self._symbol_configs: Dict[str, MergedSymbolConfig] = {}  # symbol -> config

        # 主循环任务
        self._main_loop_task: Optional[asyncio.Task[None]] = None
        self._timeout_check_task: Optional[asyncio.Task[None]] = None
        self._market_ws_task: Optional[asyncio.Task[None]] = None
        self._user_data_ws_task: Optional[asyncio.Task[None]] = None
        self._calibration_task: Optional[asyncio.Task[None]] = None
        self._calibration_lock = asyncio.Lock()
        self._calibrating = False

        self._shutdown_started = False

        # 订单归属标记：用于“本次运行”隔离撤单范围（避免误撤手动/其他实例订单）
        self._run_id: Optional[str] = None
        self._client_order_id_prefix: Optional[str] = None

        # 仓位更新同步（用于 Telegram 仓位 before->after 以及开仓告警）
        self._positions_ready = False
        self._position_update_events: dict[tuple[str, PositionSide], asyncio.Event] = {}
        self._position_revision: dict[tuple[str, PositionSide], int] = {}
        self._position_last_change: dict[tuple[str, PositionSide], tuple[Decimal, Decimal]] = {}
        self._no_position_logged: set[tuple[str, PositionSide]] = set()
        self._panic_last_tier: dict[tuple[str, PositionSide], Decimal] = {}
        self._external_takeover: dict[tuple[str, PositionSide], ExternalTakeoverState] = {}

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

        说明：同侧可能同时存在多个外部 stop/tp；WS 收到某一张终态不代表“外部接管结束”。
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
                "protective_stop",
                event_cn="保护止损",
                symbol=symbol,
                side=position_side.value,
                reason="external_takeover_set",
                source=source,
                order_id=order_id,
                client_order_id=client_order_id,
                order_type=order_type,
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
                "protective_stop",
                event_cn="保护止损",
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
        （其中 `client_order_prefix` 为常量 `CLIENT_ORDER_PREFIX`），用于退出时“只清理本次运行挂单”。
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
        self._protective_stop_tasks.pop(symbol, None)
        self._protective_stop_task_reasons.pop(symbol, None)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_error(f"保护止损任务状态异常: {name} - {e}", symbol=symbol)
            return

        if exc:
            log_error(f"保护止损任务异常: {name} - {exc}", symbol=symbol)

    def _schedule_protective_stop_sync(self, symbol: str, reason: str) -> None:
        """异步调度保护止损同步（合并短时间内的多次触发）。"""
        if not self._running:
            return
        if not self.exchange or not self.protective_stop_manager:
            return

        debounce_s = self._protective_stop_debounce_s(reason)

        prev = self._protective_stop_tasks.get(symbol)
        prev_reason = self._protective_stop_task_reasons.get(symbol)
        # verify 任务只需要“至少跑一次”，若已经有 verify 在跑/排队就不重复调度（避免刷屏）
        if reason == "external_takeover_verify" and prev and not prev.done() and prev_reason == "external_takeover_verify":
            return

        if prev and not prev.done():
            prev.cancel()

        async def _runner() -> None:
            try:
                # debounce：合并短时间内的多次触发（按 reason 分级）
                if debounce_s > 0:
                    await asyncio.sleep(debounce_s)
                await self._sync_protective_stop(symbol=symbol, reason=reason)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log_error(f"保护止损同步失败: {e}", symbol=symbol)

        task = asyncio.create_task(_runner())
        self._protective_stop_tasks[symbol] = task
        self._protective_stop_task_reasons[symbol] = reason
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
                                "protective_stop",
                                event_cn="保护止损",
                                symbol=symbol,
                                side=side.value,
                                reason="external_takeover_verify",
                                external_present=present,
                            )
        except Exception as e:
            log_error(f"保护止损同步异常: {e}", symbol=symbol, reason=reason)
            return

        if needs_resync:
            await self._sync_protective_stop(symbol=symbol, reason="external_takeover_release")

    async def _sync_protective_stops_all(self, *, reason: str) -> None:
        if not self.config_loader:
            return
        for symbol in self.config_loader.get_symbols():
            await self._sync_protective_stop(symbol=symbol, reason=reason)

    def _on_engine_fill(
        self,
        symbol: str,
        position_side: PositionSide,
        mode: ExecutionMode,
        filled_qty: Decimal,
        avg_price: Decimal,
        reason: str,
    ) -> None:
        """ExecutionEngine 成交回调：用于触发 Telegram 通知（不得阻塞）。"""
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
            ),
            name=f"fill:{symbol}:{position_side.value}",
        )

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
        撤销“本次运行”在该 symbol+side 的遗留挂单。

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
                await exchange.cancel_order(str(symbol), str(order_id))
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
        symbols = self.config_loader.get_symbols()

        if not symbols:
            raise ValueError("配置中没有定义任何 symbol")

        # 设置日志
        # 支持通过环境变量指定日志目录（便于 systemd/Docker 持久化日志）
        log_dir = Path(os.environ.get("VQ_LOG_DIR", "logs"))
        setup_logger(log_dir, level="INFO", file_level="DEBUG", console=True)
        log_startup(symbols)

        logger.info(f"配置加载完成，symbols: {symbols}")

        # 预加载每个 symbol 的合并配置
        for symbol in symbols:
            self._symbol_configs[symbol] = self.config_loader.get_symbol_config(symbol)

        # 2. 初始化交易所适配器
        logger.info("初始化交易所适配器...")
        global_config = self.config_loader.config.global_

        # Telegram 通知（可选）
        telegram_cfg = global_config.telegram
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
        )

        # 加载交易规则
        await self.exchange.load_markets()

        for symbol in symbols:
            rules = self.exchange.get_rules(symbol)
            if rules:
                self._rules[symbol] = rules
                logger.info(f"{symbol} 规则: tick={rules.tick_size}, step={rules.step_size}, min_qty={rules.min_qty}")
            else:
                logger.warning(f"{symbol} 未找到交易规则")

        # 3. 初始化信号引擎
        logger.info("初始化信号引擎...")
        self.signal_engine = SignalEngine(
            min_signal_interval_ms=global_config.execution.min_signal_interval_ms,
        )
        for symbol in symbols:
            cfg = self._symbol_configs[symbol]
            self.signal_engine.configure_symbol(
                symbol,
                min_signal_interval_ms=cfg.min_signal_interval_ms,
                accel_window_ms=cfg.accel_window_ms,
                accel_tiers=[(t.ret, t.mult) for t in cfg.accel_tiers],
                roi_tiers=[(t.roi, t.mult) for t in cfg.roi_tiers],
            )

        # 4. 初始化执行引擎（每个 symbol 一个）
        logger.info("初始化执行引擎...")
        for symbol in symbols:
            config = self._symbol_configs[symbol]
            self.execution_engines[symbol] = ExecutionEngine(
                place_order=self._place_order,
                cancel_order=self._cancel_order,
                on_fill=self._on_engine_fill,
                order_ttl_ms=config.order_ttl_ms,
                repost_cooldown_ms=config.repost_cooldown_ms,
                base_lot_mult=config.base_lot_mult,
                maker_price_mode=config.maker_price_mode,
                maker_n_ticks=config.maker_n_ticks,
                maker_safety_ticks=config.maker_safety_ticks,
                maker_timeouts_to_escalate=config.maker_timeouts_to_escalate,
                aggr_fills_to_deescalate=config.aggr_fills_to_deescalate,
                aggr_timeouts_to_deescalate=config.aggr_timeouts_to_deescalate,
                max_mult=config.max_mult,
                max_order_notional=config.max_order_notional,
            )

        # 5. 初始化 WebSocket 客户端
        logger.info("初始化 WebSocket 客户端...")
        ws_config = global_config.ws

        self.market_ws = MarketWSClient(
            symbols=symbols,
            on_event=self._on_market_event,
            on_reconnect=self._on_ws_reconnect,
            initial_delay_ms=ws_config.reconnect.initial_delay_ms,
            max_delay_ms=ws_config.reconnect.max_delay_ms,
            multiplier=ws_config.reconnect.multiplier,
            stale_data_ms=ws_config.stale_data_ms,
        )

        self.user_data_ws = UserDataWSClient(
            api_key=self.config_loader.api_key,
            api_secret=self.config_loader.api_secret,
            on_order_update=self._on_order_update,
            on_algo_order_update=self._on_algo_order_update,
            on_position_update=self._on_position_update,
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

        return await self.exchange.place_order(intent)

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
        if not self.signal_engine:
            return

        # 更新信号引擎的市场状态
        self.signal_engine.update_market(event)

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

                symbols = self.config_loader.get_symbols()
                for symbol in symbols:
                    rules = self.exchange.get_rules(symbol)
                    if rules:
                        self._rules[symbol] = rules

                await self._fetch_positions()
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
        cleared: bool = True,
    ) -> None:
        key = (symbol, position_side)
        if key in self._no_position_logged:
            return
        self._no_position_logged.add(key)
        side = position_side.value
        logger = get_logger()
        if cleared:
            logger.info(f"{symbol} {side} ✅ 仓位已全部平掉")
        logger.info(f"{symbol} {side} ⏳ 当前无持仓，等待开仓...")

    def _clear_no_position_log(self, symbol: str, position_side: PositionSide) -> None:
        self._no_position_logged.discard((symbol, position_side))

    def _on_position_update(self, update: PositionUpdate) -> None:
        """处理仓位更新回调（ACCOUNT_UPDATE）。"""
        if not self._running:
            return

        # 只跟踪配置中已启用的 symbols
        if update.symbol not in self.execution_engines:
            return

        symbol_positions = self._positions.setdefault(update.symbol, {})
        prev = symbol_positions.get(update.position_side)
        prev_amt = prev.position_amt if prev else Decimal("0")

        if prev_amt != update.position_amt:
            key = (update.symbol, update.position_side)
            self._position_revision[key] = self._position_revision.get(key, 0) + 1
            self._position_last_change[key] = (prev_amt, update.position_amt)
            self._position_update_events.setdefault(key, asyncio.Event()).set()

        # 0 仓位：删除缓存，避免“幽灵仓位”
        if abs(update.position_amt) == Decimal("0"):
            symbol_positions.pop(update.position_side, None)
            if abs(prev_amt) > Decimal("0"):
                log_position_update(
                    symbol=update.symbol,
                    side=update.position_side.value,
                    position_amt=Decimal("0"),
                )
                self._log_no_position(update.symbol, update.position_side, cleared=True)
                # 仓位归零：尽快撤销该侧遗留挂单，避免后续触发导致反向开仓
                asyncio.create_task(
                    self._cancel_run_prefix_orders_for_side(
                        symbol=update.symbol,
                        position_side=update.position_side,
                        reason="position_zero",
                    )
                )
            self._schedule_protective_stop_sync(update.symbol, reason=f"position_update:{update.position_side.value}")
            return

        self._clear_no_position_log(update.symbol, update.position_side)

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
                            symbol=update.symbol,
                            side=update.position_side.value,
                            position_before=str(abs(prev_amt)),
                            position_after=str(abs(update.position_amt)),
                        ),
                        name=f"open_alert:{update.symbol}:{update.position_side.value}",
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
            symbol=update.symbol,
            position_side=update.position_side,
            position_amt=update.position_amt,
            entry_price=entry_price,
            unrealized_pnl=unrealized_pnl,
            leverage=prev.leverage if prev else self._symbol_leverage.get(update.symbol, 1),
            mark_price=prev.mark_price if prev else None,
            liquidation_price=prev.liquidation_price if prev else None,
        )

        symbol_positions[update.position_side] = merged
        if prev_amt != update.position_amt:
            log_position_update(
                symbol=update.symbol,
                side=update.position_side.value,
                position_amt=update.position_amt,
            )
            self._schedule_protective_stop_sync(update.symbol, reason=f"position_update:{update.position_side.value}")

    def _on_leverage_update(self, update: LeverageUpdate) -> None:
        """处理杠杆更新回调（ACCOUNT_CONFIG_UPDATE）。"""
        if not self._running:
            return

        # 只跟踪配置中已启用的 symbols
        if update.symbol not in self.execution_engines:
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
            "leverage_update",
            symbol=update.symbol,
            reason="ws_account_config_update",
            leverage=update.leverage,
        )

    async def _handle_order_update(self, update: OrderUpdate) -> None:
        """异步处理订单更新"""
        engine = self.execution_engines.get(update.symbol)
        if engine:
            await engine.on_order_update(update, current_time_ms())
        if self.protective_stop_manager:
            await self.protective_stop_manager.on_order_update(update)
            if update.client_order_id.startswith(PROTECTIVE_STOP_PREFIX):
                self._schedule_protective_stop_sync(update.symbol, reason=f"order_update:{update.status.value}")
            elif update.order_type and update.order_type.upper() in _STOP_ORDER_TYPES and (
                update.close_position is True or update.reduce_only is True
            ):
                # 外部 closePosition 或 reduceOnly 条件单状态变化也可能导致“外部接管/释放”
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

    async def _fetch_positions(self) -> None:
        """获取所有仓位"""
        if not self.exchange or not self.config_loader:
            return

        symbols = self.config_loader.get_symbols()
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

        for symbol in symbols:
            leverage_override = leverage_map.get(symbol)
            if leverage_override and leverage_override > 0:
                self._symbol_leverage[symbol] = leverage_override
            positions = await self.exchange.fetch_positions(symbol)
            # 先清空，再回填：避免 fetch_positions 不返回 0 仓位导致“幽灵仓位”
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
                    log_position_update(
                        symbol=symbol,
                        side=pos.position_side.value,
                        position_amt=pos.position_amt,
                    )

    def _log_startup_no_positions(self) -> None:
        if not self.config_loader:
            return

        for symbol in self.config_loader.get_symbols():
            for position_side in (PositionSide.LONG, PositionSide.SHORT):
                position = self._positions.get(symbol, {}).get(position_side)
                if position and abs(position.position_amt) > Decimal("0"):
                    continue
                self._log_no_position(symbol, position_side, cleared=False)

    async def run(self) -> None:
        """运行应用"""
        logger = get_logger()
        self._running = True

        try:
            # 获取初始仓位
            logger.info("获取初始仓位...")
            await self._fetch_positions()
            self._log_startup_no_positions()
            self._positions_ready = True
            await self._sync_protective_stops_all(reason="startup")

            # 连接 WebSocket
            logger.info("连接 WebSocket...")
            if self.market_ws:
                self._market_ws_task = asyncio.create_task(self.market_ws.connect())
                self._market_ws_task.add_done_callback(
                    lambda t, n="market_ws.connect": self._on_background_task_done(t, n)
                )
            if self.user_data_ws:
                self._user_data_ws_task = asyncio.create_task(self.user_data_ws.connect())
                self._user_data_ws_task.add_done_callback(
                    lambda t, n="user_data_ws.connect": self._on_background_task_done(t, n)
                )

            if self._shutdown_event.is_set():
                return

            # 等待数据就绪
            logger.info("等待数据就绪...")
            await asyncio.sleep(2)

            # 启动主循环任务
            self._main_loop_task = asyncio.create_task(self._main_loop())
            self._timeout_check_task = asyncio.create_task(self._timeout_check_loop())

            # 等待关闭信号
            await self._shutdown_event.wait()

        except Exception as e:
            log_error(str(e))
            raise
        finally:
            await self.shutdown()

    async def _main_loop(self) -> None:
        """主事件循环（多 symbol 并发）：为每个 symbol+side 启动独立任务。"""
        if not self.config_loader:
            return

        symbols = self.config_loader.get_symbols()
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
        symbols = self.config_loader.get_symbols()

        for symbol in symbols:
            # 检查数据是否就绪
            if not self.signal_engine.is_data_ready(symbol):
                continue

            # 检查是否有陈旧数据
            if self.market_ws and self.market_ws.is_stale(symbol):
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

        position = self._positions.get(symbol, {}).get(position_side)
        if not position or abs(position.position_amt) == Decimal("0"):
            return

        if self.market_ws and self.market_ws.is_stale(symbol):
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
                    "risk_trigger",
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
            return

        # 检查冷却期
        engine.check_cooldown(symbol, position_side, current_ms)

        # 评估信号
        signal = self.signal_engine.evaluate(  # type: ignore[union-attr]
            symbol=symbol,
            position_side=position_side,
            position=position,
            current_ms=current_ms,
        )

        if signal:
            # 风险兜底：接近强平时强制更激进的执行模式（优先级高于普通 maker）
            if self.risk_manager:
                risk_flag = self.risk_manager.check_risk(position)
                if risk_flag.is_triggered and risk_flag.dist_to_liq is not None:
                    target_mode = ExecutionMode.AGGRESSIVE_LIMIT

                    state = engine.get_state(symbol, position_side)
                    if state.mode != target_mode:
                        engine.set_mode(symbol, position_side, target_mode, reason="risk_trigger")
                        dist_display = format_decimal(risk_flag.dist_to_liq, precision=4)
                        risk_stage = risk_flag.reason or "liq_distance_breach"
                        risk_levels = self.config_loader.config.global_.risk.levels if self.config_loader else {}
                        log_event(
                            "risk_trigger",
                            symbol=symbol,
                            side=position_side.value,
                            mode=target_mode.value,
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
                await engine.on_order_placed(
                    intent=intent,
                    result=result,
                    current_ms=current_time_ms(),
                )

                # 更新仓位
                if result.success:
                    await self._refresh_position(symbol)

    async def _refresh_position(self, symbol: str) -> None:
        """刷新单个 symbol 的仓位"""
        if not self.exchange:
            return

        positions = await self.exchange.fetch_positions(symbol)
        # 先清空，再回填：避免 fetch_positions 不返回 0 仓位导致“幽灵仓位”
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
        symbols = self.config_loader.get_symbols()

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

    async def shutdown(self) -> None:
        """优雅关闭"""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        logger = get_logger()
        logger.info("开始优雅关闭...")

        self._running = False
        self._shutdown_event.set()

        # 取消主循环任务
        tasks_to_cancel = [t for t in [self._main_loop_task, self._timeout_check_task, self._calibration_task] if t]
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

        # 关闭交易所连接
        if self.exchange:
            await self._gather_with_timeout([self.exchange.close()], timeout_s=5.0, name="交易所连接关闭")

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

        symbols: List[str] = []
        if self.config_loader:
            try:
                symbols = list(self.config_loader.get_symbols())
            except Exception:
                symbols = []
        if not symbols and self._symbol_configs:
            symbols = list(self._symbol_configs.keys())

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
                        await exchange.cancel_order(str(symbol), str(order_id))
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
                    await exchange.cancel_order(str(symbol), str(order_id))
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
