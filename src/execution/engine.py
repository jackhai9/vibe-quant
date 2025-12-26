# Input: ExitSignal, OrderUpdate, OrderResult, config, rules
# Output: OrderIntent and per-side execution state transitions (including fill-rate feedback)
# Pos: per-side execution state machine with WS/REST fill meta handling
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
执行引擎模块

职责：
- 维护每个 symbol+side 的执行状态机
- 处理下单/撤单/TTL 超时
- 支持执行模式轮转（maker → aggressive limit）
- 实现 maker 定价策略（at_touch / inside_spread_1tick / custom_ticks）

输入：
- ExitSignal
- OrderUpdate
- 配置

输出：
- OrderIntent（传给 ExchangeAdapter）

状态机：
IDLE → PLACING → WAITING → (FILLED|TIMEOUT) → CANCELING → COOLDOWN → IDLE
"""

from decimal import Decimal
from typing import Callable, Dict, Optional, Awaitable

from src.models import (
    ExitSignal,
    OrderIntent,
    OrderResult,
    OrderUpdate,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeInForce,
    PositionSide,
    ExecutionState,
    ExecutionMode,
    SideExecutionState,
    MarketState,
    SymbolRules,
)
from src.utils.logger import (
    get_logger,
    log_order_place,
    log_order_cancel,
    log_order_fill,
    log_order_timeout,
    log_event,
)
from src.utils.helpers import round_to_tick, round_to_step


class ExecutionEngine:
    """执行引擎"""

    def __init__(
        self,
        place_order: Callable[[OrderIntent], Awaitable[OrderResult]],
        cancel_order: Callable[[str, str], Awaitable[OrderResult]],
        on_fill: Optional[
            Callable[
                [
                    str,
                    PositionSide,
                    ExecutionMode,
                    Decimal,
                    Decimal,
                    str,
                    Optional[str],
                    Optional[Decimal],
                    Optional[Decimal],
                    Optional[str],
                ],
                None,
            ]
        ] = None,
        fetch_order_trade_meta: Optional[
            Callable[
                [str, str],
                Awaitable[tuple[Optional[bool], Optional[Decimal], Optional[Decimal], Optional[str]]],
            ]
        ] = None,
        order_ttl_ms: int = 800,
        repost_cooldown_ms: int = 100,
        base_lot_mult: int = 1,
        maker_price_mode: str = "inside_spread_1tick",
        maker_n_ticks: int = 1,
        maker_safety_ticks: int = 1,
        maker_timeouts_to_escalate: int = 2,
        aggr_fills_to_deescalate: int = 1,
        aggr_timeouts_to_deescalate: int = 2,
        max_mult: int = 50,
        max_order_notional: Decimal = Decimal("200"),
        ws_fill_grace_ms: int = 5000,
        fill_rate_feedback_enabled: bool = False,
        fill_rate_window_ms: int = 300000,
        fill_rate_low_threshold: Decimal = Decimal("0.25"),
        fill_rate_high_threshold: Decimal = Decimal("0.75"),
        fill_rate_low_maker_timeouts_to_escalate: int = 1,
        fill_rate_high_maker_timeouts_to_escalate: Optional[int] = None,
    ):
        """
        初始化执行引擎

        Args:
            place_order: 下单函数
            cancel_order: 撤单函数
            on_fill: 成交通知回调（不得阻塞主链路）
            fetch_order_trade_meta: 查询订单 maker 状态/已实现盈亏/手续费（用于 WS 超时时回退查询）
            order_ttl_ms: 订单 TTL
            repost_cooldown_ms: 撤单后冷却时间
            base_lot_mult: 基础片大小倍数
            maker_price_mode: maker 定价模式
            maker_n_ticks: custom_ticks 模式的 tick 数
            maker_safety_ticks: post-only maker 安全距离（ticks）
            maker_timeouts_to_escalate: maker 超时升级阈值（<=0 表示不升级）
            aggr_fills_to_deescalate: aggressive 成交降级阈值（<=0 表示不降级）
            aggr_timeouts_to_deescalate: aggressive 超时降级阈值（<=0 表示不降级）
            max_mult: 最大倍数
            max_order_notional: 最大订单名义价值
            ws_fill_grace_ms: 已完成订单等待 WS 成交回执的最大时间
            fill_rate_feedback_enabled: 是否启用成交率反馈
            fill_rate_window_ms: 成交率统计窗口(ms)
            fill_rate_low_threshold: 低成交率阈值
            fill_rate_high_threshold: 高成交率阈值
            fill_rate_low_maker_timeouts_to_escalate: 低成交率时的 maker 超时升级阈值
            fill_rate_high_maker_timeouts_to_escalate: 高成交率时的 maker 超时升级阈值
        """
        self.place_order = place_order
        self.cancel_order = cancel_order
        self._on_fill = on_fill
        self._fetch_order_trade_meta = fetch_order_trade_meta
        self.order_ttl_ms = order_ttl_ms
        self.repost_cooldown_ms = repost_cooldown_ms
        self.base_lot_mult = base_lot_mult
        self.maker_price_mode = maker_price_mode
        self.maker_n_ticks = maker_n_ticks
        if maker_safety_ticks < 1:
            raise ValueError("maker_safety_ticks must be >= 1")
        self.maker_safety_ticks = maker_safety_ticks
        self.maker_timeouts_to_escalate = maker_timeouts_to_escalate
        self.aggr_fills_to_deescalate = aggr_fills_to_deescalate
        self.aggr_timeouts_to_deescalate = aggr_timeouts_to_deescalate
        self.max_mult = max_mult
        self.max_order_notional = max_order_notional
        self.ws_fill_grace_ms = ws_fill_grace_ms
        self.fill_rate_feedback_enabled = fill_rate_feedback_enabled
        self.fill_rate_window_ms = fill_rate_window_ms
        self.fill_rate_low_threshold = fill_rate_low_threshold
        self.fill_rate_high_threshold = fill_rate_high_threshold
        self.fill_rate_low_maker_timeouts_to_escalate = fill_rate_low_maker_timeouts_to_escalate
        self.fill_rate_high_maker_timeouts_to_escalate = fill_rate_high_maker_timeouts_to_escalate

        if self.fill_rate_feedback_enabled:
            if self.fill_rate_low_threshold > self.fill_rate_high_threshold:
                raise ValueError("fill_rate_low_threshold must be <= fill_rate_high_threshold")
            if self.fill_rate_window_ms <= 0:
                raise ValueError("fill_rate_window_ms must be > 0")

        self._states: Dict[str, SideExecutionState] = {}  # key: symbol:position_side

    def get_state(self, symbol: str, position_side: PositionSide) -> SideExecutionState:
        """
        获取或创建执行状态

        Args:
            symbol: 交易对
            position_side: 仓位方向

        Returns:
            SideExecutionState
        """
        key = f"{symbol}:{position_side.value}"
        if key not in self._states:
            self._states[key] = SideExecutionState(
                symbol=symbol,
                position_side=position_side,
            )
        return self._states[key]

    def set_mode(self, symbol: str, position_side: PositionSide, mode: ExecutionMode, reason: str) -> None:
        """外部强制设置执行模式（用于风控兜底等高优先级策略）。"""
        state = self.get_state(symbol, position_side)
        self._set_mode(state, mode, reason=reason)

    def _update_fill_rate(
        self,
        state: SideExecutionState,
        current_ms: int,
        *,
        is_submit: bool = False,
        is_fill: bool = False,
        force_log: bool = False,
    ) -> None:
        if not self.fill_rate_feedback_enabled:
            return

        if is_submit:
            state.recent_maker_submits.append(current_ms)
        if is_fill:
            state.recent_maker_fills.append(current_ms)

        cutoff = current_ms - self.fill_rate_window_ms
        while state.recent_maker_submits and state.recent_maker_submits[0] < cutoff:
            state.recent_maker_submits.popleft()
        while state.recent_maker_fills and state.recent_maker_fills[0] < cutoff:
            state.recent_maker_fills.popleft()

        submits = len(state.recent_maker_submits)
        if submits == 0:
            state.fill_rate = None
            state.fill_rate_bucket = None
            state.fill_rate_maker_timeouts_override = None
            return

        fills = len(state.recent_maker_fills)
        fill_rate = Decimal(fills) / Decimal(submits)
        bucket: str
        override: Optional[int]
        if fill_rate < self.fill_rate_low_threshold:
            bucket = "low"
            override = self.fill_rate_low_maker_timeouts_to_escalate
        elif fill_rate > self.fill_rate_high_threshold:
            bucket = "high"
            override = self.fill_rate_high_maker_timeouts_to_escalate
        else:
            bucket = "mid"
            override = None

        if fill_rate is not None and (force_log or bucket != state.fill_rate_bucket):
            log_event(
                "fill_rate",
                symbol=state.symbol,
                side=state.position_side.value,
                fill_rate=fill_rate,
                bucket=bucket,
                submits=submits,
                fills=fills,
                maker_timeouts_to_escalate=override,
            )

        state.fill_rate = fill_rate
        state.fill_rate_bucket = bucket
        state.fill_rate_maker_timeouts_override = override

    def log_fill_rate_snapshot(self, symbol: str, position_side: PositionSide, current_ms: int) -> None:
        """按当前窗口输出成交率快照（若无数据则跳过）。"""
        state = self.get_state(symbol, position_side)
        self._update_fill_rate(state, current_ms, force_log=True)

    async def on_signal(
        self,
        signal: ExitSignal,
        position_amt: Decimal,
        rules: SymbolRules,
        market_state: MarketState,
        current_ms: int,
    ) -> Optional[OrderIntent]:
        """
        处理平仓信号

        Args:
            signal: 平仓信号
            position_amt: 当前仓位数量
            rules: 交易规则
            market_state: 市场状态
            current_ms: 当前时间戳

        Returns:
            OrderIntent（如果需要下单）或 None
        """
        logger = get_logger()
        state = self.get_state(signal.symbol, signal.position_side)

        # 只有在 IDLE 状态才处理新信号
        if state.state != ExecutionState.IDLE:
            logger.debug(f"{signal.symbol} {signal.position_side.value} 状态为 {state.state.value}，跳过信号")
            return None

        # 检查仓位是否已完成
        if self.is_position_done(position_amt, rules.min_qty, rules.step_size):
            logger.debug(f"{signal.symbol} {signal.position_side.value} 仓位已完成")
            return None

        # 计算下单数量
        qty = self.compute_qty(
            position_amt=position_amt,
            min_qty=rules.min_qty,
            step_size=rules.step_size,
            last_trade_price=market_state.last_trade_price,
            roi_mult=signal.roi_mult,
            accel_mult=signal.accel_mult,
        )

        if qty <= Decimal("0"):
            logger.debug(f"{signal.symbol} {signal.position_side.value} 计算数量为 0")
            return None

        # 根据执行模式计算价格与 TIF
        if state.mode == ExecutionMode.MAKER_ONLY:
            price = self.build_maker_price(
                position_side=signal.position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTX  # Post-only for maker
        elif state.mode == ExecutionMode.AGGRESSIVE_LIMIT:
            price = self.build_aggressive_limit_price(
                position_side=signal.position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTC
        else:
            # 默认回退到 maker
            price = self.build_maker_price(
                position_side=signal.position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTX

        # 确定下单方向
        # LONG 平仓 -> SELL, SHORT 平仓 -> BUY
        side = OrderSide.SELL if signal.position_side == PositionSide.LONG else OrderSide.BUY

        # 创建 OrderIntent
        intent = OrderIntent(
            symbol=signal.symbol,
            side=side,
            position_side=signal.position_side,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            reduce_only=True,
        )

        # 更新状态为 PLACING
        state.state = ExecutionState.PLACING
        state.current_order_placed_ms = current_ms
        state.current_order_mode = state.mode
        state.current_order_reason = signal.reason.value
        state.current_order_is_risk = False
        state.current_order_filled_qty = Decimal("0")

        logger.debug(
            f"创建下单意图: {signal.symbol} {side.value} {qty} @ {price} "
            f"(position_side={signal.position_side.value})"
        )

        return intent

    def compute_panic_qty(
        self,
        *,
        position_amt: Decimal,
        min_qty: Decimal,
        step_size: Decimal,
        slice_ratio: Decimal,
    ) -> Decimal:
        """计算强平兜底（panic close）下单数量：按仓位比例切片，不受 max_order_notional/max_mult 约束。"""
        abs_position = abs(position_amt)
        if abs_position < min_qty:
            return Decimal("0")

        if slice_ratio <= Decimal("0"):
            return Decimal("0")

        raw_qty = abs_position * slice_ratio
        qty = round_to_step(raw_qty, step_size)

        # 规整后可能为 0：此时尝试使用 min_qty（但不得超过仓位）
        if qty < min_qty:
            qty = min_qty

        # 不得超过仓位（无 reduceOnly 下发时尤其重要）
        if qty > abs_position:
            qty = round_to_step(abs_position, step_size)

        if qty < min_qty:
            return Decimal("0")

        return qty

    async def on_panic_close(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        position_amt: Decimal,
        rules: SymbolRules,
        market_state: MarketState,
        current_ms: int,
        slice_ratio: Decimal,
        reason: str,
    ) -> Optional[OrderIntent]:
        """强平兜底：不依赖信号，强制按分级规则持续平仓。"""
        logger = get_logger()
        state = self.get_state(symbol, position_side)

        # 只有在 IDLE 状态才允许发起新的兜底订单
        if state.state != ExecutionState.IDLE:
            return None

        # 检查仓位是否已完成
        if self.is_position_done(position_amt, rules.min_qty, rules.step_size):
            return None

        qty = self.compute_panic_qty(
            position_amt=position_amt,
            min_qty=rules.min_qty,
            step_size=rules.step_size,
            slice_ratio=slice_ratio,
        )
        if qty <= Decimal("0"):
            return None

        # 根据执行模式计算价格与 TIF
        if state.mode == ExecutionMode.MAKER_ONLY:
            price = self.build_maker_price(
                position_side=position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTX
        elif state.mode == ExecutionMode.AGGRESSIVE_LIMIT:
            price = self.build_aggressive_limit_price(
                position_side=position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTC
        else:
            logger.warning(f"未知执行模式: {symbol} {position_side.value} mode={state.mode!r}，回退 maker")
            price = self.build_maker_price(
                position_side=position_side,
                best_bid=market_state.best_bid,
                best_ask=market_state.best_ask,
                tick_size=rules.tick_size,
            )
            time_in_force = TimeInForce.GTX

        side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY

        intent = OrderIntent(
            symbol=symbol,
            side=side,
            position_side=position_side,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            reduce_only=True,
            is_risk=True,
        )

        state.state = ExecutionState.PLACING
        state.current_order_placed_ms = current_ms
        state.current_order_mode = state.mode
        state.current_order_reason = reason
        state.current_order_is_risk = True
        state.current_order_filled_qty = Decimal("0")

        return intent

    async def on_order_placed(
        self,
        intent: OrderIntent,
        result: OrderResult,
        current_ms: int,
    ) -> None:
        """
        处理下单结果

        Args:
            symbol: 交易对
            position_side: 仓位方向
            result: 下单结果
            current_ms: 当前时间戳
        """
        state = self.get_state(intent.symbol, intent.position_side)

        if result.success and result.order_id:
            # 下单成功，进入 WAITING 状态
            state.state = ExecutionState.WAITING
            state.current_order_id = result.order_id
            state.current_order_placed_ms = current_ms
            state.current_order_filled_qty = result.filled_qty

            log_order_place(
                symbol=intent.symbol,
                side=intent.position_side.value,
                mode=state.mode.value,
                qty=intent.qty,
                price=intent.price,
                order_id=result.order_id,
            )

            order_mode = state.current_order_mode or state.mode
            if (not intent.is_risk) and order_mode == ExecutionMode.MAKER_ONLY:
                self._update_fill_rate(state, current_ms, is_submit=True)

            # 如果已经完全成交，立即完成状态并等待 WS 成交回执补全 role
            if result.status == OrderStatus.FILLED:
                state.last_completed_order_id = result.order_id
                state.last_completed_ms = current_ms
                state.pending_fill_log = True
                state.last_completed_filled_qty = result.filled_qty
                state.last_completed_avg_price = result.avg_price
                state.last_completed_mode = state.current_order_mode or state.mode
                state.last_completed_reason = state.current_order_reason or "unknown"
                state.last_completed_realized_pnl = None
                state.last_completed_fee = None
                state.last_completed_fee_asset = None
                await self._handle_filled(
                    intent.symbol,
                    intent.position_side,
                    result,
                    current_ms=current_ms,
                    emit_fill_log=False,
                    emit_on_fill=False,
                )
        else:
            # 下单失败，回到 IDLE 状态
            # 下单失败进入短暂冷却，避免连续触发导致刷屏/触发限速
            state.state = ExecutionState.COOLDOWN
            state.current_order_id = None
            state.current_order_placed_ms = current_ms
            state.current_order_mode = None
            state.current_order_reason = None
            state.current_order_is_risk = False
            state.current_order_filled_qty = Decimal("0")
            # -5022 Post Only rejected 属于“可预期”的交易所拒单，已由 ExchangeAdapter 以结构化日志记录，避免重复刷屏
            if result.error_code == "-5022":
                return
            get_logger().warning(f"下单失败: {intent.symbol} {intent.position_side.value} - {result.error_message}")

    def _should_accept_late_fill(
        self,
        state: SideExecutionState,
        update: OrderUpdate,
        current_ms: int,
    ) -> bool:
        if not state.pending_fill_log or not state.last_completed_order_id:
            return False
        if update.order_id != state.last_completed_order_id:
            return False
        if current_ms - state.last_completed_ms > self.ws_fill_grace_ms:
            return False
        return update.status == OrderStatus.FILLED and update.filled_qty > Decimal("0")

    async def _flush_pending_fill_if_expired(
        self,
        state: SideExecutionState,
        current_ms: int,
    ) -> None:
        if not state.pending_fill_log:
            return
        if current_ms - state.last_completed_ms <= self.ws_fill_grace_ms:
            return
        if state.last_completed_order_id:
            # 尝试通过 REST 查询 maker 状态与已实现盈亏
            role: Optional[str] = None
            pnl: Optional[Decimal] = None
            fee: Optional[Decimal] = None
            fee_asset: Optional[str] = None
            if self._fetch_order_trade_meta:
                try:
                    is_maker, realized_pnl, fee_value, fee_asset_value = await self._fetch_order_trade_meta(
                        state.symbol, state.last_completed_order_id
                    )
                    if is_maker is not None:
                        role = "maker" if is_maker else "taker"
                    pnl = realized_pnl
                    fee = fee_value
                    fee_asset = fee_asset_value
                except Exception as e:
                    get_logger().warning(f"查询成交元数据失败: {e}")
            if role is None:
                role = "unknown"
            if pnl is None:
                pnl = state.last_completed_realized_pnl
            if fee is None:
                fee = state.last_completed_fee
            if fee_asset is None:
                fee_asset = state.last_completed_fee_asset

            log_order_fill(
                symbol=state.symbol,
                side=state.position_side.value,
                order_id=state.last_completed_order_id,
                filled_qty=state.last_completed_filled_qty,
                avg_price=state.last_completed_avg_price,
                role=role,
                pnl=pnl,
                fee=fee,
                # fee_asset=fee_asset,
            )
            # 触发成交通知（使用缓存的 mode 和 reason）
            if self._on_fill:
                try:
                    self._on_fill(
                        state.symbol,
                        state.position_side,
                        state.last_completed_mode or state.mode,
                        state.last_completed_filled_qty,
                        state.last_completed_avg_price,
                        state.last_completed_reason or "unknown",
                        role,
                        pnl,
                        fee,
                        fee_asset,
                    )
                except Exception as e:
                    get_logger().warning(f"on_fill 回调异常: {e}")
        state.pending_fill_log = False
        state.last_completed_ms = current_ms
        state.last_completed_mode = None
        state.last_completed_reason = None
        state.last_completed_realized_pnl = None
        state.last_completed_fee = None
        state.last_completed_fee_asset = None

    async def on_order_update(self, update: OrderUpdate, current_ms: int) -> None:
        """
        处理订单更新

        Args:
            update: 订单更新事件
            current_ms: 当前时间戳
        """
        state = self.get_state(update.symbol, update.position_side)

        await self._flush_pending_fill_if_expired(state, current_ms)

        # 检查是否是当前订单
        if state.current_order_id != update.order_id:
            if self._should_accept_late_fill(state, update, current_ms):
                role = None
                pnl = update.realized_pnl
                fee = update.fee
                fee_asset = update.fee_asset
                if update.is_maker is not None:
                    role = "maker" if update.is_maker else "taker"
                log_order_fill(
                    symbol=update.symbol,
                    side=update.position_side.value,
                    order_id=update.order_id,
                    filled_qty=update.filled_qty,
                    avg_price=update.avg_price,
                    role=role,
                    pnl=pnl,
                    fee=fee,
                    # fee_asset=fee_asset,
                )
                # 触发成交通知（使用缓存的 mode 和 reason）
                if self._on_fill:
                    try:
                        self._on_fill(
                            update.symbol,
                            update.position_side,
                            state.last_completed_mode or state.mode,
                            state.last_completed_filled_qty,
                            state.last_completed_avg_price,
                            state.last_completed_reason or "unknown",
                            role,
                            pnl,
                            fee,
                            fee_asset,
                        )
                    except Exception as e:
                        get_logger().warning(f"on_fill 回调异常: {e}")
                state.pending_fill_log = False
                state.last_completed_order_id = None
                state.last_completed_ms = 0
                state.last_completed_filled_qty = Decimal("0")
                state.last_completed_avg_price = Decimal("0")
                state.last_completed_fee = None
                state.last_completed_fee_asset = None
                state.last_completed_mode = None
                state.last_completed_reason = None
                state.last_completed_realized_pnl = None
            else:
                if state.last_completed_order_id == update.order_id:
                    # TODO: WS 回执迟到且已超时忽略，后续落库时补数据一致性
                    pass
            return

        if update.status == OrderStatus.FILLED:
            await self._handle_filled(update.symbol, update.position_side, update, current_ms=current_ms)
        elif update.status == OrderStatus.CANCELED:
            await self._handle_canceled(update.symbol, update.position_side, current_ms)
        elif update.status == OrderStatus.REJECTED:
            await self._handle_rejected(update.symbol, update.position_side)
        elif update.status == OrderStatus.EXPIRED:
            await self._handle_expired(update.symbol, update.position_side, current_ms)
        elif update.status == OrderStatus.PARTIALLY_FILLED:
            # 部分成交，保持 WAITING 状态
            role = None
            if update.is_maker is not None:
                role = "maker" if update.is_maker else "taker"
            log_order_fill(
                symbol=update.symbol,
                side=update.position_side.value,
                order_id=update.order_id,
                filled_qty=update.filled_qty,
                avg_price=update.avg_price,
                role=role,
                pnl=update.realized_pnl,
                fee=update.fee,
                # fee_asset=update.fee_asset,
            )
            state.current_order_filled_qty = update.filled_qty

            # 部分成交视为“有成交”：重置超时计数器（避免误升级为更激进模式）
            order_mode = state.current_order_mode or state.mode
            if update.filled_qty > Decimal("0"):
                if order_mode == ExecutionMode.MAKER_ONLY:
                    state.maker_timeout_count = 0
                elif order_mode == ExecutionMode.AGGRESSIVE_LIMIT:
                    state.aggr_timeout_count = 0
                    # 有成交说明价格合理：后续优先回到 maker
                    if state.mode != ExecutionMode.MAKER_ONLY:
                        self._set_mode(state, ExecutionMode.MAKER_ONLY, reason="partial_fill_deescalate")

    async def _handle_filled(
        self,
        symbol: str,
        position_side: PositionSide,
        update: OrderUpdate | OrderResult,
        *,
        current_ms: int,
        emit_fill_log: bool = True,
        emit_on_fill: bool = True,
    ) -> None:
        """处理完全成交"""
        state = self.get_state(symbol, position_side)
        executed_mode = state.current_order_mode or state.mode

        filled_qty = update.filled_qty if update.filled_qty else Decimal("0")
        avg_price = update.avg_price if update.avg_price else Decimal("0")
        order_id = update.order_id if update.order_id else ""
        order_mode = executed_mode
        order_reason = state.current_order_reason or "unknown"

        role = None
        pnl: Optional[Decimal] = None
        fee: Optional[Decimal] = None
        fee_asset: Optional[str] = None
        if isinstance(update, OrderUpdate) and update.is_maker is not None:
            role = "maker" if update.is_maker else "taker"
        if isinstance(update, OrderUpdate):
            pnl = update.realized_pnl
            fee = update.fee
            fee_asset = update.fee_asset
        if emit_fill_log:
            log_order_fill(
                symbol=symbol,
                side=position_side.value,
                order_id=order_id,
                filled_qty=filled_qty,
                avg_price=avg_price,
                role=role,
                pnl=pnl,
                fee=fee,
                # fee_asset=fee_asset,
            )

        if (not state.current_order_is_risk) and executed_mode == ExecutionMode.MAKER_ONLY:
            self._update_fill_rate(state, current_ms, is_fill=True)

        # 成交通知（必须不阻塞主链路）
        if emit_on_fill and self._on_fill:
            try:
                self._on_fill(
                    symbol,
                    position_side,
                    order_mode,
                    filled_qty,
                    avg_price,
                    order_reason,
                    role,
                    pnl,
                    fee,
                    fee_asset,
                )
            except Exception as e:
                get_logger().warning(f"on_fill 回调异常: {e}")

        # 成交后更新轮转计数器/模式
        if executed_mode == ExecutionMode.MAKER_ONLY:
            state.maker_timeout_count = 0
        elif executed_mode == ExecutionMode.AGGRESSIVE_LIMIT:
            state.aggr_timeout_count = 0
            state.aggr_fill_count += 1
            if self.aggr_fills_to_deescalate > 0 and state.aggr_fill_count >= self.aggr_fills_to_deescalate:
                self._set_mode(state, ExecutionMode.MAKER_ONLY, reason="aggr_fill_deescalate")

        # 回到 IDLE 状态
        state.state = ExecutionState.IDLE
        state.current_order_id = None
        state.current_order_placed_ms = 0
        state.current_order_mode = None
        state.current_order_reason = None
        state.current_order_is_risk = False
        state.current_order_filled_qty = Decimal("0")

    async def _handle_canceled(
        self,
        symbol: str,
        position_side: PositionSide,
        current_ms: int,
    ) -> None:
        """处理订单取消"""
        state = self.get_state(symbol, position_side)

        log_order_cancel(
            symbol=symbol,
            order_id=state.current_order_id or "",
            reason=f"timeout_{position_side.value}",
        )

        # 进入 COOLDOWN 状态
        state.state = ExecutionState.COOLDOWN
        state.current_order_id = None
        state.current_order_placed_ms = current_ms  # 用作冷却开始时间
        state.current_order_mode = None
        state.current_order_reason = None
        state.current_order_is_risk = False
        state.current_order_filled_qty = Decimal("0")

    async def _handle_rejected(
        self,
        symbol: str,
        position_side: PositionSide,
    ) -> None:
        """处理订单拒绝（例如 GTX 被拒）"""
        state = self.get_state(symbol, position_side)

        get_logger().warning(f"订单被拒绝: {symbol} {position_side.value}")

        # 回到 IDLE 状态
        state.state = ExecutionState.IDLE
        state.current_order_id = None
        state.current_order_placed_ms = 0
        state.current_order_mode = None
        state.current_order_reason = None
        state.current_order_is_risk = False
        state.current_order_filled_qty = Decimal("0")

    async def _handle_expired(
        self,
        symbol: str,
        position_side: PositionSide,
        current_ms: int,
    ) -> None:
        """处理订单过期"""
        state = self.get_state(symbol, position_side)

        get_logger().info(f"订单过期: {symbol} {position_side.value}")

        # 进入 COOLDOWN 状态
        state.state = ExecutionState.COOLDOWN
        state.current_order_id = None
        state.current_order_placed_ms = current_ms
        state.current_order_mode = None
        state.current_order_reason = None
        state.current_order_is_risk = False
        state.current_order_filled_qty = Decimal("0")

    async def check_timeout(self, symbol: str, position_side: PositionSide, current_ms: int) -> bool:
        """
        检查订单是否超时，如果超时则撤单

        Args:
            symbol: 交易对
            position_side: 仓位方向
            current_ms: 当前时间戳

        Returns:
            True 如果触发了撤单
        """
        state = self.get_state(symbol, position_side)
        await self._flush_pending_fill_if_expired(state, current_ms)
        if not state.pending_fill_log and state.last_completed_order_id:
            if current_ms - state.last_completed_ms > self.ws_fill_grace_ms:
                state.last_completed_order_id = None
                state.last_completed_ms = 0
                state.last_completed_filled_qty = Decimal("0")
                state.last_completed_avg_price = Decimal("0")
                state.last_completed_realized_pnl = None
                state.last_completed_fee = None
                state.last_completed_fee_asset = None

        # 只在 WAITING 状态检查超时
        if state.state != ExecutionState.WAITING:
            return False

        order_mode = state.current_order_mode or state.mode
        ttl_ms = state.ttl_ms_override if state.ttl_ms_override is not None else self.order_ttl_ms
        elapsed = current_ms - state.current_order_placed_ms
        if elapsed < ttl_ms:
            return False

        had_fill = state.current_order_filled_qty > Decimal("0")

        # 更新状态为 CANCELING
        state.state = ExecutionState.CANCELING
        if order_mode == ExecutionMode.AGGRESSIVE_LIMIT:
            if had_fill:
                state.aggr_timeout_count = 0
            else:
                state.aggr_timeout_count += 1
            timeout_count = state.aggr_timeout_count
        else:
            if had_fill:
                state.maker_timeout_count = 0
            else:
                state.maker_timeout_count += 1
            timeout_count = state.maker_timeout_count

        # 超时，触发撤单
        log_order_timeout(
            symbol=symbol,
            side=position_side.value,
            order_id=state.current_order_id or "",
            timeout_count=timeout_count,
        )

        # 超时后执行模式轮转（不等待撤单完成）
        if order_mode == ExecutionMode.MAKER_ONLY:
            if state.maker_timeouts_to_escalate_override is not None:
                maker_escalate = state.maker_timeouts_to_escalate_override
            elif state.fill_rate_maker_timeouts_override is not None:
                maker_escalate = state.fill_rate_maker_timeouts_override
            else:
                maker_escalate = self.maker_timeouts_to_escalate
            if maker_escalate > 0 and state.maker_timeout_count >= maker_escalate:
                self._set_mode(state, ExecutionMode.AGGRESSIVE_LIMIT, reason="maker_timeout_escalate")
        elif order_mode == ExecutionMode.AGGRESSIVE_LIMIT:
            if self.aggr_timeouts_to_deescalate > 0 and state.aggr_timeout_count >= self.aggr_timeouts_to_deescalate:
                self._set_mode(state, ExecutionMode.MAKER_ONLY, reason="aggr_timeout_deescalate")
            # 风控兜底：若出现过成交（部分成交），下一轮优先回到 maker
            elif had_fill and state.mode != ExecutionMode.MAKER_ONLY:
                self._set_mode(state, ExecutionMode.MAKER_ONLY, reason="partial_fill_deescalate")

        # 发起撤单请求
        if state.current_order_id:
            try:
                result = await self.cancel_order(symbol, state.current_order_id)
                if not result.success:
                    get_logger().warning(
                        f"撤单请求失败: {symbol} {state.current_order_id} - {result.error_message}"
                    )
                # 进入冷却期但保留订单上下文，确保后续 WS 回执仍可被处理
                state.state = ExecutionState.COOLDOWN
                state.current_order_placed_ms = current_ms
            except Exception as e:
                get_logger().warning(f"撤单请求失败: {symbol} {state.current_order_id} - {e}")
                # 即使撤单请求失败，也进入 COOLDOWN（等待下次重试）
                state.state = ExecutionState.COOLDOWN
                state.current_order_placed_ms = current_ms

        return True

    def check_cooldown(self, symbol: str, position_side: PositionSide, current_ms: int) -> bool:
        """
        检查冷却是否结束

        Args:
            symbol: 交易对
            position_side: 仓位方向
            current_ms: 当前时间戳

        Returns:
            True 如果冷却结束（状态变为 IDLE）
        """
        state = self.get_state(symbol, position_side)

        if state.state != ExecutionState.COOLDOWN:
            return False

        elapsed = current_ms - state.current_order_placed_ms
        if elapsed < self.repost_cooldown_ms:
            return False

        # 冷却结束，回到 IDLE
        state.state = ExecutionState.IDLE
        state.current_order_placed_ms = 0

        return True

    def build_maker_price(
        self,
        position_side: PositionSide,
        best_bid: Decimal,
        best_ask: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        """
        计算 maker 挂单价格

        定价策略：
        - at_touch: LONG SELL -> best_ask, SHORT BUY -> best_bid
        - inside_spread_1tick: LONG SELL -> best_ask - tick, SHORT BUY -> best_bid + tick
        - custom_ticks: LONG SELL -> best_ask - n*tick, SHORT BUY -> best_bid + n*tick

        Args:
            position_side: 仓位方向（决定平仓方向）
            best_bid: 买一价
            best_ask: 卖一价
            tick_size: 价格最小变动

        Returns:
            挂单价格
        """
        if position_side == PositionSide.LONG:
            # LONG 平仓 -> SELL，挂在卖方
            if self.maker_price_mode == "at_touch":
                price = best_ask
            elif self.maker_price_mode == "inside_spread_1tick":
                price = best_ask - tick_size
            elif self.maker_price_mode == "custom_ticks":
                price = best_ask - tick_size * self.maker_n_ticks
            else:
                price = best_ask - tick_size  # 默认 inside_spread_1tick
        else:
            # SHORT 平仓 -> BUY，挂在买方
            if self.maker_price_mode == "at_touch":
                price = best_bid
            elif self.maker_price_mode == "inside_spread_1tick":
                price = best_bid + tick_size
            elif self.maker_price_mode == "custom_ticks":
                price = best_bid + tick_size * self.maker_n_ticks
            else:
                price = best_bid + tick_size  # 默认 inside_spread_1tick

        # 按 tick_size 规整
        price = round_to_tick(price, tick_size)

        # Post-only（GTX）订单必须是 maker：不能与对手价立即成交
        # - SELL: price 必须 > best_bid
        # - BUY:  price 必须 < best_ask
        if tick_size > Decimal("0"):
            if position_side == PositionSide.LONG:
                min_maker_price = round_to_tick(best_bid, tick_size) + tick_size * self.maker_safety_ticks
                if price < min_maker_price:
                    price = min_maker_price
            else:
                max_maker_price = round_to_tick(best_ask, tick_size) - tick_size * self.maker_safety_ticks
                if max_maker_price <= Decimal("0"):
                    max_maker_price = tick_size
                if price > max_maker_price:
                    price = max_maker_price

        return price

    def build_aggressive_limit_price(
        self,
        position_side: PositionSide,
        best_bid: Decimal,
        best_ask: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        """
        计算 aggressive limit 价格（不使用 post-only）

        - LONG 平仓（SELL）：price = best_bid
        - SHORT 平仓（BUY）：price = best_ask（向上规整到 tick）
        """
        if tick_size <= Decimal("0"):
            return best_bid if position_side == PositionSide.LONG else best_ask

        if position_side == PositionSide.LONG:
            return round_to_tick(best_bid, tick_size)

        # BUY 需要 >= best_ask，避免 floor 规整后落在 best_ask 下方导致不够激进
        price = round_to_tick(best_ask, tick_size)
        if price < best_ask:
            price += tick_size
        return price

    def _set_mode(self, state: SideExecutionState, new_mode: ExecutionMode, reason: str) -> None:
        """切换执行模式并重置相关计数器。"""
        if state.mode == new_mode:
            return

        from_mode = state.mode
        state.mode = new_mode

        # 重置计数器，避免跨模式累积导致频繁抖动
        state.maker_timeout_count = 0
        state.aggr_timeout_count = 0
        state.aggr_fill_count = 0

        log_event(
            "mode",
            symbol=state.symbol,
            side=state.position_side.value,
            mode=new_mode.value,
            reason=reason,
            from_mode=from_mode.value,
        )

    def compute_qty(
        self,
        position_amt: Decimal,
        min_qty: Decimal,
        step_size: Decimal,
        last_trade_price: Decimal,
        roi_mult: int = 1,
        accel_mult: int = 1,
    ) -> Decimal:
        """
        计算下单数量

        MVP 策略：
        - 基础数量 = min_qty * base_lot_mult
        - 确保不超过仓位
        - 确保不超过 max_order_notional
        - 按 step_size 规整

        Args:
            position_amt: 当前仓位
            min_qty: 最小下单量
            step_size: 数量步进
            last_trade_price: 最近成交价

        Returns:
            下单数量
        """
        abs_position = abs(position_amt)

        if abs_position < min_qty:
            return Decimal("0")

        base_mult = max(int(self.base_lot_mult), 1)
        roi_mult = max(int(roi_mult), 1)
        accel_mult = max(int(accel_mult), 1)
        max_mult = max(int(self.max_mult), 1)

        final_mult = base_mult * roi_mult * accel_mult
        if final_mult > max_mult:
            final_mult = max_mult

        base_qty = min_qty * final_mult

        # 不超过仓位
        qty = min(base_qty, abs_position)

        # 不超过 max_order_notional
        if last_trade_price > Decimal("0") and self.max_order_notional > Decimal("0"):
            max_qty_by_notional = self.max_order_notional / last_trade_price
            qty = min(qty, max_qty_by_notional)

        # 按 step_size 规整
        qty = round_to_step(qty, step_size)

        # 规整后仍需满足 min_qty，否则视为不可下单（尤其在 max_order_notional 很低时）
        if qty < min_qty:
            return Decimal("0")

        return qty

    def is_position_done(self, position_amt: Decimal, min_qty: Decimal, step_size: Decimal) -> bool:
        """
        检查仓位是否已完成（不可交易余量）

        条件：规整后为 0 或 < min_qty

        Args:
            position_amt: 当前仓位
            min_qty: 最小下单量
            step_size: 数量步进

        Returns:
            True 如果仓位已完成
        """
        abs_position = abs(position_amt)

        # 按 step_size 规整
        rounded = round_to_step(abs_position, step_size)

        return rounded == Decimal("0") or rounded < min_qty

    def reset_state(self, symbol: str, position_side: PositionSide) -> None:
        """
        重置执行状态

        Args:
            symbol: 交易对
            position_side: 仓位方向
        """
        key = f"{symbol}:{position_side.value}"
        if key in self._states:
            self._states[key] = SideExecutionState(
                symbol=symbol,
                position_side=position_side,
            )
