# Input: MarketEvent, Position, config
# Output: ExitSignal and per-symbol runtime gating for orderbook_price/orderbook_pressure (including semantic signal-log denoise, pressure timing jitter, and active burst pacing)
# Pos: signal evaluation engine with orderbook_price/orderbook_pressure log heartbeat, pressure dwell/freshness, TTL/cooldown jitter, and active burst pacing
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
信号引擎模块

职责：
- 维护每个 symbol 的 MarketState（best_bid/ask, last/prev trade price）
- 评估 LONG/SHORT 平仓触发条件
- 实现触发节流（min_signal_interval_ms）
- 维护滑动窗口回报率（accel）并计算 accel_mult
- 计算 ROI 并匹配 roi_mult
- 为 orderbook_pressure 维护 depth/book freshness 与 dwell 状态

输入：
- MarketEvent
- Position

输出：
- ExitSignal（满足条件时）
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from random import randint
from typing import Deque, Dict, Optional, Tuple, List

from src.models import (
    MarketEvent,
    MarketState,
    Position,
    PositionSide,
    ExitSignal,
    SignalReason,
    StrategyMode,
    SignalExecutionPreference,
    QtyPolicy,
)
from src.utils.logger import get_logger, log_event, log_signal
from src.utils.helpers import current_time_ms

SIGNAL_LOG_HEARTBEAT_MS = 5000


@dataclass
class PressureSignalConfig:
    threshold_qty: Decimal
    sustain_ms: int
    passive_level: int
    lot_mult: int
    active_recheck_cooldown_ms: int
    passive_ttl_ms: int
    active_recheck_cooldown_jitter_pct: Decimal = Decimal("0.15")
    passive_ttl_jitter_pct: Decimal = Decimal("0.15")
    active_burst_window_ms: int = 10000
    active_burst_max_attempts: int = 8
    active_burst_max_fills: int = 5
    active_burst_pause_min_ms: int = 2500
    active_burst_pause_max_ms: int = 6000
    qty_jitter_pct: Decimal = Decimal("0.15")
    qty_anti_repeat_lookback: int = 3


class SignalEngine:
    """信号引擎"""

    def __init__(self, min_signal_interval_ms: int = 200):
        """
        初始化信号引擎

        Args:
            min_signal_interval_ms: 同一侧仓位两次信号的最小间隔
        """
        self.min_signal_interval_ms = min_signal_interval_ms
        self._market_states: Dict[str, MarketState] = {}
        self._last_signal_ms: Dict[str, int] = {}  # key: symbol:position_side
        self._last_logged_signal: Dict[str, Tuple[Tuple[object, ...], int]] = {}  # key: symbol:position_side

        # 追踪是否收到过 bid/ask 和 trade 数据
        self._has_book_data: Dict[str, bool] = {}
        self._has_trade_data: Dict[str, bool] = {}
        self._has_depth_data: Dict[str, bool] = {}

        # per-symbol 参数（允许覆盖）
        self._symbol_strategy_modes: Dict[str, StrategyMode] = {}
        self._symbol_pressure_configs: Dict[str, PressureSignalConfig] = {}
        self._symbol_min_signal_interval_ms: Dict[str, int] = {}
        self._symbol_accel_window_ms: Dict[str, int] = {}
        self._symbol_accel_tiers: Dict[str, List[Tuple[Decimal, int]]] = {}
        self._symbol_roi_tiers: Dict[str, List[Tuple[Decimal, int]]] = {}

        # trade 价格序列（用于 accel 滑动窗口）
        self._trade_history: Dict[str, Deque[Tuple[int, Decimal]]] = {}
        self._pressure_dwell_start_ms: Dict[str, int] = {}
        self._last_pressure_skip_reason: Dict[str, str] = {}
        self._pressure_active_attempt_history_ms: Dict[str, Deque[int]] = {}
        self._pressure_active_fill_history_ms: Dict[str, Deque[int]] = {}
        self._pressure_active_pause_until_ms: Dict[str, int] = {}

    def configure_symbol(
        self,
        symbol: str,
        *,
        strategy_mode: StrategyMode = StrategyMode.ORDERBOOK_PRICE,
        pressure_config: Optional[PressureSignalConfig] = None,
        min_signal_interval_ms: Optional[int] = None,
        accel_window_ms: Optional[int] = None,
        accel_tiers: Optional[List[Tuple[Decimal, int]]] = None,
        roi_tiers: Optional[List[Tuple[Decimal, int]]] = None,
    ) -> None:
        """配置某个 symbol 的节流/倍数档位参数。"""
        self._symbol_strategy_modes[symbol] = strategy_mode
        if pressure_config is not None:
            self._symbol_pressure_configs[symbol] = pressure_config
        elif symbol in self._symbol_pressure_configs:
            del self._symbol_pressure_configs[symbol]
        if min_signal_interval_ms is not None:
            self._symbol_min_signal_interval_ms[symbol] = min_signal_interval_ms
        if accel_window_ms is not None:
            self._symbol_accel_window_ms[symbol] = accel_window_ms
        if accel_tiers is not None:
            self._symbol_accel_tiers[symbol] = sorted(accel_tiers, key=lambda x: x[0])
        if roi_tiers is not None:
            self._symbol_roi_tiers[symbol] = sorted(roi_tiers, key=lambda x: x[0])

        self._trade_history.setdefault(symbol, deque())

    def update_market(self, event: MarketEvent) -> None:
        """
        更新市场状态

        根据事件类型更新：
        - book_ticker: 更新 best_bid/best_ask
        - agg_trade: 更新 last_trade_price (previous <- last)

        Args:
            event: 市场数据事件
        """
        symbol = event.symbol
        state = self._market_states.get(symbol)

        if state is None:
            # 初始化 MarketState
            state = MarketState(
                symbol=symbol,
                best_bid=Decimal("0"),
                best_ask=Decimal("0"),
                last_trade_price=Decimal("0"),
                best_bid_qty=Decimal("0"),
                best_ask_qty=Decimal("0"),
                previous_trade_price=None,
                last_update_ms=0,
                is_ready=False,
            )
            self._market_states[symbol] = state
            self._has_book_data[symbol] = False
            self._has_trade_data[symbol] = False
            self._has_depth_data[symbol] = False

        # 更新时间戳
        state.last_update_ms = event.timestamp_ms

        if event.event_type == "book_ticker":
            # 更新 best bid/ask
            if event.best_bid is not None:
                state.best_bid = event.best_bid
            if event.best_ask is not None:
                state.best_ask = event.best_ask
            if event.best_bid_qty is not None:
                state.best_bid_qty = event.best_bid_qty
            if event.best_ask_qty is not None:
                state.best_ask_qty = event.best_ask_qty
            state.last_book_ticker_ms = event.timestamp_ms
            self._has_book_data[symbol] = True
        elif event.event_type == "depth":
            if event.bid_levels is not None:
                state.bid_levels = list(event.bid_levels)
            if event.ask_levels is not None:
                state.ask_levels = list(event.ask_levels)
            state.last_depth_update_ms = event.timestamp_ms
            self._has_depth_data[symbol] = True

        elif event.event_type == "agg_trade":
            # 更新 trade price，保存上一次价格
            if event.last_trade_price is not None:
                # 维护 trade 历史（用于 accel 滑动窗口）
                history = self._trade_history.setdefault(symbol, deque())
                history.append((event.timestamp_ms, event.last_trade_price))

                # 只有当 last_trade_price 已有有效值时才保存到 previous
                if state.last_trade_price > Decimal("0"):
                    state.previous_trade_price = state.last_trade_price
                state.last_trade_price = event.last_trade_price
                state.last_trade_update_ms = event.timestamp_ms
                self._has_trade_data[symbol] = True

        state.is_ready = self._is_symbol_ready(symbol, state)

    def evaluate(
        self,
        symbol: str,
        position_side: PositionSide,
        position: Position,
        current_ms: Optional[int] = None,
    ) -> Optional[ExitSignal]:
        """
        评估是否满足平仓条件

        Args:
            symbol: 交易对
            position_side: 仓位方向
            position: 当前仓位
            current_ms: 当前时间戳（可选，默认使用系统时间）

        Returns:
            ExitSignal（满足条件时）或 None
        """
        if current_ms is None:
            current_ms = current_time_ms()

        state = self._market_states.get(symbol)
        if state is None or not state.is_ready:
            return None

        # 检查仓位是否有效（非零）
        if abs(position.position_amt) == Decimal("0"):
            self.reset_pressure_dwell(symbol, position_side, reason="position_flat")
            self._clear_pressure_skip_reason(f"{symbol}:{position_side.value}")
            self._clear_pressure_skip_reason(f"{symbol}:freshness")
            return None

        # 检查节流
        if self._is_throttled(symbol, position_side, current_ms):
            return None

        strategy_mode = self._symbol_strategy_modes.get(symbol, StrategyMode.ORDERBOOK_PRICE)
        if strategy_mode == StrategyMode.ORDERBOOK_PRESSURE:
            return self._evaluate_orderbook_pressure(
                symbol=symbol,
                position_side=position_side,
                position=position,
                state=state,
                current_ms=current_ms,
            )

        reason: Optional[SignalReason] = None

        if position_side == PositionSide.LONG:
            reason = self._check_long_exit(state)
        elif position_side == PositionSide.SHORT:
            reason = self._check_short_exit(state)

        if reason is None:
            return None

        ret_window = self._compute_accel_ret(symbol, current_ms, state.last_trade_price)
        accel_mult = self._select_accel_mult(symbol, position_side, ret_window)

        roi = self._compute_roi(position)
        roi_mult = self._select_roi_mult(symbol, roi)

        # 更新最后信号时间
        key = f"{symbol}:{position_side.value}"
        self._last_signal_ms[key] = current_ms

        # 创建 ExitSignal
        signal = ExitSignal(
            symbol=symbol,
            position_side=position_side,
            reason=reason,
            timestamp_ms=current_ms,
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            last_trade_price=state.last_trade_price,
            roi_mult=roi_mult,
            accel_mult=accel_mult,
            roi=roi,
            ret_window=ret_window,
        )

        self._log_signal_if_changed(key, signal)

        return signal

    def _evaluate_orderbook_pressure(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        position: Position,
        state: MarketState,
        current_ms: int,
    ) -> Optional[ExitSignal]:
        cfg = self._symbol_pressure_configs.get(symbol)
        if cfg is None:
            return None

        dwell_key = f"{symbol}:{position_side.value}"
        active_paused = False
        active_qty = state.best_bid_qty if position_side == PositionSide.LONG else state.best_ask_qty
        if active_qty > cfg.threshold_qty:
            start_ms = self._pressure_dwell_start_ms.get(dwell_key)
            if start_ms is None:
                self._pressure_dwell_start_ms[dwell_key] = current_ms
                return None
            if current_ms - start_ms < cfg.sustain_ms:
                return None

            if self._is_pressure_active_paused(
                key=dwell_key,
                symbol=symbol,
                position_side=position_side,
                current_ms=current_ms,
            ):
                active_paused = True
            else:
                reason = (
                    SignalReason.LONG_BID_PRESSURE_ACTIVE
                    if position_side == PositionSide.LONG
                    else SignalReason.SHORT_ASK_PRESSURE_ACTIVE
                )
                self._clear_pressure_skip_reason(dwell_key)
                self._last_signal_ms[dwell_key] = current_ms
                signal = ExitSignal(
                    symbol=symbol,
                    position_side=position_side,
                    reason=reason,
                    timestamp_ms=current_ms,
                    best_bid=state.best_bid,
                    best_ask=state.best_ask,
                    last_trade_price=state.last_trade_price if state.last_trade_price > Decimal("0")
                    else (state.best_bid if position_side == PositionSide.LONG else state.best_ask),
                    strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
                    execution_preference=SignalExecutionPreference.AGGRESSIVE,
                    qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
                    price_override=state.best_bid if position_side == PositionSide.LONG else state.best_ask,
                    ttl_override_ms=None,
                    cooldown_override_ms=self._jitter_duration_ms(
                        cfg.active_recheck_cooldown_ms,
                        cfg.active_recheck_cooldown_jitter_pct,
                        minimum_ms=0,
                    ),
                    fixed_lot_mult=cfg.lot_mult,
                    fixed_qty_jitter_pct=cfg.qty_jitter_pct,
                    fixed_qty_anti_repeat_lookback=cfg.qty_anti_repeat_lookback,
                    active_burst_window_ms=cfg.active_burst_window_ms,
                    active_burst_max_attempts=cfg.active_burst_max_attempts,
                    active_burst_max_fills=cfg.active_burst_max_fills,
                    active_burst_pause_min_ms=cfg.active_burst_pause_min_ms,
                    active_burst_pause_max_ms=cfg.active_burst_pause_max_ms,
                )
                self._log_signal_if_changed(dwell_key, signal)
                return signal

        if active_qty <= cfg.threshold_qty and dwell_key in self._pressure_dwell_start_ms:
            del self._pressure_dwell_start_ms[dwell_key]

        passive_price = self._resolve_passive_price(state, position_side, cfg.passive_level)
        if passive_price is None:
            self._log_pressure_skip_once(
                dwell_key,
                reason="passive_level_missing",
                message=(
                    f"盘口量模式跳过: {symbol} {position_side.value} "
                    f"passive_level={cfg.passive_level} 对应档位不存在或数据无效"
                ),
            )
            return None

        reason = (
            SignalReason.LONG_ASK_PRESSURE_PASSIVE
            if position_side == PositionSide.LONG
            else SignalReason.SHORT_BID_PRESSURE_PASSIVE
        )
        if not active_paused:
            self._clear_pressure_skip_reason(dwell_key)
        self._last_signal_ms[dwell_key] = current_ms
        signal = ExitSignal(
            symbol=symbol,
            position_side=position_side,
            reason=reason,
            timestamp_ms=current_ms,
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            last_trade_price=state.last_trade_price if state.last_trade_price > Decimal("0")
            else passive_price,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            execution_preference=SignalExecutionPreference.PASSIVE,
            qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
            price_override=passive_price,
            ttl_override_ms=self._jitter_duration_ms(
                cfg.passive_ttl_ms,
                cfg.passive_ttl_jitter_pct,
                minimum_ms=1,
            ),
            cooldown_override_ms=0,
            fixed_lot_mult=cfg.lot_mult,
            fixed_qty_jitter_pct=cfg.qty_jitter_pct,
            fixed_qty_anti_repeat_lookback=cfg.qty_anti_repeat_lookback,
        )
        self._log_signal_if_changed(dwell_key, signal)
        return signal

    def record_pressure_active_attempt(self, symbol: str, position_side: PositionSide, *, ts_ms: int) -> None:
        self._record_pressure_active_event(
            symbol=symbol,
            position_side=position_side,
            ts_ms=ts_ms,
            is_fill=False,
        )

    def record_pressure_active_fill(self, symbol: str, position_side: PositionSide, *, ts_ms: int) -> None:
        self._record_pressure_active_event(
            symbol=symbol,
            position_side=position_side,
            ts_ms=ts_ms,
            is_fill=True,
        )

    def _record_pressure_active_event(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        ts_ms: int,
        is_fill: bool,
    ) -> None:
        cfg = self._symbol_pressure_configs.get(symbol)
        if cfg is None:
            return
        if (
            cfg.active_burst_window_ms <= 0
            or cfg.active_burst_pause_max_ms <= 0
            or (
                cfg.active_burst_max_attempts <= 0
                and cfg.active_burst_max_fills <= 0
            )
        ):
            return

        key = f"{symbol}:{position_side.value}"
        attempt_history = self._pressure_active_attempt_history_ms.setdefault(key, deque())
        fill_history = self._pressure_active_fill_history_ms.setdefault(key, deque())
        self._prune_pressure_active_history(
            attempt_history=attempt_history,
            fill_history=fill_history,
            current_ms=ts_ms,
            window_ms=cfg.active_burst_window_ms,
        )

        if is_fill:
            fill_history.append(ts_ms)
        else:
            attempt_history.append(ts_ms)

        pause_until_ms = self._pressure_active_pause_until_ms.get(key, 0)
        if pause_until_ms > ts_ms:
            return

        should_pause = (
            (cfg.active_burst_max_attempts > 0 and len(attempt_history) >= cfg.active_burst_max_attempts)
            or (cfg.active_burst_max_fills > 0 and len(fill_history) >= cfg.active_burst_max_fills)
        )
        if not should_pause:
            return

        pause_ms = self._compute_pressure_active_pause_ms(cfg)
        if pause_ms <= 0:
            return

        self._pressure_active_pause_until_ms[key] = ts_ms + pause_ms
        log_event(
            "pressure_burst",
            level="info",
            symbol=symbol,
            side=position_side.value,
            reason="active_burst_pause",
            attempts=len(attempt_history),
            fills=len(fill_history),
            window_ms=cfg.active_burst_window_ms,
            pause_ms=pause_ms,
        )

    @staticmethod
    def _prune_pressure_active_history(
        *,
        attempt_history: Deque[int],
        fill_history: Deque[int],
        current_ms: int,
        window_ms: int,
    ) -> None:
        cutoff_ms = current_ms - window_ms
        while attempt_history and attempt_history[0] < cutoff_ms:
            attempt_history.popleft()
        while fill_history and fill_history[0] < cutoff_ms:
            fill_history.popleft()

    def _is_pressure_active_paused(
        self,
        *,
        key: str,
        symbol: str,
        position_side: PositionSide,
        current_ms: int,
    ) -> bool:
        pause_until_ms = self._pressure_active_pause_until_ms.get(key, 0)
        if pause_until_ms <= current_ms:
            if pause_until_ms > 0:
                del self._pressure_active_pause_until_ms[key]
                self._clear_pressure_skip_reason(key)
            return False

        self._log_pressure_skip_once(
            key,
            reason="active_burst_pause",
            message=(
                f"盘口量模式跳过主动单: {symbol} {position_side.value} "
                f"reason=active_burst_pause remaining_ms={pause_until_ms - current_ms}"
            ),
        )
        return True

    @staticmethod
    def _compute_pressure_active_pause_ms(cfg: PressureSignalConfig) -> int:
        if cfg.active_burst_pause_max_ms <= 0:
            return 0
        lower = max(0, cfg.active_burst_pause_min_ms)
        upper = max(lower, cfg.active_burst_pause_max_ms)
        return randint(lower, upper)

    @staticmethod
    def _jitter_duration_ms(base_ms: int, jitter_pct: Decimal, *, minimum_ms: int) -> int:
        if base_ms <= 0:
            return 0 if minimum_ms == 0 else minimum_ms
        if jitter_pct <= 0:
            return max(base_ms, minimum_ms)

        jitter_range = max(1, round(base_ms * float(jitter_pct)))
        lower = max(minimum_ms, base_ms - jitter_range)
        upper = max(lower, base_ms + jitter_range)
        return randint(lower, upper)

    def _resolve_passive_price(
        self,
        state: MarketState,
        position_side: PositionSide,
        passive_level: int,
    ) -> Optional[Decimal]:
        levels = state.ask_levels if position_side == PositionSide.LONG else state.bid_levels
        index = passive_level - 1
        if index < 0 or index >= len(levels):
            return None
        price, qty = levels[index]
        if price <= Decimal("0") or qty <= Decimal("0"):
            return None
        return price

    def _log_signal_if_changed(self, key: str, signal: ExitSignal) -> None:
        signature = self._build_signal_log_signature(signal)
        previous = self._last_logged_signal.get(key)
        if previous is not None:
            previous_signature, previous_ts_ms = previous
            if previous_signature == signature:
                if signal.timestamp_ms - previous_ts_ms < SIGNAL_LOG_HEARTBEAT_MS:
                    return

        self._last_logged_signal[key] = (signature, signal.timestamp_ms)
        log_signal(
            symbol=signal.symbol,
            side=signal.position_side.value,
            reason=signal.reason.value,
            best_bid=signal.best_bid,
            best_ask=signal.best_ask,
            last_trade=signal.last_trade_price,
            roi_mult=signal.roi_mult,
            accel_mult=signal.accel_mult,
            roi=signal.roi,
            ret_window=signal.ret_window,
        )

    @staticmethod
    def _build_signal_log_signature(signal: ExitSignal) -> Tuple[object, ...]:
        if signal.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE:
            return (
                signal.strategy_mode,
                signal.reason,
                signal.execution_preference,
                signal.price_override,
            )
        return (
            signal.strategy_mode,
            signal.reason,
            signal.roi_mult,
            signal.accel_mult,
        )

    def _log_pressure_skip_once(self, key: str, *, reason: str, message: str) -> None:
        if self._last_pressure_skip_reason.get(key) == reason:
            return
        self._last_pressure_skip_reason[key] = reason
        get_logger().debug(message)

    def _clear_pressure_skip_reason(self, key: str) -> None:
        self._last_pressure_skip_reason.pop(key, None)

    def reset_pressure_dwell(
        self,
        symbol: str,
        position_side: PositionSide,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if self._symbol_strategy_modes.get(symbol, StrategyMode.ORDERBOOK_PRICE) != StrategyMode.ORDERBOOK_PRESSURE:
            return

        key = f"{symbol}:{position_side.value}"
        if key in self._pressure_dwell_start_ms:
            del self._pressure_dwell_start_ms[key]
        if reason is not None:
            self._clear_pressure_skip_reason(key)

    def is_strategy_data_stale(self, symbol: str, current_ms: int, stale_data_ms: int) -> bool:
        state = self._market_states.get(symbol)
        if state is None:
            return True

        strategy_mode = self._symbol_strategy_modes.get(symbol, StrategyMode.ORDERBOOK_PRICE)
        if strategy_mode != StrategyMode.ORDERBOOK_PRESSURE:
            return (current_ms - state.last_update_ms) > stale_data_ms if state.last_update_ms > 0 else True

        book_stale = (
            state.last_book_ticker_ms <= 0 or current_ms - state.last_book_ticker_ms > stale_data_ms
        )
        depth_stale = (
            state.last_depth_update_ms <= 0 or current_ms - state.last_depth_update_ms > stale_data_ms
        )
        if book_stale or depth_stale:
            stale_parts: list[str] = []
            if book_stale:
                stale_parts.append("book_ticker")
            if depth_stale:
                stale_parts.append("depth10")
            self._log_pressure_skip_once(
                f"{symbol}:freshness",
                reason="|".join(stale_parts),
                message=f"盘口量模式跳过: {symbol} 数据陈旧 | stale_sources={','.join(stale_parts)}",
            )
            return True

        self._clear_pressure_skip_reason(f"{symbol}:freshness")
        return False

    def _is_symbol_ready(self, symbol: str, state: MarketState) -> bool:
        strategy_mode = self._symbol_strategy_modes.get(symbol, StrategyMode.ORDERBOOK_PRICE)
        if strategy_mode == StrategyMode.ORDERBOOK_PRESSURE:
            return (
                self._has_book_data.get(symbol, False)
                and self._has_depth_data.get(symbol, False)
                and state.best_bid > Decimal("0")
                and state.best_ask > Decimal("0")
                and len(state.bid_levels) > 0
                and len(state.ask_levels) > 0
            )

        return (
            self._has_book_data.get(symbol, False)
            and self._has_trade_data.get(symbol, False)
            and state.previous_trade_price is not None
            and state.best_bid > Decimal("0")
            and state.best_ask > Decimal("0")
            and state.last_trade_price > Decimal("0")
        )

    def _check_long_exit(self, state: MarketState) -> Optional[SignalReason]:
        """
        检查 LONG 平仓条件

        条件（设计文档 3.1）：
        - long_primary: last > prev AND best_bid >= last
        - long_bid_improve: (not primary) AND best_bid >= last AND best_bid > prev

        Args:
            state: 市场状态

        Returns:
            SignalReason 或 None
        """
        if state.previous_trade_price is None:
            return None

        last = state.last_trade_price
        prev = state.previous_trade_price
        best_bid = state.best_bid

        # Primary condition: 价格上涨 AND 买一支撑当前价
        long_primary = last > prev and best_bid >= last

        if long_primary:
            return SignalReason.LONG_PRIMARY

        # Bid improve condition: 买一支撑当前价 AND 买一比上一成交价高
        long_bid_improve = best_bid >= last and best_bid > prev

        if long_bid_improve:
            return SignalReason.LONG_BID_IMPROVE

        return None

    def _check_short_exit(self, state: MarketState) -> Optional[SignalReason]:
        """
        检查 SHORT 平仓条件

        条件（设计文档 3.2）：
        - short_primary: last < prev AND best_ask <= last
        - short_ask_improve: (not primary) AND best_ask <= last AND best_ask < prev

        Args:
            state: 市场状态

        Returns:
            SignalReason 或 None
        """
        if state.previous_trade_price is None:
            return None

        last = state.last_trade_price
        prev = state.previous_trade_price
        best_ask = state.best_ask

        # Primary condition: 价格下跌 AND 卖一压低到当前价
        short_primary = last < prev and best_ask <= last

        if short_primary:
            return SignalReason.SHORT_PRIMARY

        # Ask improve condition: 卖一压低到当前价 AND 卖一比上一成交价低
        short_ask_improve = best_ask <= last and best_ask < prev

        if short_ask_improve:
            return SignalReason.SHORT_ASK_IMPROVE

        return None

    def _is_throttled(self, symbol: str, position_side: PositionSide, current_ms: int) -> bool:
        """
        检查是否在节流期内

        Args:
            symbol: 交易对
            position_side: 仓位方向
            current_ms: 当前时间戳

        Returns:
            True 如果在节流期内
        """
        key = f"{symbol}:{position_side.value}"
        last_signal_ms = self._last_signal_ms.get(key, 0)

        if last_signal_ms == 0:
            return False

        elapsed = current_ms - last_signal_ms
        interval = self._symbol_min_signal_interval_ms.get(symbol, self.min_signal_interval_ms)
        return elapsed < interval

    def _compute_accel_ret(self, symbol: str, current_ms: int, last_price: Decimal) -> Optional[Decimal]:
        """计算滑动窗口回报率 ret = p_now/p_window_ago - 1（基于 last_trade_price）。"""
        if last_price <= Decimal("0"):
            return None

        history = self._trade_history.get(symbol)
        if not history or len(history) < 2:
            return None

        window_ms = self._symbol_accel_window_ms.get(symbol, 2000)
        cutoff = current_ms - window_ms

        # 移除窗口外数据（保留窗口内最早点作为 window_ago 近似）
        while history and history[0][0] < cutoff:
            history.popleft()

        if not history:
            return None

        window_price = history[0][1]
        if window_price <= Decimal("0"):
            return None

        return (last_price / window_price) - Decimal("1")

    def _select_accel_mult(
        self, symbol: str, position_side: PositionSide, ret_window: Optional[Decimal]
    ) -> int:
        """按档位选择 accel_mult（取满足条件的最高档）。LONG/SHORT 共用 tiers，方向自动处理。"""
        if ret_window is None:
            return 1

        tiers = self._symbol_accel_tiers.get(symbol, [])

        best_mult = 1
        for threshold, mult in tiers:
            candidate = max(int(mult), 1)
            if position_side == PositionSide.LONG:
                if ret_window >= threshold:
                    best_mult = max(best_mult, candidate)
            else:
                if ret_window <= -threshold:
                    best_mult = max(best_mult, candidate)
        return best_mult

    def _compute_roi(self, position: Position) -> Optional[Decimal]:
        """计算该侧仓位 ROI（以初始保证金为分母的比例值）。"""
        qty = abs(position.position_amt)
        if qty <= Decimal("0"):
            return None
        if position.entry_price <= Decimal("0"):
            return None

        leverage = position.leverage if position.leverage > 0 else 1
        notional = qty * position.entry_price
        initial_margin = notional / Decimal(leverage)
        if initial_margin <= Decimal("0"):
            return None

        return position.unrealized_pnl / initial_margin

    def _select_roi_mult(self, symbol: str, roi: Optional[Decimal]) -> int:
        """按档位选择 roi_mult（取满足条件的最高档）。"""
        if roi is None:
            return 1

        tiers = self._symbol_roi_tiers.get(symbol, [])
        best_mult = 1
        for threshold, mult in tiers:
            candidate = max(int(mult), 1)
            if roi >= threshold:
                best_mult = max(best_mult, candidate)
        return best_mult

    def get_market_state(self, symbol: str) -> Optional[MarketState]:
        """
        获取 symbol 的市场状态

        Args:
            symbol: 交易对

        Returns:
            MarketState 或 None
        """
        return self._market_states.get(symbol)

    def is_data_ready(self, symbol: str) -> bool:
        """
        检查是否有足够数据进行信号判断

        Args:
            symbol: 交易对

        Returns:
            True 如果数据就绪
        """
        state = self._market_states.get(symbol)
        return state is not None and state.is_ready

    def reset_throttle(self, symbol: str, position_side: PositionSide) -> None:
        """
        重置节流计时器

        Args:
            symbol: 交易对
            position_side: 仓位方向
        """
        key = f"{symbol}:{position_side.value}"
        if key in self._last_signal_ms:
            del self._last_signal_ms[key]
        if key in self._last_logged_signal:
            del self._last_logged_signal[key]

    def clear_state(self, symbol: str) -> None:
        """
        清除指定 symbol 的状态

        Args:
            symbol: 交易对
        """
        if symbol in self._market_states:
            del self._market_states[symbol]
        if symbol in self._has_book_data:
            del self._has_book_data[symbol]
        if symbol in self._has_trade_data:
            del self._has_trade_data[symbol]
        if symbol in self._has_depth_data:
            del self._has_depth_data[symbol]
        if symbol in self._trade_history:
            del self._trade_history[symbol]
        keys_to_remove = [k for k in self._pressure_dwell_start_ms if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._pressure_dwell_start_ms[key]
        keys_to_remove = [k for k in self._last_pressure_skip_reason if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._last_pressure_skip_reason[key]
        keys_to_remove = [k for k in self._pressure_active_attempt_history_ms if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._pressure_active_attempt_history_ms[key]
        keys_to_remove = [k for k in self._pressure_active_fill_history_ms if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._pressure_active_fill_history_ms[key]
        keys_to_remove = [k for k in self._pressure_active_pause_until_ms if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._pressure_active_pause_until_ms[key]

        # 清除相关的节流记录
        keys_to_remove = [k for k in self._last_signal_ms if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._last_signal_ms[key]

        keys_to_remove = [k for k in self._last_logged_signal if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del self._last_logged_signal[key]
