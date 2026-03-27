# Input: 执行引擎与 pytest 夹具
# Output: 状态机行为断言与执行反馈校验（含成交率、撤单/成交竞态、orphan 恢复与 reduce-only 挂单占仓回归）
# Pos: ExecutionEngine 测试用例与撤单竞态、panic close、自恢复安全回归
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
执行引擎模块测试
"""

import pytest
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock

from src.execution.engine import ExecutionEngine
from src.models import (
    ExitSignal,
    OrderIntent,
    OrderResult,
    OrderUpdate,
    OrderStatus,
    OrderSide,
    OrderType,
    PositionSide,
    ExecutionMode,
    ExecutionState,
    TimeInForce,
    SignalReason,
    StrategyMode,
    SignalExecutionPreference,
    QtyPolicy,
    MarketState,
    SymbolRules,
    ReduceOnlyBlockInfo,
)
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


@pytest.fixture
def mock_place_order():
    """Mock 下单函数"""
    return AsyncMock(return_value=OrderResult(
        success=True,
        order_id="test_order_123",
        status=OrderStatus.NEW,
        filled_qty=Decimal("0"),
        avg_price=Decimal("0"),
    ))


@pytest.fixture
def mock_cancel_order():
    """Mock 撤单函数"""
    return AsyncMock(return_value=OrderResult(
        success=True,
        order_id="test_order_123",
        status=OrderStatus.CANCELED,
    ))


@pytest.fixture
def engine(mock_place_order, mock_cancel_order):
    """创建测试引擎"""
    return ExecutionEngine(
        place_order=mock_place_order,
        cancel_order=mock_cancel_order,
        order_ttl_ms=800,
        repost_cooldown_ms=100,
        base_lot_mult=1,
        maker_price_mode="inside_spread_1tick",
        maker_n_ticks=1,
        max_mult=50,
        max_order_notional=Decimal("200"),
    )


@pytest.fixture
def symbol_rules():
    """交易规则"""
    return SymbolRules(
        symbol="BTC/USDT:USDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


@pytest.fixture
def market_state():
    """市场状态"""
    return MarketState(
        symbol="BTC/USDT:USDT",
        best_bid=Decimal("50000"),
        best_ask=Decimal("50001"),
        last_trade_price=Decimal("50000.5"),
        previous_trade_price=Decimal("50000"),
        last_update_ms=1000,
        is_ready=True,
    )


def make_reduce_only_block_info() -> ReduceOnlyBlockInfo:
    return ReduceOnlyBlockInfo(
        symbol="BTC/USDT:USDT",
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-0.003"),
        tradable_position_amt=Decimal("0.003"),
        blocking_qty=Decimal("0.003"),
        blocking_order_count=2,
        own_blocking_qty=Decimal("0.001"),
        own_blocking_order_count=1,
        external_blocking_qty=Decimal("0.002"),
        external_blocking_order_count=1,
    )


class TestExecutionEngineInit:
    """初始化测试"""

    def test_init_default(self, mock_place_order, mock_cancel_order):
        """测试默认初始化"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
        )
        assert engine.order_ttl_ms == 800
        assert engine.repost_cooldown_ms == 100
        assert engine.base_lot_mult == 1
        assert engine.maker_price_mode == "inside_spread_1tick"
        assert engine.maker_n_ticks == 1
        assert engine.max_mult == 50
        assert engine.max_order_notional == Decimal("200")

    def test_init_custom(self, mock_place_order, mock_cancel_order):
        """测试自定义初始化"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            order_ttl_ms=1000,
            repost_cooldown_ms=200,
            base_lot_mult=2,
            maker_price_mode="at_touch",
            maker_n_ticks=3,
            max_mult=100,
            max_order_notional=Decimal("500"),
        )
        assert engine.order_ttl_ms == 1000
        assert engine.repost_cooldown_ms == 200
        assert engine.base_lot_mult == 2
        assert engine.maker_price_mode == "at_touch"
        assert engine.maker_n_ticks == 3
        assert engine.max_mult == 100
        assert engine.max_order_notional == Decimal("500")


@pytest.mark.asyncio
async def test_fill_rate_feedback_low_sets_bucket():
    """成交率低时应标记为 low，但不覆盖 TTL。"""
    engine = ExecutionEngine(
        place_order=AsyncMock(),
        cancel_order=AsyncMock(),
        fill_rate_feedback_enabled=True,
        fill_rate_window_min=Decimal("1"),
        fill_rate_low_threshold=Decimal("0.5"),
        fill_rate_high_threshold=Decimal("0.9"),
        order_ttl_ms=800,
    )
    state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
    state.mode = ExecutionMode.MAKER_ONLY
    state.current_order_mode = ExecutionMode.MAKER_ONLY

    intent = OrderIntent(
        symbol="BTC/USDT:USDT",
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        qty=Decimal("0.01"),
        price=Decimal("100"),
        time_in_force=TimeInForce.GTX,
        reduce_only=True,
    )
    result = OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
    await engine.on_order_placed(intent, result, current_ms=0)
    result2 = OrderResult(success=True, order_id="2", status=OrderStatus.NEW)
    await engine.on_order_placed(intent, result2, current_ms=10)

    assert state.fill_rate_bucket == "low"
    assert state.fill_rate_ttl_override is None


@pytest.mark.asyncio
async def test_fill_rate_feedback_high_sets_ttl_override():
    """成交率高时应覆盖 TTL。"""
    engine = ExecutionEngine(
        place_order=AsyncMock(),
        cancel_order=AsyncMock(),
        fill_rate_feedback_enabled=True,
        fill_rate_window_min=Decimal("1"),
        fill_rate_low_threshold=Decimal("0.1"),
        fill_rate_high_threshold=Decimal("0.8"),
        order_ttl_ms=800,
    )
    state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
    state.mode = ExecutionMode.MAKER_ONLY
    state.current_order_mode = ExecutionMode.MAKER_ONLY

    intent = OrderIntent(
        symbol="BTC/USDT:USDT",
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        qty=Decimal("0.01"),
        price=Decimal("100"),
        time_in_force=TimeInForce.GTX,
        reduce_only=True,
    )

    for idx in range(3):
        order_id = str(idx + 1)
        result = OrderResult(success=True, order_id=order_id, status=OrderStatus.NEW)
        await engine.on_order_placed(intent, result, current_ms=idx * 10)
        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id=order_id,
            client_order_id="test",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.01"),
            avg_price=Decimal("100"),
            timestamp_ms=idx * 10 + 1,
            is_maker=True,
        )
        await engine.on_order_update(update, current_ms=idx * 10 + 1)

    assert state.fill_rate_bucket == "high"
    assert state.fill_rate_ttl_override == 1000


class TestStateManagement:
    """状态管理测试"""

    def test_get_state_creates_new(self, engine):
        """测试获取状态（不存在时创建）"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)

        assert state is not None
        assert state.symbol == "BTC/USDT:USDT"
        assert state.position_side == PositionSide.LONG
        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None
        assert state.maker_timeout_count == 0


class TestOrderbookPressureExecution:
    """盘口量模式执行测试"""

    @pytest.mark.asyncio
    async def test_on_signal_uses_fixed_qty_and_price_override(self, engine, symbol_rules, market_state):
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_ASK_PRESSURE_ACTIVE,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            execution_preference=SignalExecutionPreference.AGGRESSIVE,
            qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
            price_override=Decimal("50001"),
            cooldown_override_ms=1000,
            fixed_lot_mult=5,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.02"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is not None
        assert intent.qty == Decimal("0.005")
        assert intent.price == Decimal("50001")
        assert intent.time_in_force == TimeInForce.GTC

        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        assert state.current_order_execution_preference == SignalExecutionPreference.AGGRESSIVE
        assert state.current_order_strategy_mode == StrategyMode.ORDERBOOK_PRESSURE
        assert state.current_order_cooldown_ms_override == 1000

    @pytest.mark.asyncio
    async def test_on_signal_uses_post_only_tif_for_passive_price_override(self, engine, symbol_rules, market_state):
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            execution_preference=SignalExecutionPreference.PASSIVE,
            qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
            price_override=Decimal("49999.5"),
            ttl_override_ms=10000,
            cooldown_override_ms=0,
            fixed_lot_mult=1,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.02"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is not None
        assert intent.time_in_force == TimeInForce.GTX

    def test_compute_fixed_qty_clamps_to_position(self, engine):
        qty = engine.compute_fixed_qty(
            position_amt=Decimal("0.0024"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            lot_mult=5,
        )
        assert qty == Decimal("0.002")

    @pytest.mark.asyncio
    async def test_filled_order_enters_strategy_cooldown(self, engine, symbol_rules, market_state):
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_ASK_PRESSURE_ACTIVE,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            execution_preference=SignalExecutionPreference.AGGRESSIVE,
            qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
            price_override=Decimal("50001"),
            cooldown_override_ms=1000,
            fixed_lot_mult=1,
        )
        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.01"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )
        assert intent is not None

        result = OrderResult(
            success=True,
            order_id="filled-1",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50001"),
        )
        await engine.on_order_placed(intent, result, current_ms=1000)

        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        assert state.state == ExecutionState.COOLDOWN
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, 1999) is False
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, 2000) is True

    @pytest.mark.asyncio
    async def test_cancel_current_order_for_preempt_preserves_waiting_passive_context(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive-1"
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE
        state.current_order_cooldown_ms_override = 0

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1000,
        )

        assert cancelled is True
        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id == "passive-1"
        assert state.current_order_terminal_grace_until_ms == 6000

    @pytest.mark.asyncio
    async def test_cancel_current_order_for_preempt_allows_late_fill_update(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive-1"
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_reason = SignalReason.SHORT_BID_PRESSURE_PASSIVE.value
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE
        state.current_order_strategy_mode = StrategyMode.ORDERBOOK_PRESSURE
        state.current_order_cooldown_ms_override = 0

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1000,
        )

        assert cancelled is True

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="passive-1",
            client_order_id="client_123",
            side=OrderSide.BUY,
            position_side=PositionSide.SHORT,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1100,
        )
        await engine.on_order_update(update, current_ms=1100)

        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None

    @pytest.mark.asyncio
    async def test_cancel_current_order_for_preempt_handles_order_missing_after_concurrent_fill(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive-1"
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_reason = SignalReason.SHORT_BID_PRESSURE_PASSIVE.value
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE
        state.current_order_strategy_mode = StrategyMode.ORDERBOOK_PRESSURE
        state.current_order_cooldown_ms_override = 0

        async def cancel_side_effect(symbol: str, order_id: str) -> OrderResult:
            update = OrderUpdate(
                symbol=symbol,
                order_id=order_id,
                client_order_id="client_123",
                side=OrderSide.BUY,
                position_side=PositionSide.SHORT,
                status=OrderStatus.FILLED,
                filled_qty=Decimal("0.001"),
                avg_price=Decimal("50000"),
                timestamp_ms=1100,
            )
            await engine.on_order_update(update, current_ms=1100)
            return OrderResult(
                success=False,
                order_id=order_id,
                error_message='binanceusdm {"code":-2011,"msg":"Unknown order sent."}',
            )

        mock_cancel_order.side_effect = cancel_side_effect

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1000,
        )

        assert cancelled is True
        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None
        assert state.current_order_cancel_retry_after_ms == 0

    @pytest.mark.asyncio
    async def test_cancel_current_order_for_preempt_backoffs_after_cancel_failure(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive-1"
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE

        mock_cancel_order.return_value = OrderResult(
            success=False,
            order_id="passive-1",
            error_message="rate_limited: cancel_order",
        )

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1000,
        )
        assert cancelled is False
        assert state.state == ExecutionState.WAITING
        assert state.current_order_id == "passive-1"
        assert state.current_order_cancel_retry_after_ms == 1100

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1050,
        )
        assert cancelled is False
        assert mock_cancel_order.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_current_order_for_preempt_recovers_from_exception(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive-1"
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE

        mock_cancel_order.side_effect = Exception("network timeout")

        cancelled = await engine.cancel_current_order_for_preempt(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            current_ms=1000,
        )
        assert cancelled is False
        assert state.state == ExecutionState.WAITING
        assert state.current_order_id == "passive-1"
        assert state.current_order_cancel_retry_after_ms == 1100

    def test_get_state_returns_existing(self, engine):
        """测试获取状态（返回已存在）"""
        state1 = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state1.state = ExecutionState.WAITING
        state1.current_order_id = "order_123"

        state2 = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)

        assert state2.state == ExecutionState.WAITING
        assert state2.current_order_id == "order_123"

    def test_get_state_separate_by_side(self, engine):
        """测试 LONG/SHORT 状态独立"""
        long_state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        short_state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)

        long_state.state = ExecutionState.WAITING

        assert long_state.state == ExecutionState.WAITING
        assert short_state.state == ExecutionState.IDLE

    def test_reset_state(self, engine):
        """测试重置状态"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.maker_timeout_count = 5

        engine.reset_state("BTC/USDT:USDT", PositionSide.LONG)

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None
        assert state.maker_timeout_count == 0


class TestBuildMakerPrice:
    """Maker 价格计算测试"""

    def test_long_at_touch(self, mock_place_order, mock_cancel_order):
        """测试 LONG at_touch 模式"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="at_touch",
        )

        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # LONG 平仓 -> SELL，挂在卖方 at_touch = best_ask
        assert price == Decimal("50001")

    def test_long_inside_spread_1tick(self, engine):
        """测试 LONG inside_spread_1tick 模式"""
        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # LONG 平仓 -> SELL，best_ask - tick_size
        assert price == Decimal("50000.9")

    def test_long_custom_ticks(self, mock_place_order, mock_cancel_order):
        """测试 LONG custom_ticks 模式"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="custom_ticks",
            maker_n_ticks=3,
        )

        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # LONG 平仓 -> SELL，best_ask - 3*tick_size
        assert price == Decimal("50000.7")

    def test_short_at_touch(self, mock_place_order, mock_cancel_order):
        """测试 SHORT at_touch 模式"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="at_touch",
        )

        price = engine.build_maker_price(
            position_side=PositionSide.SHORT,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # SHORT 平仓 -> BUY，挂在买方 at_touch = best_bid
        assert price == Decimal("50000")

    def test_short_inside_spread_1tick(self, engine):
        """测试 SHORT inside_spread_1tick 模式"""
        price = engine.build_maker_price(
            position_side=PositionSide.SHORT,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # SHORT 平仓 -> BUY，best_bid + tick_size
        assert price == Decimal("50000.1")

    def test_short_custom_ticks(self, mock_place_order, mock_cancel_order):
        """测试 SHORT custom_ticks 模式"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="custom_ticks",
            maker_n_ticks=3,
        )

        price = engine.build_maker_price(
            position_side=PositionSide.SHORT,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            tick_size=Decimal("0.1"),
        )

        # SHORT 平仓 -> BUY，best_bid + 3*tick_size
        assert price == Decimal("50000.3")

    def test_price_rounding(self, engine):
        """测试价格规整"""
        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("50000.05"),
            best_ask=Decimal("50000.15"),
            tick_size=Decimal("0.1"),
        )

        # round_to_tick 向下会导致触碰 best_bid，Post-only 需要上移到 > best_bid 的最小 tick
        assert price == Decimal("50000.1")

    def test_inside_spread_with_1tick_spread_should_fallback_to_touch_long(self, engine):
        """测试 1 tick spread 时 LONG inside_spread_1tick 会回退到 at_touch"""
        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("8.111"),
            best_ask=Decimal("8.112"),
            tick_size=Decimal("0.001"),
        )
        assert price == Decimal("8.112")

    def test_inside_spread_with_1tick_spread_should_fallback_to_touch_short(self, engine):
        """测试 1 tick spread 时 SHORT inside_spread_1tick 会回退到 at_touch"""
        price = engine.build_maker_price(
            position_side=PositionSide.SHORT,
            best_bid=Decimal("8.111"),
            best_ask=Decimal("8.112"),
            tick_size=Decimal("0.001"),
        )
        assert price == Decimal("8.111")

    def test_maker_safety_ticks_long(self, mock_place_order, mock_cancel_order):
        """测试 maker_safety_ticks 会让 LONG SELL 远离 best_bid"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="inside_spread_1tick",
            maker_safety_ticks=2,
        )

        price = engine.build_maker_price(
            position_side=PositionSide.LONG,
            best_bid=Decimal("8.051"),
            best_ask=Decimal("8.052"),
            tick_size=Decimal("0.001"),
        )
        assert price == Decimal("8.053")  # >= best_bid + 2*tick

    def test_maker_safety_ticks_short(self, mock_place_order, mock_cancel_order):
        """测试 maker_safety_ticks 会让 SHORT BUY 远离 best_ask"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_price_mode="inside_spread_1tick",
            maker_safety_ticks=2,
        )

        price = engine.build_maker_price(
            position_side=PositionSide.SHORT,
            best_bid=Decimal("8.051"),
            best_ask=Decimal("8.052"),
            tick_size=Decimal("0.001"),
        )
        assert price == Decimal("8.050")  # <= best_ask - 2*tick


class TestComputeQty:
    """数量计算测试"""

    def test_basic_qty(self, engine):
        """测试基础数量计算"""
        qty = engine.compute_qty(
            position_amt=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),
        )

        # base_lot_mult=1, min_qty=0.001, base_qty=0.001
        assert qty == Decimal("0.001")

    def test_qty_limited_by_position(self, engine):
        """测试数量受仓位限制"""
        qty = engine.compute_qty(
            position_amt=Decimal("0.0005"),  # 小于 min_qty
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),
        )

        # 仓位小于 min_qty，返回 0
        assert qty == Decimal("0")

    def test_qty_limited_by_notional(self, engine):
        """测试数量受名义价值限制"""
        engine.max_order_notional = Decimal("100")  # 限制 100 USDT
        engine.base_lot_mult = 10  # 增大 base_qty 以触发 notional 限制

        qty = engine.compute_qty(
            position_amt=Decimal("1"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),  # max_qty = 100/50000 = 0.002
        )

        # base_qty = 0.001 * 10 = 0.01, limited by notional to 0.002
        assert qty == Decimal("0.002")

    def test_qty_with_larger_mult(self, mock_place_order, mock_cancel_order):
        """测试较大倍数"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            base_lot_mult=10,
            max_order_notional=Decimal("1000"),  # 更大的 notional 限制
        )

        qty = engine.compute_qty(
            position_amt=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),
        )

        # base_lot_mult=10, base_qty=0.01, max_notional/price = 1000/50000 = 0.02
        # min(0.01, 0.1, 0.02) = 0.01
        assert qty == Decimal("0.01")

    def test_qty_step_size_rounding(self, engine):
        """测试数量步进规整"""
        engine.max_order_notional = Decimal("150")
        engine.base_lot_mult = 10  # base_qty = 0.01

        qty = engine.compute_qty(
            position_amt=Decimal("1"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),  # max_qty = 150/50000 = 0.003
        )

        # base_qty = 0.01, limited by notional to 0.003
        assert qty == Decimal("0.003")

    def test_qty_uses_roi_and_accel_mult_and_caps_by_max_mult(self, engine):
        """测试 ROI/加速倍数叠加并受 max_mult 截断"""
        engine.base_lot_mult = 10
        engine.max_mult = 50
        engine.max_order_notional = Decimal("1000000")

        qty = engine.compute_qty(
            position_amt=Decimal("10"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("1"),
            roi_mult=6,
            accel_mult=4,
        )

        # final_mult = min(10*6*4, 50) = 50 => qty = 0.001*50 = 0.05
        assert qty == Decimal("0.05")

    def test_qty_returns_zero_when_notional_cap_below_min_qty(self, engine):
        """测试 max_order_notional 太小导致无法满足 min_qty 时返回 0"""
        engine.base_lot_mult = 1
        engine.max_order_notional = Decimal("20")

        qty = engine.compute_qty(
            position_amt=Decimal("1"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            last_trade_price=Decimal("50000"),
        )

        assert qty == Decimal("0")


class TestIsPositionDone:
    """仓位完成检查测试"""

    def test_zero_position(self, engine):
        """测试零仓位"""
        assert engine.is_position_done(
            position_amt=Decimal("0"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        ) is True

    def test_below_min_qty(self, engine):
        """测试低于最小数量"""
        assert engine.is_position_done(
            position_amt=Decimal("0.0005"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        ) is True

    def test_above_min_qty(self, engine):
        """测试高于最小数量"""
        assert engine.is_position_done(
            position_amt=Decimal("0.01"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        ) is False

    def test_negative_position(self, engine):
        """测试负仓位（SHORT）"""
        assert engine.is_position_done(
            position_amt=Decimal("-0.01"),
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        ) is False


class TestOnSignal:
    """信号处理测试"""

    @pytest.mark.asyncio
    async def test_signal_creates_intent(self, engine, symbol_rules, market_state):
        """测试信号创建下单意图"""
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is not None
        assert intent.symbol == "BTC/USDT:USDT"
        assert intent.side == OrderSide.SELL  # LONG 平仓 -> SELL
        assert intent.position_side == PositionSide.LONG
        assert intent.reduce_only is True

    @pytest.mark.asyncio
    async def test_signal_skipped_when_not_idle(self, engine, symbol_rules, market_state):
        """测试非 IDLE 状态跳过信号"""
        # 设置为 WAITING 状态
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is None

    @pytest.mark.asyncio
    async def test_signal_skipped_when_position_done(self, engine, symbol_rules, market_state):
        """测试仓位完成时跳过信号"""
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.0001"),  # 低于 min_qty
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is None

    @pytest.mark.asyncio
    async def test_short_signal_creates_buy(self, engine, symbol_rules, market_state):
        """测试 SHORT 信号创建 BUY 意图"""
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.1"),  # SHORT 仓位为负
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is not None
        assert intent.side == OrderSide.BUY  # SHORT 平仓 -> BUY


class TestOnOrderPlaced:
    """下单结果处理测试"""

    @pytest.mark.asyncio
    async def test_order_placed_success(self, engine):
        """测试下单成功"""
        # 先设置为 PLACING 状态
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.PLACING

        result = OrderResult(
            success=True,
            order_id="order_123",
            status=OrderStatus.NEW,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        await engine.on_order_placed(
            intent=intent,
            result=result,
            current_ms=1000,
        )

        assert state.state == ExecutionState.WAITING
        assert state.current_order_id == "order_123"

    @pytest.mark.asyncio
    async def test_order_placed_failed(self, engine):
        """测试下单失败"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.PLACING

        result = OrderResult(
            success=False,
            error_message="Insufficient balance",
        )

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        await engine.on_order_placed(
            intent=intent,
            result=result,
            current_ms=1000,
        )

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None
        assert state.current_order_placed_ms == 1000

    @pytest.mark.asyncio
    async def test_order_placed_immediately_filled(self, engine):
        """测试下单后立即成交"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.PLACING

        result = OrderResult(
            success=True,
            order_id="order_123",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
        )

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        await engine.on_order_placed(
            intent=intent,
            result=result,
            current_ms=1000,
        )

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None
        assert state.pending_fill_log is True
        assert state.last_completed_order_id == "order_123"
        assert state.last_completed_filled_qty == Decimal("0.001")
        assert state.last_completed_avg_price == Decimal("50000")

    @pytest.mark.asyncio
    async def test_order_placed_4118_latches_reduce_only_block(self, mock_place_order, mock_cancel_order):
        inspect_reduce_only_block = AsyncMock(return_value=make_reduce_only_block_info())
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            inspect_reduce_only_block=inspect_reduce_only_block,
        )

        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.PLACING

        result = OrderResult(
            success=False,
            error_code="-4118",
            error_message="ReduceOnly Order Failed",
        )
        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.BUY,
            position_side=PositionSide.SHORT,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        await engine.on_order_placed(intent=intent, result=result, current_ms=1000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.reduce_only_block is not None
        assert state.reduce_only_block.blocking_qty == Decimal("0.003")
        inspect_reduce_only_block.assert_awaited_once_with("BTC/USDT:USDT", PositionSide.SHORT)


class TestReduceOnlyBlock:
    """`-4118` 挂单占仓锁存测试。"""

    @pytest.mark.asyncio
    async def test_on_signal_skips_while_reduce_only_block_active_before_recheck(
        self,
        mock_place_order,
        mock_cancel_order,
        symbol_rules,
        market_state,
    ):
        inspect_reduce_only_block = AsyncMock()
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            inspect_reduce_only_block=inspect_reduce_only_block,
        )
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.reduce_only_block = make_reduce_only_block_info()
        state.reduce_only_block_recheck_after_ms = 2000

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_PRIMARY,
            timestamp_ms=1000,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            last_trade_price=market_state.last_trade_price,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.003"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1500,
        )

        assert intent is None
        assert state.state == ExecutionState.IDLE
        inspect_reduce_only_block.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_signal_rechecks_reduce_only_block_and_resumes_after_release(
        self,
        mock_place_order,
        mock_cancel_order,
        symbol_rules,
        market_state,
    ):
        inspect_reduce_only_block = AsyncMock(return_value=None)
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            inspect_reduce_only_block=inspect_reduce_only_block,
        )
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.reduce_only_block = make_reduce_only_block_info()
        state.reduce_only_block_recheck_after_ms = 1000

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_PRIMARY,
            timestamp_ms=1000,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            last_trade_price=market_state.last_trade_price,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.003"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1500,
        )

        assert intent is not None
        assert intent.side == OrderSide.BUY
        assert state.reduce_only_block is None
        assert state.state == ExecutionState.PLACING
        inspect_reduce_only_block.assert_awaited_once_with("BTC/USDT:USDT", PositionSide.SHORT)


class TestOnOrderUpdate:
    """订单更新处理测试"""

    @pytest.mark.asyncio
    async def test_order_filled(self, engine):
        """测试订单成交"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None

    @pytest.mark.asyncio
    async def test_order_canceled(self, engine):
        """测试订单取消"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.CANCELED,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)

        assert state.state == ExecutionState.COOLDOWN

    @pytest.mark.asyncio
    async def test_order_rejected(self, engine):
        """测试订单拒绝"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.REJECTED,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None
        assert state.current_order_placed_ms == 1000

    @pytest.mark.asyncio
    async def test_order_expired(self, engine):
        """测试订单过期"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.EXPIRED,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)

        assert state.state == ExecutionState.COOLDOWN

    @pytest.mark.asyncio
    async def test_ignore_other_order(self, engine):
        """测试忽略其他订单"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_456",  # 不同订单
            client_order_id="client_456",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)

        # 状态应该不变
        assert state.state == ExecutionState.WAITING
        assert state.current_order_id == "order_123"


class TestCheckTimeout:
    """超时检查测试"""

    @pytest.mark.asyncio
    async def test_timeout_not_triggered(self, engine):
        """测试未超时"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000

        # 800ms TTL，当前时间 1500ms，未超时
        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1500)

        assert result is False
        assert state.state == ExecutionState.WAITING

    @pytest.mark.asyncio
    async def test_timeout_triggered(self, engine, mock_cancel_order):
        """测试超时触发"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000

        # 800ms TTL，当前时间 2000ms，已超时
        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert result is True
        # 修复后：撤单成功直接进入 COOLDOWN（不等 WS 回执）
        assert state.state == ExecutionState.COOLDOWN
        assert state.maker_timeout_count == 1
        mock_cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_allows_late_cancel_update(self, engine):
        """测试撤单回执延迟仍可处理"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id == "order_123"
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=2100) is False
        assert state.current_order_id == "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.CANCELED,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
            timestamp_ms=2100,
        )
        await engine.on_order_update(update, current_ms=2100)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None

    @pytest.mark.asyncio
    async def test_timeout_with_zero_strategy_cooldown_keeps_context_until_grace(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.WAITING
        state.current_order_id = "passive_123"
        state.current_order_placed_ms = 1000
        state.current_order_cooldown_ms_override = 0
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.SHORT, current_ms=2000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id == "passive_123"
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, current_ms=2000) is False
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, current_ms=6999) is False
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, current_ms=7000) is True
        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None

    @pytest.mark.asyncio
    async def test_timeout_regular_order_keeps_context_until_grace(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id == "order_123"
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=2100) is False
        assert state.current_order_id == "order_123"
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=6999) is False
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=7000) is True
        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None

    @pytest.mark.asyncio
    async def test_timeout_order_missing_after_concurrent_fill_does_not_revert_to_waiting(
        self,
        engine,
        mock_cancel_order,
    ):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_reason = SignalReason.LONG_PRIMARY.value

        async def cancel_side_effect(symbol: str, order_id: str) -> OrderResult:
            update = OrderUpdate(
                symbol=symbol,
                order_id=order_id,
                client_order_id="client_123",
                side=OrderSide.SELL,
                position_side=PositionSide.LONG,
                status=OrderStatus.FILLED,
                filled_qty=Decimal("0.001"),
                avg_price=Decimal("50000"),
                timestamp_ms=2001,
                is_maker=True,
            )
            await engine.on_order_update(update, current_ms=2001)
            return OrderResult(
                success=False,
                order_id=order_id,
                error_message='binanceusdm {"code":-2011,"msg":"Unknown order sent."}',
            )

        mock_cancel_order.side_effect = cancel_side_effect

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert result is True
        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id is None
        assert state.current_order_cancel_retry_after_ms == 0

    @pytest.mark.asyncio
    async def test_timeout_cancel_failure_keeps_waiting_context_and_retries_after_backoff(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 1000

        mock_cancel_order.side_effect = [
            OrderResult(
                success=False,
                order_id="order_123",
                error_message="rate_limited: cancel_order",
            ),
            OrderResult(
                success=True,
                order_id="order_123",
                status=OrderStatus.CANCELED,
            ),
        ]

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)
        assert result is True
        assert state.state == ExecutionState.WAITING
        assert state.current_order_id == "order_123"
        assert state.current_order_cancel_retry_after_ms == 2100

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2050)
        assert result is False
        assert mock_cancel_order.await_count == 1

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2100)
        assert result is True
        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_id == "order_123"
        assert state.current_order_cancel_retry_after_ms == 0
        assert mock_cancel_order.await_count == 2

    @pytest.mark.asyncio
    async def test_timeout_skip_non_waiting(self, engine):
        """测试非 WAITING 状态跳过"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.IDLE

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert result is False

    @pytest.mark.asyncio
    async def test_on_signal_recovers_orphaned_canceling_state(self, engine, symbol_rules, market_state):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.CANCELING
        state.current_order_id = None

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
            strategy_mode=StrategyMode.ORDERBOOK_PRICE,
            execution_preference=SignalExecutionPreference.AGGRESSIVE,
            qty_policy=QtyPolicy.DYNAMIC,
            roi_mult=1,
            accel_mult=1,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.01"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is None
        assert state.state == ExecutionState.COOLDOWN
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.SHORT, current_ms=1100) is True

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.01"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1200,
        )

        assert intent is not None
        assert state.state == ExecutionState.PLACING

    @pytest.mark.asyncio
    async def test_on_signal_orphan_recovery_waits_for_recent_fill_reconcile(self, engine, symbol_rules, market_state):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.SHORT)
        state.state = ExecutionState.CANCELING
        state.current_order_id = None
        state.current_order_cooldown_ms_override = 0
        state.last_completed_order_id = "filled_123"
        state.last_completed_ms = 995

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.SHORT,
            reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            execution_preference=SignalExecutionPreference.PASSIVE,
            qty_policy=QtyPolicy.FIXED_MIN_QTY_MULT,
            fixed_lot_mult=1,
            price_override=Decimal("50000"),
            cooldown_override_ms=0,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.01"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is None
        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_cooldown_ms_override == engine.ws_fill_grace_ms
        assert engine.check_cooldown(
            "BTC/USDT:USDT",
            PositionSide.SHORT,
            current_ms=1000 + engine.ws_fill_grace_ms - 1,
        ) is False
        assert engine.check_cooldown(
            "BTC/USDT:USDT",
            PositionSide.SHORT,
            current_ms=1000 + engine.ws_fill_grace_ms,
        ) is True

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("-0.01"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1001 + engine.ws_fill_grace_ms,
        )

        assert intent is not None
        assert state.state == ExecutionState.PLACING


class TestModeRotation:
    """执行模式轮转测试"""

    @pytest.mark.asyncio
    async def test_escalate_to_aggressive_after_maker_timeouts(self, mock_place_order, mock_cancel_order):
        """测试 maker 超时达到阈值后升级到 AGGRESSIVE_LIMIT"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_timeouts_to_escalate=2,
        )

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_1"
        state.current_order_placed_ms = 0

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)
        assert state.mode == ExecutionMode.MAKER_ONLY

        # 模拟下一笔订单再次超时
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_2"
        state.current_order_placed_ms = 0

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)
        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT

    @pytest.mark.asyncio
    async def test_aggressive_intent_uses_gtc_and_best_price(self, engine, symbol_rules, market_state):
        """测试 AGGRESSIVE_LIMIT 下单意图的 tif/price"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            last_trade_price=market_state.last_trade_price,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        assert intent is not None
        assert intent.time_in_force == TimeInForce.GTC
        assert intent.price == market_state.best_bid

    @pytest.mark.asyncio
    async def test_deescalate_to_maker_after_aggressive_fill(self, engine):
        """测试 aggressive 成交后按阈值降级到 MAKER_ONLY"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1000,
        )

        await engine.on_order_update(update, current_ms=1000)
        assert state.mode == ExecutionMode.MAKER_ONLY

    @pytest.mark.asyncio
    async def test_deescalate_to_maker_after_aggressive_timeouts(self, mock_place_order, mock_cancel_order):
        """测试 aggressive 超时达到阈值后降级到 MAKER_ONLY"""
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            aggr_timeouts_to_deescalate=1,
        )

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_placed_ms = 0

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)
        assert state.mode == ExecutionMode.MAKER_ONLY


class TestCheckCooldown:
    """冷却检查测试"""

    def test_cooldown_not_ended(self, engine):
        """测试冷却未结束"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.COOLDOWN
        state.current_order_placed_ms = 1000

        # 100ms 冷却，当前 1050ms，未结束
        result = engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=1050)

        assert result is False
        assert state.state == ExecutionState.COOLDOWN

    def test_cooldown_ended(self, engine):
        """测试冷却结束"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.COOLDOWN
        state.current_order_placed_ms = 1000

        # 100ms 冷却，当前 1200ms，已结束
        result = engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=1200)

        assert result is True
        assert state.state == ExecutionState.IDLE

    def test_cooldown_skip_non_cooldown(self, engine):
        """测试非 COOLDOWN 状态跳过"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING

        result = engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert result is False
        assert state.state == ExecutionState.WAITING


class TestStateMachine:
    """状态机完整流程测试"""

    @pytest.mark.asyncio
    async def test_full_cycle(self, engine, symbol_rules, market_state, mock_place_order, mock_cancel_order):
        """测试完整状态机周期"""
        # 1. IDLE -> 收到信号
        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=Decimal("50000.5"),
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        assert state.state == ExecutionState.PLACING
        assert intent is not None

        # 2. PLACING -> 下单成功 -> WAITING
        result = OrderResult(
            success=True,
            order_id="order_123",
            status=OrderStatus.NEW,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )
        await engine.on_order_placed(intent=intent, result=result, current_ms=1100)

        assert state.state == ExecutionState.WAITING

        # 3. WAITING -> 超时 -> COOLDOWN（修复后：撤单成功直接进入 COOLDOWN）
        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert state.state == ExecutionState.COOLDOWN

        # 4. COOLDOWN -> grace 未结束前保持上下文，直到 grace 结束才回到 IDLE
        assert engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=2300) is False
        engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=7000)

        assert state.state == ExecutionState.IDLE


class TestFillCallback:
    """成交通知回调测试"""

    @pytest.mark.asyncio
    async def test_on_fill_callback_receives_mode_and_reason(self, mock_place_order, mock_cancel_order, symbol_rules, market_state):
        events = []

        def on_fill(symbol, position_side, order_id, mode, filled_qty, avg_price, reason, role, pnl, fee, fee_asset):  # noqa: ANN001
            events.append((symbol, position_side, order_id, mode, filled_qty, avg_price, reason, role, pnl, fee, fee_asset))

        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            on_fill=on_fill,
        )

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            last_trade_price=market_state.last_trade_price,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )
        assert intent is not None

        result = OrderResult(
            success=True,
            order_id="order_123",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
        )
        await engine.on_order_placed(intent=intent, result=result, current_ms=1100)

        # REST 成交后 on_fill 不触发，等待 WS 回执
        assert len(events) == 0

        # 模拟 WS 回执到来
        ws_update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            status=OrderStatus.FILLED,
            position_side=PositionSide.LONG,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1200,
            is_maker=True,
            realized_pnl=Decimal("-0.1234"),
            fee=Decimal("0.0001"),
            fee_asset="USDT",
        )
        await engine.on_order_update(ws_update, current_ms=1200)

        assert len(events) == 1
        assert events[0][0] == "BTC/USDT:USDT"
        assert events[0][1] == PositionSide.LONG
        assert events[0][3] == ExecutionMode.MAKER_ONLY
        assert events[0][6] == SignalReason.LONG_PRIMARY.value
        assert events[0][7] == "maker"
        assert events[0][8] == Decimal("-0.1234")
        assert events[0][9] == Decimal("0.0001")
        assert events[0][10] == "USDT"

    @pytest.mark.asyncio
    async def test_on_fill_callback_uses_order_mode_at_placement(self, mock_place_order, mock_cancel_order, symbol_rules, market_state):
        events = []

        def on_fill(symbol, position_side, order_id, mode, filled_qty, avg_price, reason, role, pnl, fee, fee_asset):  # noqa: ANN001
            events.append((symbol, position_side, order_id, mode, filled_qty, avg_price, reason, role, pnl, fee, fee_asset))

        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            on_fill=on_fill,
            aggr_fills_to_deescalate=1,
        )

        engine.set_mode("BTC/USDT:USDT", PositionSide.LONG, ExecutionMode.AGGRESSIVE_LIMIT, reason="test")

        signal = ExitSignal(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            reason=SignalReason.LONG_PRIMARY,
            timestamp_ms=1000,
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            last_trade_price=market_state.last_trade_price,
        )

        intent = await engine.on_signal(
            signal=signal,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
        )
        assert intent is not None

        result = OrderResult(
            success=True,
            order_id="order_123",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
        )
        await engine.on_order_placed(intent=intent, result=result, current_ms=1100)

        # REST 成交后 on_fill 不触发，等待 WS 回执
        assert len(events) == 0

        # 模拟 WS 回执到来
        ws_update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            status=OrderStatus.FILLED,
            position_side=PositionSide.LONG,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=1200,
            is_maker=False,
        )
        await engine.on_order_update(ws_update, current_ms=1200)

        assert len(events) == 1
        assert events[0][3] == ExecutionMode.AGGRESSIVE_LIMIT
        assert events[0][7] == "taker"

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        assert state.mode == ExecutionMode.MAKER_ONLY


class TestPanicClose:
    """强平兜底（panic close）测试"""

    def test_compute_panic_qty_basic(self, engine):
        qty = engine.compute_panic_qty(
            position_amt=Decimal("10"),
            min_qty=Decimal("0.1"),
            step_size=Decimal("0.1"),
            slice_ratio=Decimal("0.02"),
        )
        assert qty == Decimal("0.2")

    def test_compute_panic_qty_uses_min_qty_and_caps_by_position(self, engine):
        qty = engine.compute_panic_qty(
            position_amt=Decimal("0.15"),
            min_qty=Decimal("0.1"),
            step_size=Decimal("0.1"),
            slice_ratio=Decimal("0.01"),  # raw=0.0015 -> < min_qty
        )
        assert qty == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_on_panic_close_creates_risk_intent(self, engine, symbol_rules, market_state):
        intent = await engine.on_panic_close(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
            slice_ratio=Decimal("0.02"),
            reason="panic_close@test",
        )

        assert intent is not None
        assert intent.is_risk is True
        assert intent.reduce_only is True
        assert intent.position_side == PositionSide.LONG
        assert intent.side == OrderSide.SELL
        assert intent.order_type == OrderType.LIMIT
        assert intent.time_in_force == TimeInForce.GTX

        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        assert state.state == ExecutionState.PLACING
        assert state.current_order_is_risk is True

    @pytest.mark.asyncio
    async def test_on_panic_close_recovers_orphaned_canceling_state_immediately(
        self,
        engine,
        symbol_rules,
        market_state,
    ):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.CANCELING
        state.current_order_id = None

        intent = await engine.on_panic_close(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
            slice_ratio=Decimal("0.02"),
            reason="panic_close@test",
        )

        assert intent is not None
        assert state.state == ExecutionState.PLACING
        assert state.current_order_is_risk is True

    @pytest.mark.asyncio
    async def test_on_panic_close_waits_for_recent_fill_reconcile(
        self,
        engine,
        symbol_rules,
        market_state,
    ):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.CANCELING
        state.current_order_id = None
        state.last_completed_order_id = "filled_123"
        state.last_completed_ms = 995

        intent = await engine.on_panic_close(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.1"),
            rules=symbol_rules,
            market_state=market_state,
            current_ms=1000,
            slice_ratio=Decimal("0.02"),
            reason="panic_close@test",
        )

        assert intent is None
        assert state.state == ExecutionState.COOLDOWN
        assert state.current_order_cooldown_ms_override == engine.ws_fill_grace_ms

    @pytest.mark.asyncio
    async def test_risk_timeout_uses_ttl_override_without_decay(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_placed_ms = 0
        state.current_order_is_risk = True
        state.ttl_ms_override = 400

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=400)

        assert result is True
        # 修复后：撤单成功直接进入 COOLDOWN
        assert state.state == ExecutionState.COOLDOWN
        mock_cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_maker_timeouts_to_escalate_override(self, mock_place_order, mock_cancel_order):
        engine = ExecutionEngine(
            place_order=mock_place_order,
            cancel_order=mock_cancel_order,
            maker_timeouts_to_escalate=99,
        )
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_placed_ms = 0
        state.maker_timeouts_to_escalate_override = 1

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)
        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT

    @pytest.mark.asyncio
    async def test_partial_fill_resets_timeout_count_and_does_not_increment_on_timeout(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.MAKER_ONLY
        state.current_order_placed_ms = 0
        state.maker_timeout_count = 1  # 模拟此前连续超时计数

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=100,
        )
        await engine.on_order_update(update, current_ms=100)

        assert state.maker_timeout_count == 0
        assert state.current_order_filled_qty == Decimal("0.001")

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)
        assert state.maker_timeout_count == 0
        assert state.mode == ExecutionMode.MAKER_ONLY
        mock_cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_partial_fill_deescalates_to_maker_after_aggressive(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.current_order_placed_ms = 0

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=100,
        )
        await engine.on_order_update(update, current_ms=100)

        assert state.mode == ExecutionMode.MAKER_ONLY

    @pytest.mark.asyncio
    async def test_partial_fill_keeps_aggressive_under_liq_distance(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.liq_distance_active = True
        state.liq_distance_reason = "liq_distance"
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.current_order_placed_ms = 0

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=100,
        )
        await engine.on_order_update(update, current_ms=100)

        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT

    @pytest.mark.asyncio
    async def test_filled_order_keeps_aggressive_under_liq_distance(self, engine):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.liq_distance_active = True
        state.liq_distance_reason = "liq_distance"
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.current_order_placed_ms = 0
        state.current_order_reason = SignalReason.LONG_PRIMARY.value

        update = OrderUpdate(
            symbol="BTC/USDT:USDT",
            order_id="order_123",
            client_order_id="client_123",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000"),
            timestamp_ms=100,
        )
        await engine.on_order_update(update, current_ms=100)

        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT

    @pytest.mark.asyncio
    async def test_timeout_keeps_aggressive_under_liq_distance(self, engine, mock_cancel_order):
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.liq_distance_active = True
        state.liq_distance_reason = "liq_distance"
        state.state = ExecutionState.WAITING
        state.current_order_id = "order_123"
        state.current_order_mode = ExecutionMode.AGGRESSIVE_LIMIT
        state.current_order_placed_ms = 0

        await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=1000)

        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT
        assert state.state == ExecutionState.COOLDOWN
        mock_cancel_order.assert_called_once()
