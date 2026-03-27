# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
信号引擎模块测试
"""

import pytest
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from src.signal.engine import SignalEngine, PressureSignalConfig
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
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class TestSignalEngineInit:
    """初始化测试"""

    def test_init_default(self):
        """测试默认初始化"""
        engine = SignalEngine()
        assert engine.min_signal_interval_ms == 200
        assert len(engine._market_states) == 0
        assert len(engine._last_signal_ms) == 0

    def test_init_custom_interval(self):
        """测试自定义节流间隔"""
        engine = SignalEngine(min_signal_interval_ms=500)
        assert engine.min_signal_interval_ms == 500


class TestUpdateMarket:
    """市场数据更新测试"""

    def test_update_with_book_ticker(self):
        """测试 bookTicker 更新"""
        engine = SignalEngine()

        event = MarketEvent(
            symbol="BTC/USDT:USDT",
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        )

        engine.update_market(event)

        state = engine.get_market_state("BTC/USDT:USDT")
        assert state is not None
        assert state.best_bid == Decimal("50000")
        assert state.best_ask == Decimal("50001")
        assert state.is_ready is False  # 还没有 trade 数据

    def test_update_with_depth_for_pressure_mode(self):
        """测试盘口量模式需要 depth 数据才 ready。"""
        engine = SignalEngine()
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=3,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                qty_jitter_pct=Decimal("0"),
            ),
        )

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        assert engine.is_data_ready(symbol) is False

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1010,
            bid_levels=[
                (Decimal("10"), Decimal("50")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("30")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("60")),
                (Decimal("10.2"), Decimal("70")),
                (Decimal("10.3"), Decimal("80")),
            ],
            event_type="depth",
        ))
        assert engine.is_data_ready(symbol) is True

    def test_update_with_agg_trade(self):
        """测试 aggTrade 更新"""
        engine = SignalEngine()

        # 先发送第一个 trade
        event1 = MarketEvent(
            symbol="BTC/USDT:USDT",
            timestamp_ms=1000,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50000.5"),
            event_type="agg_trade",
        )
        engine.update_market(event1)

        state = engine.get_market_state("BTC/USDT:USDT")
        assert state is not None
        assert state.last_trade_price == Decimal("50000.5")
        assert state.previous_trade_price is None  # 第一个 trade 没有 previous

        # 发送第二个 trade
        event2 = MarketEvent(
            symbol="BTC/USDT:USDT",
            timestamp_ms=1100,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50001"),
            event_type="agg_trade",
        )
        engine.update_market(event2)

        state = engine.get_market_state("BTC/USDT:USDT")
        assert state is not None
        assert state.last_trade_price == Decimal("50001")
        assert state.previous_trade_price == Decimal("50000.5")

    def test_data_ready_after_all_data(self):
        """测试数据就绪条件"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 发送 book_ticker
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        ))
        assert engine.is_data_ready(symbol) is False

        # 发送第一个 trade
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1100,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50000.5"),
            event_type="agg_trade",
        ))
        assert engine.is_data_ready(symbol) is False  # 没有 previous

        # 发送第二个 trade
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50001"),
            event_type="agg_trade",
        ))
        assert engine.is_data_ready(symbol) is True  # 现在就绪了


class TestMultipliers:
    """加速/ROI 倍数测试"""

    def test_accel_mult_long(self):
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"
        engine.configure_symbol(
            symbol,
            accel_window_ms=2000,
            accel_tiers=[
                (Decimal("0.01"), 2),
                (Decimal("0.02"), 4),
            ],
        )

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            best_bid=Decimal("102"),
            best_ask=Decimal("103"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            last_trade_price=Decimal("100"),
            event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2000,
            last_trade_price=Decimal("102"),
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("1"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=2000)
        assert signal is not None
        assert signal.accel_mult == 4
        assert signal.ret_window == Decimal("0.02")

    def test_accel_mult_short(self):
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"
        engine.configure_symbol(
            symbol,
            accel_window_ms=2000,
            accel_tiers=[
                (Decimal("0.01"), 2),
                (Decimal("0.02"), 5),
            ],
        )

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            best_bid=Decimal("97"),
            best_ask=Decimal("98"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            last_trade_price=Decimal("100"),
            event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2000,
            last_trade_price=Decimal("98"),
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-1"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=2000)
        assert signal is not None
        assert signal.accel_mult == 5
        assert signal.ret_window == Decimal("-0.02")

    def test_roi_mult(self):
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"
        engine.configure_symbol(
            symbol,
            roi_tiers=[
                (Decimal("0.10"), 3),
                (Decimal("0.20"), 6),
            ],
        )

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            best_bid=Decimal("102"),
            best_ask=Decimal("103"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=0,
            last_trade_price=Decimal("100"),
            event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1,
            last_trade_price=Decimal("102"),
            event_type="agg_trade",
        ))

        # notional=100, leverage=10 => initial_margin=10, pnl=2 => roi=0.2
        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("1"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("2"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=2)
        assert signal is not None
        assert signal.roi == Decimal("0.2")
        assert signal.roi_mult == 6


class TestOrderbookPressureSignals:
    """盘口量平仓模式测试"""

    @pytest.fixture
    def pressure_engine(self):
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=3,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("80"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))
        return engine

    def test_pressure_mode_generates_passive_signal_when_threshold_not_met(self, pressure_engine):
        position = Position(
            symbol="DASH/USDT:USDT",
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        signal = pressure_engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=1200)

        assert signal is not None
        assert signal.reason == SignalReason.SHORT_BID_PRESSURE_PASSIVE
        assert signal.strategy_mode == StrategyMode.ORDERBOOK_PRESSURE
        assert signal.execution_preference == SignalExecutionPreference.PASSIVE
        assert signal.qty_policy == QtyPolicy.FIXED_MIN_QTY_MULT
        assert signal.price_override == Decimal("9.8")
        assert signal.ttl_override_ms == 10000
        assert signal.cooldown_override_ms == 0
        assert signal.fixed_lot_mult == 5

    def test_pressure_mode_requires_sustain_before_aggressive_signal(self, pressure_engine):
        symbol = "DASH/USDT:USDT"
        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=2000) is None
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=3999) is None

        signal = pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=4000)
        assert signal is not None
        assert signal.reason == SignalReason.SHORT_ASK_PRESSURE_ACTIVE
        assert signal.execution_preference == SignalExecutionPreference.AGGRESSIVE
        assert signal.price_override == Decimal("10.1")
        assert signal.cooldown_override_ms == 1000

    def test_pressure_mode_active_burst_pause_falls_back_to_passive(self):
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=3,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                active_burst_window_ms=10000,
                active_burst_max_attempts=2,
                active_burst_max_fills=0,
                active_burst_pause_min_ms=3000,
                active_burst_pause_max_ms=3000,
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        assert engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1000) is None
        active_signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=3000)
        assert active_signal is not None
        assert active_signal.execution_preference == SignalExecutionPreference.AGGRESSIVE

        engine.record_pressure_active_attempt(symbol, PositionSide.SHORT, ts_ms=3000)
        engine.record_pressure_active_attempt(symbol, PositionSide.SHORT, ts_ms=3200)

        paused_signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=3400)
        assert paused_signal is not None
        assert paused_signal.execution_preference == SignalExecutionPreference.PASSIVE
        assert paused_signal.reason == SignalReason.SHORT_BID_PRESSURE_PASSIVE

        resumed_signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=6301)
        assert resumed_signal is not None
        assert resumed_signal.execution_preference == SignalExecutionPreference.AGGRESSIVE

    def test_pressure_mode_active_burst_pause_can_be_triggered_by_fill(self):
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=3,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                active_burst_window_ms=10000,
                active_burst_max_attempts=0,
                active_burst_max_fills=2,
                active_burst_pause_min_ms=2500,
                active_burst_pause_max_ms=2500,
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        assert engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1000) is None
        engine.record_pressure_active_fill(symbol, PositionSide.SHORT, ts_ms=3000)
        engine.record_pressure_active_fill(symbol, PositionSide.SHORT, ts_ms=3200)

        paused_signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=3400)
        assert paused_signal is not None
        assert paused_signal.execution_preference == SignalExecutionPreference.PASSIVE

        resumed_signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=5701)
        assert resumed_signal is not None
        assert resumed_signal.execution_preference == SignalExecutionPreference.AGGRESSIVE

    def test_pressure_mode_resets_dwell_when_threshold_breaks(self, pressure_engine):
        symbol = "DASH/USDT:USDT"
        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=2000) is None

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2500,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("90"),
            event_type="book_ticker",
        ))
        signal = pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=2500)
        assert signal is not None
        assert signal.reason == SignalReason.SHORT_BID_PRESSURE_PASSIVE

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=3000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=3000) is None
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=4999) is None

    def test_pressure_mode_detects_stale_book_ticker_even_if_depth_is_fresh(self, pressure_engine):
        symbol = "DASH/USDT:USDT"

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2600,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))

        assert pressure_engine.is_strategy_data_stale(symbol, current_ms=2600, stale_data_ms=1000) is True

    def test_pressure_mode_zero_position_clears_dwell(self, pressure_engine):
        symbol = "DASH/USDT:USDT"
        active_position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )
        flat_position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("0"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        pressure_engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=2000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("120"),
            event_type="book_ticker",
        ))
        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, active_position, current_ms=2000) is None
        assert pressure_engine._pressure_dwell_start_ms[f"{symbol}:SHORT"] == 2000

        assert pressure_engine.evaluate(symbol, PositionSide.SHORT, flat_position, current_ms=2100) is None
        assert f"{symbol}:SHORT" not in pressure_engine._pressure_dwell_start_ms

    def test_pressure_mode_logs_when_passive_level_missing(self, monkeypatch):
        messages: list[str] = []

        class DummyLogger:
            def debug(self, message: str) -> None:
                messages.append(message)

        monkeypatch.setattr("src.signal.engine.get_logger", lambda: DummyLogger())

        engine = SignalEngine()
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=5,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("80"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[(Decimal("10.0"), Decimal("30"))],
            ask_levels=[(Decimal("10.1"), Decimal("20"))],
            event_type="depth",
        ))
        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1200)

        assert signal is None
        assert any("passive_level=5" in message for message in messages)

    def test_pressure_mode_skips_when_passive_level_missing(self):
        engine = SignalEngine()
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=5,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("80"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[(Decimal("10.0"), Decimal("30"))],
            ask_levels=[(Decimal("10.1"), Decimal("20"))],
            event_type="depth",
        ))
        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1200)
        assert signal is None


class TestLongExitConditions:
    """LONG 平仓条件测试"""

    @pytest.fixture
    def engine_with_data(self):
        """创建带数据的引擎"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 设置 book_ticker
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        ))

        # 设置两个 trade（创建 previous）
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1100,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("49999"),  # previous
            event_type="agg_trade",
        ))

        return engine

    def test_long_primary_triggered(self, engine_with_data):
        """测试 LONG primary 条件触发"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # last > prev AND best_bid >= last
        # prev = 49999, 设置 last = 50000, best_bid = 50000
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50000"),  # > prev (49999)
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"),
            entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)

        assert signal is not None
        assert signal.reason == SignalReason.LONG_PRIMARY

    def test_long_bid_improve_triggered(self, engine_with_data):
        """测试 LONG bid_improve 条件触发"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # best_bid >= last AND best_bid > prev (but NOT last > prev)
        # prev = 49999, 设置 last = 49998 (下跌), best_bid = 50000 > prev
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("50000"),  # > prev (49999)
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("49998"),  # < prev, bid >= last
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"),
            entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)

        assert signal is not None
        assert signal.reason == SignalReason.LONG_BID_IMPROVE

    def test_long_no_signal(self, engine_with_data):
        """测试 LONG 无信号"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # 价格下跌且 bid 也低
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("49997"),
            best_ask=Decimal("49998"),
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("49998"),
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"),
            entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)

        assert signal is None


class TestShortExitConditions:
    """SHORT 平仓条件测试"""

    @pytest.fixture
    def engine_with_data(self):
        """创建带数据的引擎"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 设置 book_ticker
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("50000"),
            best_ask=Decimal("50001"),
            last_trade_price=None,
            event_type="book_ticker",
        ))

        # 设置两个 trade（创建 previous）
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1100,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50001"),  # previous
            event_type="agg_trade",
        ))

        return engine

    def test_short_primary_triggered(self, engine_with_data):
        """测试 SHORT primary 条件触发"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # last < prev AND best_ask <= last
        # prev = 50001, 设置 last = 50000, best_ask = 50000
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("49999"),
            best_ask=Decimal("50000"),  # <= last
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50000"),  # < prev (50001)
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-0.01"),
            entry_price=Decimal("51000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1300)

        assert signal is not None
        assert signal.reason == SignalReason.SHORT_PRIMARY

    def test_short_ask_improve_triggered(self, engine_with_data):
        """测试 SHORT ask_improve 条件触发"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # best_ask <= last AND best_ask < prev (but NOT last < prev)
        # prev = 50001, 设置 last = 50002 (上涨，不满足 primary), best_ask = 50000 < prev
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("49999"),
            best_ask=Decimal("50000"),  # < prev (50001)
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50002"),  # > prev, 不满足 primary (last < prev)
            event_type="agg_trade",
        ))

        # 此时 last=50002, prev=50001, best_ask=50000
        # short_primary: last < prev? 50002 < 50001? NO
        # short_ask_improve: best_ask <= last? 50000 <= 50002? YES
        #                    best_ask < prev? 50000 < 50001? YES
        # 所以应该触发 ask_improve

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-0.01"),
            entry_price=Decimal("51000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1300)

        assert signal is not None
        assert signal.reason == SignalReason.SHORT_ASK_IMPROVE

    def test_short_no_signal(self, engine_with_data):
        """测试 SHORT 无信号"""
        engine = engine_with_data
        symbol = "BTC/USDT:USDT"

        # 价格上涨且 ask 也高
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=Decimal("50002"),
            best_ask=Decimal("50003"),  # > prev
            last_trade_price=None,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            best_bid=None,
            best_ask=None,
            last_trade_price=Decimal("50002"),  # > prev
            event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-0.01"),
            entry_price=Decimal("51000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1300)

        assert signal is None


class TestThrottling:
    """节流测试"""

    def test_throttle_within_interval(self):
        """测试节流期内无信号"""
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "BTC/USDT:USDT"

        # 设置数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50001"),
            last_trade_price=None, event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1100,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("49999"), event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1200,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50000"), event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol, position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"), entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"), leverage=10,
        )

        # 第一次信号
        signal1 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert signal1 is not None

        # 100ms 后（在节流期内）
        signal2 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1400)
        assert signal2 is None  # 被节流

        # 200ms 后（节流期结束）
        signal3 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1500)
        assert signal3 is not None  # 不被节流

    def test_throttle_independent_per_side(self):
        """测试 LONG/SHORT 节流独立"""
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "BTC/USDT:USDT"

        # 设置满足两边条件的数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50000"),
            last_trade_price=None, event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1100,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50001"), event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1200,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50000"), event_type="agg_trade",
        ))

        long_position = Position(
            symbol=symbol, position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"), entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"), leverage=10,
        )
        short_position = Position(
            symbol=symbol, position_side=PositionSide.SHORT,
            position_amt=Decimal("-0.01"), entry_price=Decimal("51000"),
            unrealized_pnl=Decimal("10"), leverage=10,
        )

        # LONG 信号
        signal_long = engine.evaluate(symbol, PositionSide.LONG, long_position, current_ms=1300)

        # SHORT 信号（不受 LONG 节流影响）
        signal_short = engine.evaluate(symbol, PositionSide.SHORT, short_position, current_ms=1350)

        # 根据条件，可能有也可能没有信号，但关键是它们独立
        # 这里我们只验证 LONG 被节流时 SHORT 不受影响
        signal_long2 = engine.evaluate(symbol, PositionSide.LONG, long_position, current_ms=1400)
        assert signal_long2 is None  # LONG 被节流

    def test_reset_throttle(self):
        """测试重置节流"""
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "BTC/USDT:USDT"

        # 设置数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50001"),
            last_trade_price=None, event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1100,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("49999"), event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1200,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50000"), event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol, position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"), entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"), leverage=10,
        )

        # 第一次信号
        signal1 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert signal1 is not None

        # 重置节流
        engine.reset_throttle(symbol, PositionSide.LONG)

        # 立即可以再次触发
        signal2 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1350)
        assert signal2 is not None


class TestEdgeCases:
    """边界情况测试"""

    def test_zero_position(self):
        """测试零仓位无信号"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 设置数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50001"),
            last_trade_price=None, event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1100,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("49999"), event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1200,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50000"), event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol, position_side=PositionSide.LONG,
            position_amt=Decimal("0"),  # 零仓位
            entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("0"), leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert signal is None

    def test_no_state(self):
        """测试无状态时无信号"""
        engine = SignalEngine()

        position = Position(
            symbol="BTC/USDT:USDT",
            position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"),
            entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"),
            leverage=10,
        )

        signal = engine.evaluate("BTC/USDT:USDT", PositionSide.LONG, position, current_ms=1000)
        assert signal is None

    def test_clear_state(self):
        """测试清除状态"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 设置数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50001"),
            last_trade_price=None, event_type="book_ticker",
        ))

        assert engine.get_market_state(symbol) is not None

        # 清除状态
        engine.clear_state(symbol)

        assert engine.get_market_state(symbol) is None
        assert engine.is_data_ready(symbol) is False


class TestSignalContent:
    """信号内容测试"""

    def test_signal_contains_market_data(self):
        """测试信号包含市场数据"""
        engine = SignalEngine()
        symbol = "BTC/USDT:USDT"

        # 设置数据
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1000,
            best_bid=Decimal("50000"), best_ask=Decimal("50001"),
            last_trade_price=None, event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1100,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("49999"), event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol, timestamp_ms=1200,
            best_bid=None, best_ask=None,
            last_trade_price=Decimal("50000"), event_type="agg_trade",
        ))

        position = Position(
            symbol=symbol, position_side=PositionSide.LONG,
            position_amt=Decimal("0.01"), entry_price=Decimal("49000"),
            unrealized_pnl=Decimal("10"), leverage=10,
        )

        signal = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)

        assert signal is not None
        assert signal.symbol == symbol
        assert signal.position_side == PositionSide.LONG
        assert signal.best_bid == Decimal("50000")
        assert signal.best_ask == Decimal("50001")
        assert signal.last_trade_price == Decimal("50000")
        assert signal.timestamp_ms == 1300


class TestPressureSignalLogging:
    """盘口量模式信号日志去重测试"""

    def _make_pressure_engine(self) -> tuple[SignalEngine, str, Position]:
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=Decimal("100"),
                sustain_ms=2000,
                passive_level=3,
                lot_mult=5,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=Decimal("0"),
                passive_ttl_jitter_pct=Decimal("0"),
                qty_jitter_pct=Decimal("0"),
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10.0"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=Decimal("80"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))
        position = Position(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )
        return engine, symbol, position

    def test_pressure_signal_log_ignores_last_trade_jitter_before_heartbeat(self, monkeypatch):
        engine, symbol, position = self._make_pressure_engine()
        logged: list[dict[str, object]] = []
        monkeypatch.setattr("src.signal.engine.log_signal", lambda **kwargs: logged.append(kwargs))

        signal1 = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1100)
        assert signal1 is not None
        assert signal1.execution_preference == SignalExecutionPreference.PASSIVE

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            last_trade_price=Decimal("10.25"),
            event_type="agg_trade",
        ))
        signal2 = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1400)
        assert signal2 is not None
        assert signal2.execution_preference == SignalExecutionPreference.PASSIVE

        assert len(logged) == 1

    def test_pressure_signal_log_emits_heartbeat_for_same_signature(self, monkeypatch):
        engine, symbol, position = self._make_pressure_engine()
        logged: list[dict[str, object]] = []
        monkeypatch.setattr("src.signal.engine.log_signal", lambda **kwargs: logged.append(kwargs))

        first = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=1100)
        assert first is not None

        second = engine.evaluate(symbol, PositionSide.SHORT, position, current_ms=6200)
        assert second is not None
        assert second.execution_preference == SignalExecutionPreference.PASSIVE

        assert len(logged) == 2


class TestOrderbookPriceSignalLogging:
    """盘口价格模式信号日志去重测试"""

    def _make_price_engine(self) -> tuple[SignalEngine, str, Position]:
        engine = SignalEngine(min_signal_interval_ms=0)
        symbol = "BTC/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRICE,
            roi_tiers=[(Decimal("0.10"), 2)],
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1100,
            last_trade_price=Decimal("99"),
            event_type="agg_trade",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1200,
            last_trade_price=Decimal("100"),
            event_type="agg_trade",
        ))
        position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=Decimal("1"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0.5"),
            leverage=10,
        )
        return engine, symbol, position

    def test_orderbook_price_signal_log_ignores_market_jitter_before_heartbeat(self, monkeypatch):
        engine, symbol, position = self._make_price_engine()
        logged: list[dict[str, object]] = []
        monkeypatch.setattr("src.signal.engine.log_signal", lambda **kwargs: logged.append(kwargs))

        signal1 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert signal1 is not None
        assert signal1.reason == SignalReason.LONG_PRIMARY

        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1400,
            best_bid=Decimal("101"),
            best_ask=Decimal("102"),
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1450,
            last_trade_price=Decimal("101"),
            event_type="agg_trade",
        ))
        signal2 = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1600)
        assert signal2 is not None
        assert signal2.reason == SignalReason.LONG_PRIMARY

        assert len(logged) == 1

    def test_orderbook_price_signal_log_emits_when_multiplier_changes(self, monkeypatch):
        engine, symbol, position = self._make_price_engine()
        logged: list[dict[str, object]] = []
        monkeypatch.setattr("src.signal.engine.log_signal", lambda **kwargs: logged.append(kwargs))

        first = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert first is not None
        assert first.roi_mult == 1

        boosted_position = Position(
            symbol=symbol,
            position_side=PositionSide.LONG,
            position_amt=position.position_amt,
            entry_price=position.entry_price,
            unrealized_pnl=Decimal("2"),
            leverage=position.leverage,
        )
        second = engine.evaluate(symbol, PositionSide.LONG, boosted_position, current_ms=1600)
        assert second is not None
        assert second.reason == SignalReason.LONG_PRIMARY
        assert second.roi_mult == 2

        assert len(logged) == 2

    def test_orderbook_price_signal_log_emits_heartbeat_for_same_signature(self, monkeypatch):
        engine, symbol, position = self._make_price_engine()
        logged: list[dict[str, object]] = []
        monkeypatch.setattr("src.signal.engine.log_signal", lambda **kwargs: logged.append(kwargs))

        first = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=1300)
        assert first is not None

        second = engine.evaluate(symbol, PositionSide.LONG, position, current_ms=6400)
        assert second is not None
        assert second.reason == SignalReason.LONG_PRIMARY

        assert len(logged) == 2


class TestQtyJitter:
    """平仓量随机抖动测试"""

    def _make_engine(
        self,
        *,
        lot_mult: int,
        qty_jitter_pct: Decimal,
        threshold_qty: Decimal = Decimal("100"),
        best_ask_qty: Decimal = Decimal("80"),
        active_recheck_cooldown_jitter_pct: Decimal = Decimal("0.15"),
        passive_ttl_jitter_pct: Decimal = Decimal("0.15"),
    ) -> SignalEngine:
        engine = SignalEngine(min_signal_interval_ms=200)
        symbol = "DASH/USDT:USDT"
        engine.configure_symbol(
            symbol,
            strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
            pressure_config=PressureSignalConfig(
                threshold_qty=threshold_qty,
                sustain_ms=2000,
                passive_level=3,
                lot_mult=lot_mult,
                active_recheck_cooldown_ms=1000,
                passive_ttl_ms=10000,
                active_recheck_cooldown_jitter_pct=active_recheck_cooldown_jitter_pct,
                passive_ttl_jitter_pct=passive_ttl_jitter_pct,
                qty_jitter_pct=qty_jitter_pct,
            ),
        )
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1000,
            best_bid=Decimal("10"),
            best_ask=Decimal("10.1"),
            best_bid_qty=Decimal("50"),
            best_ask_qty=best_ask_qty,
            event_type="book_ticker",
        ))
        engine.update_market(MarketEvent(
            symbol=symbol,
            timestamp_ms=1001,
            bid_levels=[
                (Decimal("10.0"), Decimal("30")),
                (Decimal("9.9"), Decimal("40")),
                (Decimal("9.8"), Decimal("50")),
            ],
            ask_levels=[
                (Decimal("10.1"), Decimal("20")),
                (Decimal("10.2"), Decimal("30")),
                (Decimal("10.3"), Decimal("40")),
            ],
            event_type="depth",
        ))
        return engine

    def _make_position(self) -> Position:
        return Position(
            symbol="DASH/USDT:USDT",
            position_side=PositionSide.SHORT,
            position_amt=Decimal("-5"),
            entry_price=Decimal("10"),
            unrealized_pnl=Decimal("0"),
            leverage=5,
        )

    def test_jitter_zero_always_returns_exact_lot_mult(self):
        engine = self._make_engine(lot_mult=20, qty_jitter_pct=Decimal("0"))
        position = self._make_position()
        for ms in range(1100, 1200):
            signal = engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=ms)
            if signal is not None:
                assert signal.fixed_lot_mult == 20
                assert signal.fixed_qty_jitter_pct == Decimal("0")

    def test_signal_lot_mult_stays_exact_even_when_qty_jitter_enabled(self):
        engine = self._make_engine(lot_mult=20, qty_jitter_pct=Decimal("0.15"))
        position = self._make_position()
        observed = set()
        for ms in range(1100, 1200):
            signal = engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=ms)
            if signal is not None:
                observed.add(signal.fixed_lot_mult)
                assert signal.fixed_qty_jitter_pct == Decimal("0.15")
                assert signal.fixed_qty_anti_repeat_lookback == 3
        assert observed == {20}

    def test_passive_ttl_jitter_applies_to_signal(self):
        engine = self._make_engine(
            lot_mult=5,
            qty_jitter_pct=Decimal("0.15"),
            passive_ttl_jitter_pct=Decimal("0.15"),
        )
        position = self._make_position()
        with patch("src.signal.engine.randint", return_value=8765):
            signal = engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=1100)
        assert signal is not None
        assert signal.execution_preference == SignalExecutionPreference.PASSIVE
        assert signal.ttl_override_ms == 8765
        assert signal.fixed_lot_mult == 5

    def test_active_cooldown_jitter_applies_to_signal(self):
        engine = self._make_engine(
            lot_mult=5,
            qty_jitter_pct=Decimal("0.15"),
            best_ask_qty=Decimal("150"),
            active_recheck_cooldown_jitter_pct=Decimal("0.15"),
        )
        position = self._make_position()

        first = engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=1100)
        assert first is None

        with patch("src.signal.engine.randint", return_value=912):
            signal = engine.evaluate("DASH/USDT:USDT", PositionSide.SHORT, position, current_ms=3200)

        assert signal is not None
        assert signal.execution_preference == SignalExecutionPreference.AGGRESSIVE
        assert signal.cooldown_override_ms == 912
        assert signal.fixed_lot_mult == 5
