# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果与状态机行为验证
# Pos: 测试用例
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
    MarketState,
    SymbolRules,
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

        assert state.state == ExecutionState.IDLE
        assert state.current_order_id is None


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

        assert state.state == ExecutionState.IDLE
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

        assert state.state == ExecutionState.IDLE

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
    async def test_timeout_skip_non_waiting(self, engine):
        """测试非 WAITING 状态跳过"""
        state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
        state.state = ExecutionState.IDLE

        result = await engine.check_timeout("BTC/USDT:USDT", PositionSide.LONG, current_ms=2000)

        assert result is False


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

        # 4. COOLDOWN -> 冷却结束 -> IDLE
        engine.check_cooldown("BTC/USDT:USDT", PositionSide.LONG, current_ms=2300)

        assert state.state == ExecutionState.IDLE


class TestFillCallback:
    """成交通知回调测试"""

    @pytest.mark.asyncio
    async def test_on_fill_callback_receives_mode_and_reason(self, mock_place_order, mock_cancel_order, symbol_rules, market_state):
        events = []

        def on_fill(symbol, position_side, mode, filled_qty, avg_price, reason):  # noqa: ANN001
            events.append((symbol, position_side, mode, filled_qty, avg_price, reason))

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

        assert len(events) == 1
        assert events[0][0] == "BTC/USDT:USDT"
        assert events[0][1] == PositionSide.LONG
        assert events[0][2] == ExecutionMode.MAKER_ONLY
        assert events[0][5] == SignalReason.LONG_PRIMARY.value

    @pytest.mark.asyncio
    async def test_on_fill_callback_uses_order_mode_at_placement(self, mock_place_order, mock_cancel_order, symbol_rules, market_state):
        events = []

        def on_fill(symbol, position_side, mode, filled_qty, avg_price, reason):  # noqa: ANN001
            events.append((symbol, position_side, mode, filled_qty, avg_price, reason))

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

        assert len(events) == 1
        assert events[0][2] == ExecutionMode.AGGRESSIVE_LIMIT

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
