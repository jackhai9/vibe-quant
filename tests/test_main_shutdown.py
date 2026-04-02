# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例（main.py 关闭行为 + 命令解析 + 保护止损调度竞态）
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
main.py 应用关闭行为 & 命令解析 & 保护止损调度竞态测试
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import Application
from src.models import AccountUpdateEvent, MarketEvent, Position, PositionSide
from src.models import (
    ExecutionMode,
    ExecutionState,
    ExitSignal,
    MarketState,
    OrderSide,
    OrderStatus,
    OrderResult,
    OrderUpdate,
    SignalExecutionPreference,
    SignalReason,
    StrategyMode,
    SymbolRules,
    RiskFlag,
)
from src.execution.engine import ExecutionEngine
from src.notify.pause_manager import PauseManager
from src.stats.pressure_stats import (
    PressurePeriodicReport,
    PressureRecap,
    PressureStatsCollector,
    PressureWindowReport,
    RegimeLogEntry,
)
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class DummyWS:
    def __init__(self):
        self.connect_started = False
        self.disconnect_called = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self.connect_started = True
        await self._block.wait()

    async def disconnect(self) -> None:
        self.disconnect_called = True
        self._block.set()


@pytest.mark.asyncio
async def test_evaluate_symbol_side_resets_pressure_dwell_on_market_stale():
    app = Application.__new__(Application)
    app._pressure_trigger_signatures = {}
    app.signal_engine = MagicMock()
    app.signal_engine.is_strategy_data_stale.return_value = False
    app.config_loader = MagicMock()
    app.config_loader.config.global_.ws.stale_data_ms = 1000
    app._calibrating = False
    app.pause_manager = MagicMock()
    app.pause_manager.is_paused.return_value = False
    app._positions = {
        "DASH/USDT:USDT": {
            PositionSide.SHORT: Position(
                symbol="DASH/USDT:USDT",
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-5"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            )
        }
    }
    app.market_ws = MagicMock()
    app.market_ws.is_stale.return_value = True

    await app._evaluate_symbol_side("DASH/USDT:USDT", PositionSide.SHORT)

    app.signal_engine.reset_pressure_dwell.assert_called_once_with(
        "DASH/USDT:USDT",
        PositionSide.SHORT,
        reason="market_stale",
    )


@pytest.mark.asyncio
async def test_evaluate_symbol_side_resets_pressure_dwell_on_flat_position():
    app = Application.__new__(Application)
    app._pressure_trigger_signatures = {}
    app.signal_engine = MagicMock()
    app.config_loader = MagicMock()
    app._calibrating = False
    app.pause_manager = MagicMock()
    app.pause_manager.is_paused.return_value = False
    app._positions = {
        "DASH/USDT:USDT": {
            PositionSide.SHORT: Position(
                symbol="DASH/USDT:USDT",
                position_side=PositionSide.SHORT,
                position_amt=Decimal("0"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            )
        }
    }
    app.market_ws = MagicMock()

    await app._evaluate_symbol_side("DASH/USDT:USDT", PositionSide.SHORT)

    app.signal_engine.reset_pressure_dwell.assert_called_once_with(
        "DASH/USDT:USDT",
        PositionSide.SHORT,
        reason="position_flat",
    )


def test_handle_pressure_regime_updates_only_notifies_degrading_or_failed():
    app = Application.__new__(Application)
    app._running = True
    app._pressure_regime_entries = {}
    app.config_loader = MagicMock()
    app.config_loader.config.global_.telegram.enabled = True
    app.config_loader.config.global_.telegram.events.on_risk_trigger = True
    app.telegram_notifier = MagicMock()
    app.telegram_notifier.enabled = True
    app.telegram_notifier.notify_pressure_regime = MagicMock(side_effect=lambda **_: asyncio.sleep(0))
    app._schedule_telegram = MagicMock()

    app._handle_pressure_regime_updates(
        [
            RegimeLogEntry(
                symbol="DASH/USDT:USDT",
                side="LONG",
                window_label="5m",
                regime="effective",
                prev_regime="recovering",
                score=5,
                samples=12,
            ),
            RegimeLogEntry(
                symbol="DASH/USDT:USDT",
                side="LONG",
                window_label="5m",
                regime="degrading",
                prev_regime="effective",
                score=1,
                samples=12,
            ),
            RegimeLogEntry(
                symbol="DASH/USDT:USDT",
                side="SHORT",
                window_label="5m",
                regime="failed",
                prev_regime="degrading",
                score=0,
                samples=12,
            ),
        ]
    )

    assert app._pressure_regime_entries[("DASH/USDT:USDT", "LONG")].regime == "degrading"
    assert app._pressure_regime_entries[("DASH/USDT:USDT", "SHORT")].regime == "failed"
    assert app.telegram_notifier.notify_pressure_regime.call_count == 2
    app.telegram_notifier.notify_pressure_regime.assert_any_call(
        symbol="DASH/USDT:USDT",
        position_side="LONG",
        regime="degrading",
        window="5m",
        prev_regime="effective",
        score=1,
    )
    app.telegram_notifier.notify_pressure_regime.assert_any_call(
        symbol="DASH/USDT:USDT",
        position_side="SHORT",
        regime="failed",
        window="5m",
        prev_regime="degrading",
        score=0,
    )
    assert app._schedule_telegram.call_count == 2
    for call in app._schedule_telegram.call_args_list:
        call.args[0].close()


def test_restore_pressure_regime_state_restores_recent_cache():
    source = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
    for i in range(12):
        source._record_regime_snapshot(
            "DASH/USDT:USDT",
            "LONG",
            ts_ms=(i + 1) * 300_000,
            active_triggers=10 + i,
            passive_triggers=120 - i,
            active_attempts=6 + i,
            passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
            price_change_pct=Decimal(f"{i + 1}.00"),
        )
    regime = source._evaluate_regime("DASH/USDT:USDT", "LONG")
    assert regime is not None

    payload = source.export_regime_state(current_ms=3_600_000)
    payload["regime_entries"] = {
        "DASH/USDT:USDT|LONG": {
            "symbol": "DASH/USDT:USDT",
            "side": "LONG",
            "window_label": "5m",
            "regime": regime.state,
            "prev_regime": regime.prev_state,
            "score": regime.score,
            "samples": regime.samples,
        }
    }

    with TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "pressure_regime_state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        app = Application.__new__(Application)
        app._log_dir = Path(tmpdir)
        app._pressure_stats = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        app._pressure_regime_entries = {}
        app.config_loader = MagicMock()
        app.config_loader.config.global_.stats.pressure_regime_resume_enabled = True
        app.config_loader.config.global_.stats.pressure_regime_resume_max_gap_ms = 900_000

        with patch("src.main.current_time_ms", return_value=3_660_000):
            app._restore_pressure_regime_state()

        restored_entry = app._pressure_regime_entries[("DASH/USDT:USDT", "LONG")]
        assert restored_entry.window_label == "5m"
        assert restored_entry.regime == regime.state
        restored_regime = app._pressure_stats._evaluate_regime("DASH/USDT:USDT", "LONG")
        assert restored_regime is not None
        assert restored_regime.samples == 12


@pytest.mark.asyncio
async def test_handle_cmd_status_includes_latest_pressure_regime():
    app = Application.__new__(Application)
    app._running = True
    app._started_at = datetime.now()
    app._calibrating = False
    app.pause_manager = MagicMock()
    app.pause_manager.get_status.return_value = {
        "global_paused": False,
        "global_paused_at": None,
        "global_resume_at": None,
        "paused_symbols": {},
        "symbol_resume_at": {},
    }
    app._active_symbols = {"DASH/USDT:USDT"}
    app._symbol_configs = {
        "DASH/USDT:USDT": MagicMock(strategy_mode=StrategyMode.ORDERBOOK_PRESSURE),
    }
    app._positions = {
        "DASH/USDT:USDT": {
            PositionSide.LONG: Position(
                symbol="DASH/USDT:USDT",
                position_side=PositionSide.LONG,
                position_amt=Decimal("1.5"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            ),
        }
    }
    state = MagicMock()
    state.mode.value = "MAKER_ONLY"
    state.state.value = "WAITING"
    engine = MagicMock()
    engine.get_state.return_value = state
    app.execution_engines = {"DASH/USDT:USDT": engine}
    app._pressure_regime_entries = {
        ("DASH/USDT:USDT", "LONG"): RegimeLogEntry(
            symbol="DASH/USDT:USDT",
            side="LONG",
            window_label="5m",
            regime="failed",
            prev_regime="degrading",
            score=0,
            samples=12,
        )
    }

    text = await app._handle_cmd_status("")

    assert "盘口量状态:" in text
    assert "DASH/USDT LONG: 5m 失效 (score=0, samples=12)" in text


def test_restore_pressure_regime_state_filters_stale_regime_entries():
    source = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
    for i in range(12):
        source._record_regime_snapshot(
            "DASH/USDT:USDT",
            "LONG",
            ts_ms=10_800_000 + (i + 1) * 300_000,
            active_triggers=10 + i,
            passive_triggers=120 - i,
            active_attempts=6 + i,
            passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
            price_change_pct=Decimal(f"{i + 1}.00"),
        )
        source._record_regime_snapshot(
            "FORM/USDT:USDT",
            "LONG",
            ts_ms=(i + 1) * 300_000,
            active_triggers=5 + i,
            passive_triggers=150 - i,
            active_attempts=3 + i,
            passive_fill_rate=Decimal(f"0.{30 + i:03d}"),
            price_change_pct=Decimal(f"{i + 2}.00"),
        )
    dash_regime = source._evaluate_regime("DASH/USDT:USDT", "LONG")
    form_regime = source._evaluate_regime("FORM/USDT:USDT", "LONG")
    assert dash_regime is not None
    assert form_regime is not None

    payload = source.export_regime_state(current_ms=14_400_000)
    payload["regime_entries"] = {
        "DASH/USDT:USDT|LONG": {
            "symbol": "DASH/USDT:USDT",
            "side": "LONG",
            "window_label": "5m",
            "regime": dash_regime.state,
            "prev_regime": dash_regime.prev_state,
            "score": dash_regime.score,
            "samples": dash_regime.samples,
        },
        "FORM/USDT:USDT|LONG": {
            "symbol": "FORM/USDT:USDT",
            "side": "LONG",
            "window_label": "5m",
            "regime": form_regime.state,
            "prev_regime": form_regime.prev_state,
            "score": form_regime.score,
            "samples": form_regime.samples,
        },
    }

    with TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "pressure_regime_state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        app = Application.__new__(Application)
        app._log_dir = Path(tmpdir)
        app._pressure_stats = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        app._pressure_regime_entries = {}
        app.config_loader = MagicMock()
        app.config_loader.config.global_.stats.pressure_regime_resume_enabled = True
        app.config_loader.config.global_.stats.pressure_regime_resume_max_gap_ms = 900_000

        with patch("src.main.current_time_ms", return_value=14_460_000):
            app._restore_pressure_regime_state()

        assert ("DASH/USDT:USDT", "LONG") in app._pressure_regime_entries
        assert ("FORM/USDT:USDT", "LONG") not in app._pressure_regime_entries


@pytest.mark.asyncio
async def test_pressure_stats_loop_uses_configured_regime_window_interval():
    app = Application.__new__(Application)
    app._running = True
    app.config_loader = MagicMock()
    app.config_loader.config.global_.stats.pressure_regime_window_ms = 60_000
    app.config_loader.config.global_.stats.pressure_regime_samples = 12
    app._pressure_stats = MagicMock()
    app.pause_manager = MagicMock()
    app.pause_manager.is_paused.return_value = False
    app._handle_pressure_regime_updates = MagicMock()
    app._log_periodic_pressure_reports = MagicMock()
    app._save_pressure_regime_state = MagicMock()

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        app._running = False

    with patch("src.main.asyncio.sleep", side_effect=fake_sleep):
        await app._pressure_stats_loop()

    assert sleep_calls == [60.0]
    app._pressure_stats.log_all_windows.assert_not_called()


@pytest.mark.asyncio
async def test_startup_pressure_recap_once_logs_for_active_pressure_positions():
    app = Application.__new__(Application)
    app._log_dir = Path("/tmp")
    app.config_loader = MagicMock()
    app.config_loader.config.global_.stats.pressure_regime_window_ms = 60_000
    app.config_loader.config.global_.stats.pressure_regime_samples = 12
    app._active_symbols = {"DASH/USDT:USDT", "FORM/USDT:USDT"}
    app._symbol_configs = {
        "DASH/USDT:USDT": MagicMock(strategy_mode=StrategyMode.ORDERBOOK_PRESSURE),
        "FORM/USDT:USDT": MagicMock(strategy_mode=StrategyMode.ORDERBOOK_PRICE),
    }
    app._positions = {
        "DASH/USDT:USDT": {
            PositionSide.LONG: Position(
                symbol="DASH/USDT:USDT",
                position_side=PositionSide.LONG,
                position_amt=Decimal("1"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            ),
        },
        "FORM/USDT:USDT": {
            PositionSide.LONG: Position(
                symbol="FORM/USDT:USDT",
                position_side=PositionSide.LONG,
                position_amt=Decimal("2"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            ),
        },
    }

    recap = PressureRecap(
        symbol="DASH",
        side="LONG",
        window_label="1m",
        range_start=datetime(2026, 3, 28, 8, 0, 0),
        range_end=datetime(2026, 3, 28, 9, 0, 0),
        stats_samples=12,
        regime_samples=12,
        overall_active_attempts_corr=0.22,
        overall_active_triggers_corr=0.20,
        overall_passive_triggers_corr=-0.28,
        overall_passive_fill_rate_corr=0.51,
        latest_regime="degrading",
        latest_regime_ts=datetime(2026, 3, 28, 9, 0, 0),
        latest_score=1,
        latest_regime_samples=12,
        latest_active_attempts_corr=0.01,
        latest_active_triggers_corr=0.02,
        latest_passive_triggers_corr=-0.11,
        latest_passive_fill_rate_corr=0.33,
        regime_changes=["03-28 08:19 effective->degrading", "03-28 08:29 degrading->recovering"],
        interpretation="当前 side 的经验规则在衰减，警惕 regime shift",
    )

    with (
        patch("src.main.analyze_recent_pressure_logs", return_value=[recap]) as mock_analyze,
        patch("src.main.get_logger") as mock_get_logger,
    ):
        logger = MagicMock()
        mock_get_logger.return_value = logger
        await app._startup_pressure_recap_once()

    mock_analyze.assert_called_once()
    _, kwargs = mock_analyze.call_args
    assert kwargs["target_keys"] == {("DASH", "LONG")}
    assert kwargs["window_label"] == "1m"
    assert kwargs["regime_samples"] == 12
    logger.info.assert_called_once()
    report = logger.info.call_args.args[0]
    assert "[PRESSURE_RECAP] 盘口量回顾" in report
    assert "标的: DASH LONG" in report
    assert "窗口: 1m" in report
    assert "口径: same-window side-adjusted return" in report
    assert "整体相关性:" in report
    assert "当前状态:" in report
    assert "转折时间点:" in report


@pytest.mark.asyncio
async def test_pressure_stats_loop_reuses_same_cycle_regime_entries_for_periodic_reports():
    app = Application.__new__(Application)
    app._running = True
    app.config_loader = MagicMock()
    app.config_loader.config.global_.stats.pressure_regime_window_ms = 60_000
    app.config_loader.config.global_.stats.pressure_regime_samples = 12
    app._pressure_stats = MagicMock()
    regime_entries = [
        RegimeLogEntry(
            symbol="DASH/USDT:USDT",
            side="LONG",
            window_label="1m",
            regime="recovering",
            prev_regime="failed",
            score=4,
            samples=12,
        )
    ]
    app._pressure_stats.log_all_windows.return_value = regime_entries
    app.pause_manager = MagicMock()
    app.pause_manager.is_paused.return_value = False
    app._handle_pressure_regime_updates = MagicMock()
    app._log_periodic_pressure_reports = MagicMock(side_effect=lambda *_: setattr(app, "_running", False))
    app._save_pressure_regime_state = MagicMock()

    async def fake_sleep(_seconds: float) -> None:
        return None

    with patch("src.main.asyncio.sleep", side_effect=fake_sleep):
        await app._pressure_stats_loop()

    app._pressure_stats.log_all_windows.assert_called_once()
    now_ms = app._pressure_stats.log_all_windows.call_args.args[0]
    app._handle_pressure_regime_updates.assert_called_once_with(regime_entries)
    app._log_periodic_pressure_reports.assert_called_once_with(now_ms, regime_entries)
    app._save_pressure_regime_state.assert_called_once_with(current_ms=now_ms)


def test_log_periodic_pressure_reports_outputs_multiline_report_for_active_pressure_positions():
    app = Application.__new__(Application)
    app._pressure_stats = MagicMock()
    app._active_symbols = {"DASH/USDT:USDT", "FORM/USDT:USDT"}
    app._symbol_configs = {
        "DASH/USDT:USDT": MagicMock(strategy_mode=StrategyMode.ORDERBOOK_PRESSURE),
        "FORM/USDT:USDT": MagicMock(strategy_mode=StrategyMode.ORDERBOOK_PRICE),
    }
    app._positions = {
        "DASH/USDT:USDT": {
            PositionSide.LONG: Position(
                symbol="DASH/USDT:USDT",
                position_side=PositionSide.LONG,
                position_amt=Decimal("1"),
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
            ),
        }
    }
    app._pressure_stats.build_periodic_reports.return_value = [
        PressurePeriodicReport(
            symbol="DASH/USDT:USDT",
            side="LONG",
            as_of_ms=1_711_617_223_000,
            window_reports=[
                PressureWindowReport(
                    window_label="1m",
                    active_triggers=30,
                    passive_triggers=150,
                    active_attempts=10,
                    passive_attempts=5,
                    active_fills=9,
                    active_fill_rate=Decimal("0.900"),
                    passive_fills=1,
                    passive_fill_rate=Decimal("0.200"),
                    price_change_pct=Decimal("0.13"),
                ),
                PressureWindowReport(
                    window_label="5m",
                    active_triggers=120,
                    passive_triggers=880,
                    active_attempts=40,
                    passive_attempts=20,
                    active_fills=35,
                    active_fill_rate=Decimal("0.875"),
                    passive_fills=2,
                    passive_fill_rate=Decimal("0.100"),
                    price_change_pct=Decimal("0.22"),
                ),
            ],
            regime_entry=RegimeLogEntry(
                symbol="DASH/USDT:USDT",
                side="LONG",
                window_label="5m",
                regime="effective",
                prev_regime="recovering",
                score=6,
                samples=15,
                active_attempts_corr=0.335,
                active_triggers_corr=0.221,
                passive_triggers_corr=-0.191,
                passive_fill_rate_corr=0.532,
            ),
        )
    ]

    with patch("src.main.get_logger") as mock_get_logger:
        logger = MagicMock()
        mock_get_logger.return_value = logger
        app._log_periodic_pressure_reports(
            1_711_617_223_000,
            [
                RegimeLogEntry(
                    symbol="DASH/USDT:USDT",
                    side="LONG",
                    window_label="5m",
                    regime="effective",
                    prev_regime="recovering",
                    score=6,
                    samples=15,
                )
            ],
        )

    app._pressure_stats.build_periodic_reports.assert_called_once_with(
        1_711_617_223_000,
        target_keys={("DASH", "LONG")},
        regime_entries=[
            RegimeLogEntry(
                symbol="DASH/USDT:USDT",
                side="LONG",
                window_label="5m",
                regime="effective",
                prev_regime="recovering",
                score=6,
                samples=15,
            )
        ],
    )
    logger.info.assert_called_once()
    report = logger.info.call_args.args[0]
    assert "[PRESSURE_REPORT] 盘口量报告" in report
    assert "标的: DASH/USDT:USDT LONG" in report
    assert "口径: same-window side-adjusted return" in report
    assert "窗口统计:" in report
    assert "1m: active_triggers=30" in report
    assert "5m: active_triggers=120" in report
    assert "当前状态:" in report
    assert "regime=effective | prev=recovering | score=6 | samples=15" in report
    assert "结论: 当前 side 的经验规则仍有效" in report


def _make_pressure_eval_app(
    position_side: PositionSide,
    position_amt: Decimal,
    signal: ExitSignal,
    risk_triggered: bool,
    *,
    state_preset: ExecutionState = ExecutionState.IDLE,
    current_passive_order: bool = False,
):
    """构造用于 _evaluate_side P1-liq 测试的 Application stub。"""
    app = Application.__new__(Application)
    app.signal_engine = MagicMock()
    app.signal_engine.is_strategy_data_stale.return_value = False
    app.signal_engine.evaluate.return_value = signal
    app.signal_engine.get_market_state.return_value = MarketState(
        symbol=signal.symbol,
        best_bid=signal.best_bid,
        best_ask=signal.best_ask,
        last_trade_price=signal.last_trade_price,
        previous_trade_price=signal.last_trade_price,
        last_update_ms=1000,
        is_ready=True,
    )
    app.signal_engine.reset_pressure_dwell = MagicMock()

    app.config_loader = MagicMock()
    app.config_loader.config.global_.ws.stale_data_ms = 1000
    app.config_loader.config.global_.risk.levels = {}
    app.config_loader.config.global_.telegram.enabled = False

    sym_cfg = MagicMock()
    sym_cfg.liq_distance_threshold = Decimal("0.02")
    sym_cfg.pressure_exit_active_recheck_cooldown_ms = 1000
    app._symbol_configs = {signal.symbol: sym_cfg}

    app._calibrating = False
    app.pause_manager = MagicMock()
    app.pause_manager.is_paused.return_value = False
    app.telegram_notifier = None
    app._pressure_stats = None
    app._pressure_fill_recorded_orders = set()
    app._pressure_trigger_signatures = {}
    app.market_ws = MagicMock()
    app.market_ws.is_stale.return_value = False
    app.exchange = MagicMock()
    app.exchange.fetch_positions = AsyncMock(return_value=[])
    app.exchange.place_order = AsyncMock(return_value=OrderResult(success=False, error_message="skip"))
    app._client_order_id_prefix = "test-prefix-"

    app._positions = {
        signal.symbol: {
            position_side: Position(
                symbol=signal.symbol,
                position_side=position_side,
                position_amt=position_amt,
                entry_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
                mark_price=Decimal("10"),
                liquidation_price=Decimal("9"),
            )
        }
    }

    risk_flag = RiskFlag(
        symbol=signal.symbol,
        position_side=position_side,
        is_triggered=risk_triggered,
        dist_to_liq=Decimal("0.01"),
        reason="liq_distance",
    )
    app.risk_manager = MagicMock()
    app.risk_manager.check_risk.return_value = risk_flag

    rules = SymbolRules(
        symbol=signal.symbol,
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    app._rules = {signal.symbol: rules}

    engine = ExecutionEngine(
        place_order=AsyncMock(return_value=OrderResult(success=False, error_message="skip")),
        cancel_order=AsyncMock(return_value=OrderResult(success=True, order_id="x")),
    )
    state = engine.get_state(signal.symbol, position_side)
    state.state = state_preset
    if current_passive_order:
        state.current_order_id = "passive-live"
        state.current_order_execution_preference = SignalExecutionPreference.PASSIVE
        state.current_order_cooldown_ms_override = 0
    app.execution_engines = {signal.symbol: engine}

    return app, engine


@pytest.mark.asyncio
async def test_ensure_symbol_initialized_pressure_inherits_execution_modifier_defaults():
    app = Application.__new__(Application)
    app._symbol_init_lock = asyncio.Lock()
    app._active_symbols = set()
    app._rules = {}
    app._symbol_configs = {}
    app.execution_engines = {}
    app.signal_engine = MagicMock()
    app.exchange = MagicMock()
    app.exchange.get_rules.return_value = SymbolRules(
        symbol="DASH/USDT:USDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    app.exchange.fetch_order_trade_meta = AsyncMock(return_value=None)
    app.config_loader = MagicMock()
    app.config_loader.get_symbol_config.return_value = MagicMock(
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE.value,
        pressure_exit_enabled=True,
        pressure_exit_threshold_qty=Decimal("100"),
        pressure_exit_sustain_ms=2000,
        pressure_exit_passive_level=2,
        pressure_exit_use_roi_mult=None,
        pressure_exit_use_accel_mult=None,
        pressure_exit_active_recheck_cooldown_ms=1000,
        pressure_exit_active_recheck_cooldown_jitter_pct=Decimal("0.15"),
        pressure_exit_active_burst_window_ms=10000,
        pressure_exit_active_burst_max_attempts=8,
        pressure_exit_active_burst_max_fills=5,
        pressure_exit_active_burst_pause_min_ms=2500,
        pressure_exit_active_burst_pause_max_ms=6000,
        pressure_exit_passive_ttl_ms=10000,
        pressure_exit_passive_ttl_jitter_pct=Decimal("0.15"),
        pressure_exit_qty_jitter_pct=Decimal("0.20"),
        pressure_exit_qty_anti_repeat_lookback=3,
        execution_use_roi_mult=True,
        execution_use_accel_mult=False,
        min_signal_interval_ms=200,
        accel_window_ms=2000,
        accel_tiers=[],
        roi_tiers=[],
        order_ttl_ms=4000,
        repost_cooldown_ms=100,
        base_mult=31,
        maker_price_mode="inside_spread_1tick",
        maker_n_ticks=1,
        maker_safety_ticks=1,
        maker_timeouts_to_escalate=2,
        aggr_fills_to_deescalate=1,
        aggr_timeouts_to_deescalate=2,
        fill_rate_feedback_enabled=False,
        fill_rate_window_min=Decimal("5"),
        fill_rate_low_threshold=Decimal("0.25"),
        fill_rate_high_threshold=Decimal("0.75"),
        fill_rate_log_windows_min=[],
        max_mult=500,
        max_order_notional=Decimal("2000"),
    )
    app._place_order = AsyncMock()
    app._cancel_order = AsyncMock()
    app._on_engine_fill = MagicMock()
    app._inspect_reduce_only_block = AsyncMock(return_value=None)

    initialized = await app._ensure_symbol_initialized("DASH/USDT:USDT")

    assert initialized is True
    app.signal_engine.configure_symbol.assert_called_once()
    pressure_config = app.signal_engine.configure_symbol.call_args.kwargs["pressure_config"]
    assert pressure_config is not None
    assert pressure_config.base_mult == 31
    assert pressure_config.use_roi_mult is True
    assert pressure_config.use_accel_mult is False


@pytest.mark.asyncio
async def test_evaluate_side_risk_does_not_promote_pressure_passive_signal():
    """risk 触发 + pressure PASSIVE → 信号保持 PASSIVE，不被改写为 AGGRESSIVE。"""
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.PASSIVE,
        price_override=Decimal("9.8"),
        ttl_override_ms=10000,
        cooldown_override_ms=0,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=True,
    )

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1000,
    )

    # 信号保持 PASSIVE，一级风控 不改写 pressure 的主动/被动语义
    assert signal.execution_preference == SignalExecutionPreference.PASSIVE
    assert signal.price_override == Decimal("9.8")
    assert signal.ttl_override_ms == 10000
    assert signal.cooldown_override_ms == 0


@pytest.mark.asyncio
async def test_evaluate_side_risk_does_not_trigger_preempt_for_pressure_passive():
    """risk 触发 + pressure PASSIVE + WAITING(被动单) → 不触发 preempt 撤单。"""
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.PASSIVE,
        price_override=Decimal("9.8"),
        ttl_override_ms=10000,
        cooldown_override_ms=0,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=True,
        state_preset=ExecutionState.WAITING,
        current_passive_order=True,
    )

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1000,
    )

    # 信号仍然是 PASSIVE，不触发 preempt
    assert signal.execution_preference == SignalExecutionPreference.PASSIVE
    state = engine.get_state(symbol, PositionSide.SHORT)
    # 被动单未被撤，仍在 WAITING
    assert state.state == ExecutionState.WAITING
    assert state.current_order_id == "passive-live"


@pytest.mark.asyncio
async def test_evaluate_side_pressure_stats_skip_repeated_waiting_signal():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.PASSIVE,
        price_override=Decimal("9.8"),
        ttl_override_ms=10_000,
        cooldown_override_ms=0,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=False,
        state_preset=ExecutionState.WAITING,
        current_passive_order=True,
    )
    app._pressure_stats = MagicMock()

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1000,
    )
    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1100,
    )

    app._pressure_stats.record_trigger.assert_called_once_with(
        symbol=symbol,
        side=PositionSide.SHORT.value,
        is_active=False,
        mid_price=Decimal("9.95"),
        ts_ms=1000,
    )
    app._pressure_stats.record_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_side_pressure_stats_record_only_successful_attempt():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.PASSIVE,
        price_override=Decimal("9.8"),
        ttl_override_ms=10_000,
        cooldown_override_ms=0,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=False,
    )
    app._pressure_stats = MagicMock()

    async def place_order_stub(_intent):
        return OrderResult(success=True, order_id="order-1", status=OrderStatus.NEW)

    async def retry_passthrough(**kwargs):
        return kwargs["intent"], kwargs["result"], False

    app._place_order = place_order_stub  # type: ignore[method-assign]
    app._maybe_retry_post_only_reject = retry_passthrough  # type: ignore[method-assign]
    app._refresh_position = AsyncMock()  # type: ignore[method-assign]

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1000,
    )

    app._pressure_stats.record_attempt.assert_called_once_with(
        symbol=symbol,
        side=PositionSide.SHORT.value,
        is_active=False,
        mid_price=Decimal("9.95"),
        ts_ms=1000,
    )
    app._pressure_stats.record_trigger.assert_called_once_with(
        symbol=symbol,
        side=PositionSide.SHORT.value,
        is_active=False,
        mid_price=Decimal("9.95"),
        ts_ms=1000,
    )


@pytest.mark.asyncio
async def test_evaluate_side_records_pressure_active_attempt_for_signal_engine():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_ASK_PRESSURE_ACTIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.AGGRESSIVE,
        price_override=Decimal("10.1"),
        ttl_override_ms=None,
        cooldown_override_ms=1000,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=False,
    )

    async def place_order_stub(_intent):
        return OrderResult(success=True, order_id="order-1", status=OrderStatus.NEW)

    async def retry_passthrough(**kwargs):
        return kwargs["intent"], kwargs["result"], False

    app._place_order = place_order_stub  # type: ignore[method-assign]
    app._maybe_retry_post_only_reject = retry_passthrough  # type: ignore[method-assign]
    app._refresh_position = AsyncMock()  # type: ignore[method-assign]

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
        current_ms=1000,
    )

    cast(MagicMock, app.signal_engine).record_pressure_active_attempt.assert_called_once_with(
        symbol,
        PositionSide.SHORT,
        ts_ms=1000,
    )


@pytest.mark.asyncio
async def test_evaluate_side_pressure_trigger_rearms_after_signal_clears():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_BID_PRESSURE_PASSIVE,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
        strategy_mode=StrategyMode.ORDERBOOK_PRESSURE,
        execution_preference=SignalExecutionPreference.PASSIVE,
        price_override=Decimal("9.8"),
        ttl_override_ms=10_000,
        cooldown_override_ms=0,
        base_mult_override=5,
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=False,
        state_preset=ExecutionState.WAITING,
        current_passive_order=True,
    )
    app._pressure_stats = MagicMock()
    signal_engine = cast(MagicMock, app.signal_engine)

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=signal_engine.get_market_state(symbol),
        current_ms=1000,
    )

    signal_engine.evaluate.return_value = None
    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=signal_engine.get_market_state(symbol),
        current_ms=1100,
    )

    signal_engine.evaluate.return_value = signal
    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=signal_engine.get_market_state(symbol),
        current_ms=1200,
    )

    assert app._pressure_stats.record_trigger.call_count == 2
    assert app._pressure_stats.record_attempt.call_count == 0


@pytest.mark.asyncio
async def test_handle_order_update_pressure_partial_fill_recorded_once():
    app = Application.__new__(Application)
    app._pressure_stats = MagicMock()
    app._pressure_fill_recorded_orders = set()
    app._pressure_trigger_signatures = {}
    app.signal_engine = MagicMock()
    app.config_loader = None
    app.telegram_notifier = None
    app.protective_stop_manager = None
    app.execution_engines = {}

    engine = ExecutionEngine(
        place_order=AsyncMock(return_value=OrderResult(success=True, order_id="order-1")),
        cancel_order=AsyncMock(return_value=OrderResult(success=True, order_id="order-1")),
        on_fill=app._on_engine_fill,  # type: ignore[arg-type]
    )
    app.execution_engines["DASH/USDT:USDT"] = engine

    state = engine.get_state("DASH/USDT:USDT", PositionSide.SHORT)
    state.state = ExecutionState.WAITING
    state.current_order_id = "order-1"
    state.current_order_reason = SignalReason.SHORT_BID_PRESSURE_PASSIVE.value
    state.current_order_mode = ExecutionMode.MAKER_ONLY

    partial = OrderUpdate(
        symbol="DASH/USDT:USDT",
        order_id="order-1",
        client_order_id="client-1",
        side=OrderSide.BUY,
        position_side=PositionSide.SHORT,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=Decimal("0.001"),
        avg_price=Decimal("10"),
        timestamp_ms=1000,
    )
    filled = OrderUpdate(
        symbol="DASH/USDT:USDT",
        order_id="order-1",
        client_order_id="client-1",
        side=OrderSide.BUY,
        position_side=PositionSide.SHORT,
        status=OrderStatus.FILLED,
        filled_qty=Decimal("0.002"),
        avg_price=Decimal("10"),
        timestamp_ms=1100,
    )

    await app._handle_order_update(partial)
    await app._handle_order_update(filled)

    app._pressure_stats.record_outcome.assert_called_once_with(
        symbol="DASH/USDT:USDT",
        side=PositionSide.SHORT.value,
        is_active=False,
        is_filled=True,
        ts_ms=partial.timestamp_ms,
    )
    cast(MagicMock, app.signal_engine).record_pressure_active_fill.assert_not_called()


def test_record_pressure_fill_once_notifies_signal_engine_for_active_order():
    app = Application.__new__(Application)
    app._pressure_stats = MagicMock()
    app._pressure_fill_recorded_orders = set()
    app._pressure_trigger_signatures = {}
    app.signal_engine = MagicMock()

    app._record_pressure_fill_once(
        symbol="DASH/USDT:USDT",
        position_side=PositionSide.SHORT,
        order_id="order-1",
        reason=SignalReason.SHORT_ASK_PRESSURE_ACTIVE.value,
        ts_ms=1234,
    )
    app._record_pressure_fill_once(
        symbol="DASH/USDT:USDT",
        position_side=PositionSide.SHORT,
        order_id="order-1",
        reason=SignalReason.SHORT_ASK_PRESSURE_ACTIVE.value,
        ts_ms=1300,
    )

    app._pressure_stats.record_outcome.assert_called_once_with(
        symbol="DASH/USDT:USDT",
        side=PositionSide.SHORT.value,
        is_active=True,
        is_filled=True,
        ts_ms=1234,
    )
    cast(MagicMock, app.signal_engine).record_pressure_active_fill.assert_called_once_with(
        "DASH/USDT:USDT",
        PositionSide.SHORT,
        ts_ms=1234,
    )


@pytest.mark.asyncio
async def test_evaluate_side_liq_distance_logs_only_on_entry():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_PRIMARY,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=True,
    )
    engine.on_signal = AsyncMock(return_value=None)  # type: ignore[method-assign]
    state = engine.get_state(symbol, PositionSide.SHORT)

    with patch("src.main.log_event") as log_event_mock:
        await app._evaluate_side(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            engine=engine,
            rules=app._rules[symbol],
            market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
            current_ms=1000,
        )

        risk_calls = [
            call for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "risk" and call.kwargs.get("reason") == "liq_distance"
        ]
        assert len(risk_calls) == 1
        assert state.liq_distance_active is True

        log_event_mock.reset_mock()
        state.mode = ExecutionMode.MAKER_ONLY

        await app._evaluate_side(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            engine=engine,
            rules=app._rules[symbol],
            market_state=app.signal_engine.get_market_state(symbol),  # type: ignore[union-attr]
            current_ms=1100,
        )

        risk_calls = [
            call for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "risk" and call.kwargs.get("reason") == "liq_distance"
        ]
        assert len(risk_calls) == 0
        assert state.mode == ExecutionMode.AGGRESSIVE_LIMIT


@pytest.mark.asyncio
async def test_evaluate_side_liq_distance_logs_recovery_once():
    symbol = "DASH/USDT:USDT"
    signal = ExitSignal(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        reason=SignalReason.SHORT_PRIMARY,
        timestamp_ms=1000,
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.1"),
        last_trade_price=Decimal("10"),
    )
    app, engine = _make_pressure_eval_app(
        position_side=PositionSide.SHORT,
        position_amt=Decimal("-5"),
        signal=signal,
        risk_triggered=True,
    )
    signal_engine = cast(MagicMock, app.signal_engine)
    signal_engine.evaluate.return_value = None
    state = engine.get_state(symbol, PositionSide.SHORT)

    await app._evaluate_side(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        engine=engine,
        rules=app._rules[symbol],
        market_state=signal_engine.get_market_state(symbol),
        current_ms=1000,
    )
    assert state.liq_distance_active is True

    risk_manager = cast(MagicMock, app.risk_manager)
    risk_manager.check_risk.return_value = RiskFlag(
        symbol=symbol,
        position_side=PositionSide.SHORT,
        is_triggered=False,
        dist_to_liq=Decimal("0.03"),
        reason=None,
    )

    with patch("src.main.log_event") as log_event_mock:
        await app._evaluate_side(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            engine=engine,
            rules=app._rules[symbol],
            market_state=signal_engine.get_market_state(symbol),
            current_ms=1100,
        )

        recovery_calls = [
            call for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "risk" and call.kwargs.get("reason") == "liq_distance_recovered"
        ]
        assert len(recovery_calls) == 1
        assert state.liq_distance_active is False


@pytest.mark.asyncio
async def test_run_should_exit_on_shutdown_without_blocking_ws_connect(monkeypatch):
    app = Application(Path("config/config.yaml"))
    app.market_ws = DummyWS()  # type: ignore[assignment]
    app.user_data_ws = DummyWS()  # type: ignore[assignment]
    # run() 只有在存在 active_symbols 时才会触发 market ws rebuild/连接
    app._active_symbols = {"BTC/USDT:USDT"}

    async def noop() -> None:
        return

    async def wait_shutdown() -> None:
        await app._shutdown_event.wait()

    app._fetch_positions = noop  # type: ignore[method-assign]

    async def fake_rebuild_market_ws(*args, **kwargs) -> None:
        # 模拟真实 _rebuild_market_ws：启动 connect 任务但不阻塞 run()
        app._market_ws_task = asyncio.create_task(app.market_ws.connect())  # type: ignore[union-attr]

    app._rebuild_market_ws = fake_rebuild_market_ws  # type: ignore[method-assign]

    async def noop_cancel(reason: str) -> None:
        return

    app._cancel_own_orders = noop_cancel  # type: ignore[method-assign]
    app._main_loop = wait_shutdown  # type: ignore[method-assign]
    app._timeout_check_loop = wait_shutdown  # type: ignore[method-assign]

    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float, result=None):
        await original_sleep(0)
        return result

    import src.main as main_module
    monkeypatch.setattr(main_module.asyncio, "sleep", fast_sleep)

    async def trigger_shutdown() -> None:
        await original_sleep(0)
        app.request_shutdown()

    asyncio.create_task(trigger_shutdown())

    await asyncio.wait_for(app.run(), timeout=1.0)

    assert app.market_ws.connect_started is True  # type: ignore[union-attr]
    assert app.market_ws.disconnect_called is True  # type: ignore[union-attr]
    assert app.user_data_ws.connect_started is True  # type: ignore[union-attr]
    assert app.user_data_ws.disconnect_called is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_main_loop_spawns_side_tasks_and_shutdown_cancels_them():
    app = Application(Path("config/config.yaml"))
    app._running = True
    app._active_symbols = {"BTC/USDT:USDT", "ETH/USDT:USDT"}

    class DummyConfigLoader:
        def get_symbols(self):
            return ["BTC/USDT:USDT", "ETH/USDT:USDT"]

    app.config_loader = DummyConfigLoader()  # type: ignore[assignment]

    main_loop_task = asyncio.create_task(app._main_loop())
    app._main_loop_task = main_loop_task

    await asyncio.sleep(0)
    assert len(app._side_tasks) == 4  # 2 symbols × (LONG+SHORT)

    await asyncio.wait_for(app.shutdown(), timeout=2.0)
    assert len(app._side_tasks) == 0


def test_protective_stop_debounce_classification():
    assert Application._protective_stop_debounce_s("position_update:LONG") == 1.0
    assert Application._protective_stop_debounce_s("startup") == 0.0
    assert Application._protective_stop_debounce_s("calibration:user_data") == 0.0
    assert Application._protective_stop_debounce_s("order_update:FILLED") == 0.2
    assert Application._protective_stop_debounce_s("our_algo:CANCELED") == 0.2


def test_should_refresh_on_account_event_for_margin_transfer():
    event = AccountUpdateEvent(
        reason="MARGIN_TRANSFER",
        timestamp_ms=1,
        has_balance_delta=True,
        balance_delta_assets=("USDT",),
        has_position_delta=False,
    )
    assert Application._should_refresh_on_account_event(event) is True


def test_should_refresh_on_account_event_for_unknown_balance_delta():
    event = AccountUpdateEvent(
        reason=None,
        timestamp_ms=1,
        has_balance_delta=True,
        balance_delta_assets=("USDT",),
        has_position_delta=False,
    )
    assert Application._should_refresh_on_account_event(event) is True


def test_should_not_refresh_on_account_event_for_order_reason():
    event = AccountUpdateEvent(
        reason="ORDER",
        timestamp_ms=1,
        has_balance_delta=True,
        balance_delta_assets=("USDT",),
        has_position_delta=True,
    )
    assert Application._should_refresh_on_account_event(event) is False


def test_should_refresh_on_account_event_for_unknown_reason_without_position_delta():
    event = AccountUpdateEvent(
        reason="WALLET_TRANSFER_OUT",
        timestamp_ms=1,
        has_balance_delta=True,
        balance_delta_assets=("USDT",),
        has_position_delta=False,
    )
    assert Application._should_refresh_on_account_event(event) is True


def test_on_account_update_event_schedules_refresh():
    app = Application.__new__(Application)
    app._running = True
    app._margin_refresh_debounce_s = 2.5
    app._schedule_positions_refresh = MagicMock()

    app._on_account_update_event(
        AccountUpdateEvent(
            reason="MARGIN_TRANSFER",
            timestamp_ms=1,
            has_balance_delta=True,
            balance_delta_assets=("USDT",),
            has_position_delta=False,
        )
    )

    app._schedule_positions_refresh.assert_called_once_with(
        reason="account_update:MARGIN_TRANSFER",
        debounce_s=2.5,
    )


def test_on_market_event_records_with_market_recorder_before_signal_engine():
    app = Application.__new__(Application)
    app._market_recorder = MagicMock()
    app.signal_engine = None

    event = MarketEvent(
        symbol="DASH/USDT:USDT",
        timestamp_ms=1,
        best_bid=Decimal("10"),
        best_ask=Decimal("10.1"),
        best_bid_qty=Decimal("1"),
        best_ask_qty=Decimal("2"),
        event_type="book_ticker",
    )

    app._on_market_event(event)

    app._market_recorder.record.assert_called_once_with(event)


@pytest.mark.asyncio
async def test_shutdown_closes_market_recorder():
    app = Application(Path("config/config.yaml"))
    app._running = True
    app._cancel_own_orders = AsyncMock()  # type: ignore[method-assign]
    recorder = MagicMock()
    recorder.close = AsyncMock()
    app._market_recorder = recorder

    await asyncio.wait_for(app.shutdown(), timeout=2.0)

    recorder.close.assert_awaited_once()
    assert app._market_recorder is None


# ================================================================
# _resolve_symbol 测试
# ================================================================

def _make_app_with_symbols(symbols: set[str]) -> Application:
    """构造一个带 _active_symbols 的 Application（不做真实初始化）。"""
    app = Application.__new__(Application)
    app._active_symbols = symbols
    return app


def test_resolve_symbol_exact_ccxt_format():
    """精确匹配 ccxt 格式"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_resolve_symbol_compact_format():
    """简写匹配 BTCUSDT"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTCUSDT") == "BTC/USDT:USDT"


def test_resolve_symbol_base_only():
    """base 币种匹配 BTC（唯一命中）"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTC") == "BTC/USDT:USDT"


def test_resolve_symbol_base_case_insensitive():
    """base 币种匹配大小写不敏感"""
    app = _make_app_with_symbols({"DASH/USDT:USDT"})
    assert app._resolve_symbol("dash") == "DASH/USDT:USDT"


def test_resolve_symbol_base_ambiguous():
    """base 币种歧义时返回 None（假设存在 BTC/USDT:USDT 和 BTC/BUSD:BUSD）"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "BTC/BUSD:BUSD"})
    assert app._resolve_symbol("BTC") is None


def test_resolve_symbol_unknown():
    """未知 symbol 返回 None"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    assert app._resolve_symbol("XYZ") is None


def test_resolve_symbol_empty():
    """空字符串返回 None"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    assert app._resolve_symbol("") is None
    assert app._resolve_symbol("  ") is None


# ================================================================
# _parse_duration 测试
# ================================================================


def test_parse_duration_seconds():
    """秒解析"""
    assert Application._parse_duration("10s") == 10.0
    assert Application._parse_duration("1s") == 1.0
    assert Application._parse_duration("0.5s") == 0.5


def test_parse_duration_minutes():
    """分解析"""
    assert Application._parse_duration("30m") == 1800.0
    assert Application._parse_duration("1m") == 60.0


def test_parse_duration_hours():
    """时解析"""
    assert Application._parse_duration("2h") == 7200.0
    assert Application._parse_duration("1h") == 3600.0


def test_parse_duration_case_insensitive():
    """大小写不敏感"""
    assert Application._parse_duration("10S") == 10.0
    assert Application._parse_duration("30M") == 1800.0
    assert Application._parse_duration("2H") == 7200.0


def test_parse_duration_invalid():
    """无效输入返回 None"""
    assert Application._parse_duration("") is None
    assert Application._parse_duration("s") is None
    assert Application._parse_duration("abc") is None
    assert Application._parse_duration("10") is None
    assert Application._parse_duration("10x") is None
    assert Application._parse_duration("-5s") is None
    assert Application._parse_duration("0s") is None
    # NaN / Inf / 超上限
    assert Application._parse_duration("nans") is None
    assert Application._parse_duration("infs") is None
    assert Application._parse_duration("infh") is None
    assert Application._parse_duration("1e309s") is None
    assert Application._parse_duration("25h") is None  # 超过 24h 上限


# ================================================================
# _handle_cmd_pause args 拆分测试
# ================================================================


@pytest.mark.asyncio
async def test_handle_cmd_pause_global_no_args():
    """无参数 -> 全局暂停"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("")
    assert "已暂停全局" in result
    assert app.pause_manager.is_paused() is True


@pytest.mark.asyncio
async def test_handle_cmd_pause_global_with_duration():
    """全局定时暂停: /pause 10s"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("10s")
    assert "已暂停全局" in result
    assert "自动恢复" in result
    assert app.pause_manager.is_paused() is True
    # 清理
    await app.pause_manager.resume()


@pytest.mark.asyncio
async def test_handle_cmd_pause_symbol_with_duration():
    """symbol 定时暂停: /pause BTC 30m"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("BTC 30m")
    assert "已暂停" in result
    assert "自动恢复" in result
    assert app.pause_manager.is_paused("BTC/USDT:USDT") is True
    assert app.pause_manager.is_paused("ETH/USDT:USDT") is False
    # 清理
    await app.pause_manager.resume("BTC/USDT:USDT")


@pytest.mark.asyncio
async def test_handle_cmd_pause_symbol_no_duration():
    """symbol 无限期暂停: /pause BTC"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("BTC")
    assert "已暂停" in result
    assert app.pause_manager.is_paused("BTC/USDT:USDT") is True
    await app.pause_manager.resume("BTC/USDT:USDT")


@pytest.mark.asyncio
async def test_handle_cmd_pause_unknown_symbol():
    """未知 symbol"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("XYZ 10s")
    assert "未知交易对" in result


# ================================================================
# _schedule_protective_stop_sync 两阶段取消测试
# ================================================================


def _make_app_for_schedule_test() -> Application:
    """构造一个能测试 _schedule_protective_stop_sync 的 Application。"""
    app = Application.__new__(Application)
    app._running = True
    app._active_symbols = {"BTC/USDT:USDT"}
    app.exchange = MagicMock()
    app.protective_stop_manager = MagicMock()
    app._protective_stop_tasks = {}  # type: ignore[assignment]
    app._protective_stop_task_reasons = {}  # type: ignore[assignment]
    app._protective_stop_task_executing = {}  # type: ignore[assignment]
    app._protective_stop_pending_reason = {}  # type: ignore[assignment]
    app._positions = {}  # type: ignore[assignment]
    app._symbol_configs = {}  # type: ignore[assignment]
    app._rules = {}  # type: ignore[assignment]
    return app


@pytest.mark.asyncio
async def test_schedule_debounce_task_can_be_cancelled():
    """debounce 阶段的任务应该被新调度取消"""
    app = _make_app_for_schedule_test()
    sync_called = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        sync_called.set()

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度 (debounce=0.2s)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    first_task = app._protective_stop_tasks["BTC/USDT:USDT"]

    # 立即第二次调度 -> 应取消第一个(还在 debounce sleep)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    second_task = app._protective_stop_tasks["BTC/USDT:USDT"]

    assert first_task is not second_task
    # cancel 已发出, yield 让事件循环处理取消
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()

    # 等第二个完成
    await second_task
    assert sync_called.is_set()


@pytest.mark.asyncio
async def test_schedule_executing_task_not_cancelled():
    """已进入执行阶段的任务不应被取消, 新请求走脏标记, 任务完成后自行 re-run"""
    app = _make_app_for_schedule_test()

    call_order: list[str] = []
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        call_order.append(reason)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度 (startup -> debounce=0s, 立即进入执行)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    # 等第一个进入执行阶段
    await first_entered.wait()

    # 此时 past_debounce 已 set, 新调度应走脏标记路径(不创建新任务)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    # 同一个 task 对象(脏标记模式不创建新任务)
    assert app._protective_stop_tasks["BTC/USDT:USDT"] is task
    assert not task.cancelled()

    # 放行第一个
    first_can_finish.set()
    await task

    # 两次 sync 都应该被调用, 且顺序正确
    assert call_order == ["startup", "our_algo_canceled"]


@pytest.mark.asyncio
async def test_schedule_no_concurrent_sync():
    """同一 symbol 不应并发执行 _sync_protective_stop"""
    app = _make_app_for_schedule_test()

    concurrent_count = 0
    max_concurrent = 0
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()
        concurrent_count -= 1

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度(startup, debounce=0)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    await first_entered.wait()

    # 第二次调度, 走脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")

    first_can_finish.set()
    await task

    assert max_concurrent == 1  # 始终串行


@pytest.mark.asyncio
async def test_schedule_triple_trigger_no_concurrent():
    """三次触发: T1 执行中 + T2 脏标记 + T3 覆盖脏标记, 始终串行且不丢失最终请求"""
    app = _make_app_for_schedule_test()

    concurrent_count = 0
    max_concurrent = 0
    call_order: list[str] = []
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        call_order.append(reason)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()
        concurrent_count -= 1

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # T1: startup (debounce=0, 立即进入执行)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    await first_entered.wait()

    # T2: 走脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    assert app._protective_stop_pending_reason.get("BTC/USDT:USDT") == "our_algo_canceled"

    # T3: 覆盖脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "position_update")
    assert app._protective_stop_pending_reason.get("BTC/USDT:USDT") == "position_update"

    # 同一个 task 对象(T2/T3 都不创建新任务)
    assert app._protective_stop_tasks["BTC/USDT:USDT"] is task

    # 放行 T1
    first_can_finish.set()
    await task

    # T1 + T3 的 re-run(T2 被 T3 覆盖)
    assert call_order == ["startup", "position_update"]
    assert max_concurrent == 1  # 始终串行, 不会出现并发
