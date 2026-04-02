"""PressureStatsCollector 单元测试"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import src.stats.pressure_stats as pressure_stats_module
from src.stats.pressure_stats import (
    PressureStatsCollector,
    RegimeLogEntry,
    RegimeTracker,
    analyze_recent_pressure_logs,
    _window_label,
)


# ---------------------------------------------------------------------------
# _window_label
# ---------------------------------------------------------------------------

class TestWindowLabel:
    def test_minutes(self):
        assert _window_label(60_000) == "1m"
        assert _window_label(300_000) == "5m"
        assert _window_label(900_000) == "15m"

    def test_seconds_fallback(self):
        assert _window_label(30_000) == "30s"
        assert _window_label(90_000) == "90s"


# ---------------------------------------------------------------------------
# record / compute basics
# ---------------------------------------------------------------------------

class TestRecordAndCompute:
    def _make(self) -> PressureStatsCollector:
        return PressureStatsCollector(price_sample_interval_ms=0)  # 关闭节流

    def test_empty_window(self):
        c = self._make()
        stats = c.compute_window("BTC/USDT:USDT", "LONG", 60_000, 100_000)
        assert stats["active_triggers"] == 0
        assert stats["passive_triggers"] == 0
        assert stats["active_attempts"] == 0
        assert stats["passive_attempts"] == 0
        assert stats["passive_fill_rate"] is None
        assert stats["price_change_pct"] is None

    def test_signal_counting(self):
        c = self._make()
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=2000)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("102"), ts_ms=3000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=2000)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("102"), ts_ms=3000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["active_triggers"] == 2
        assert stats["passive_triggers"] == 1
        assert stats["active_attempts"] == 2
        assert stats["passive_attempts"] == 1

    def test_signal_window_filtering(self):
        """窗口外的事件不计入。"""
        c = self._make()
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=5000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=5000)

        # 窗口 = 3000ms，current = 6000 → cutoff = 3000 → 只有 ts=5000 计入
        stats = c.compute_window("BTC", "LONG", 3000, 6000)
        assert stats["active_triggers"] == 1
        assert stats["active_attempts"] == 1

    def test_outcome_fill_rate(self):
        c = self._make()
        # 5 passive triggers, 5 passive attempts, 2 filled
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=400)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=500)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=600)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=700)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=800)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=500)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=600)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=700)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=800)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=900)
        c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=1000)
        c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=2000)
        # 2 active triggers, 2 active attempts, 1 filled
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3400)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3500)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3500)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3600)
        c.record_outcome("BTC", "LONG", is_active=True, is_filled=True, ts_ms=4000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["passive_triggers"] == 5
        assert stats["passive_fills"] == 2
        assert stats["passive_fill_rate"] == Decimal("0.400")  # 2/5
        assert stats["active_triggers"] == 2
        assert stats["active_fills"] == 1
        assert stats["active_fill_rate"] == Decimal("0.500")  # 1/2

    def test_trigger_and_attempt_counts_are_distinct(self):
        c = self._make()
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=1000)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=2000)
        c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=3000)
        c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=3000)
        c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=3500)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["passive_triggers"] == 3
        assert stats["passive_attempts"] == 1
        assert stats["passive_fill_rate"] == Decimal("1.000")

    def test_price_change(self):
        c = self._make()
        c.record_price("BTC", Decimal("100"), ts_ms=1000)
        c.record_price("BTC", Decimal("105"), ts_ms=2000)
        c.record_price("BTC", Decimal("103"), ts_ms=3000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        # start=100, end=103 → (103-100)/100*100 = 3.00%
        assert stats["price_start"] == Decimal("100")
        assert stats["price_end"] == Decimal("103")
        assert stats["price_change_pct"] == Decimal("3.00")

    def test_price_change_negative(self):
        c = self._make()
        c.record_price("BTC", Decimal("100"), ts_ms=1000)
        c.record_price("BTC", Decimal("98"), ts_ms=2000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["price_change_pct"] == Decimal("-2.00")


# ---------------------------------------------------------------------------
# 采样节流
# ---------------------------------------------------------------------------

class TestPriceSampling:
    def test_throttle(self):
        c = PressureStatsCollector(price_sample_interval_ms=5000)
        c.record_price("BTC", Decimal("100"), ts_ms=10_000)
        c.record_price("BTC", Decimal("200"), ts_ms=13_000)  # 距上次 3s，应被跳过
        c.record_price("BTC", Decimal("300"), ts_ms=15_000)  # 距上次 5s，应记录

        stats = c.compute_window("BTC", "LONG", 60_000, 20_000)
        assert stats["price_start"] == Decimal("100")
        assert stats["price_end"] == Decimal("300")


# ---------------------------------------------------------------------------
# 跨 symbol/side 隔离
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_different_sides(self):
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_trigger("BTC", "SHORT", is_active=False, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("BTC", "SHORT", is_active=False, mid_price=Decimal("100"), ts_ms=1000)

        long_stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        short_stats = c.compute_window("BTC", "SHORT", 60_000, 10_000)

        assert long_stats["active_triggers"] == 1
        assert long_stats["passive_triggers"] == 0
        assert short_stats["active_triggers"] == 0
        assert short_stats["passive_triggers"] == 1
        assert long_stats["active_attempts"] == 1
        assert long_stats["passive_attempts"] == 0
        assert short_stats["active_attempts"] == 0
        assert short_stats["passive_attempts"] == 1

    def test_different_symbols(self):
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_trigger("ETH", "LONG", is_active=True, mid_price=Decimal("50"), ts_ms=1000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("ETH", "LONG", is_active=True, mid_price=Decimal("50"), ts_ms=1000)

        btc = c.compute_window("BTC", "LONG", 60_000, 10_000)
        eth = c.compute_window("ETH", "LONG", 60_000, 10_000)

        assert btc["active_triggers"] == 1
        assert eth["active_triggers"] == 1
        assert btc["active_attempts"] == 1
        assert eth["active_attempts"] == 1

    def test_price_shared_across_sides(self):
        """价格采样 per symbol，不区分 side。"""
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_price("BTC", Decimal("100"), ts_ms=1000)
        c.record_price("BTC", Decimal("110"), ts_ms=2000)

        long_stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        short_stats = c.compute_window("BTC", "SHORT", 60_000, 10_000)

        # 价格数据相同
        assert long_stats["price_change_pct"] == Decimal("10.00")
        assert short_stats["price_change_pct"] == Decimal("10.00")


# ---------------------------------------------------------------------------
# 环形缓冲区溢出
# ---------------------------------------------------------------------------

class TestRingBuffer:
    def test_max_events(self):
        c = PressureStatsCollector(max_events=5, price_sample_interval_ms=0)
        for i in range(10):
            c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=i * 1000)
            c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=i * 1000)

        # 只保留最近 5 条
        stats = c.compute_window("BTC", "LONG", 100_000, 20_000)
        assert stats["active_triggers"] == 5
        assert stats["active_attempts"] == 5


# ---------------------------------------------------------------------------
# log_all_windows
# ---------------------------------------------------------------------------

class TestLogAllWindows:
    def test_no_crash_on_empty(self):
        c = PressureStatsCollector()
        c.log_all_windows(current_ms=100_000)  # 无事件，不应抛异常

    def test_skips_empty_windows(self, caplog):
        """窗口内无事件时跳过输出。"""
        c = PressureStatsCollector(price_sample_interval_ms=0)
        # 只在很早的时间记录，1min 窗口内不会有事件
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.log_all_windows(current_ms=1_000_000)  # 1000s 之后
        # 1min 窗口应该没有输出（信号在 1s 远超 1min 前）
        # 但 15min 窗口有

    def test_outputs_when_data_present(self):
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=5000)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=5000)
        c.record_price("BTC", Decimal("100"), ts_ms=5000)
        c.record_price("BTC", Decimal("101"), ts_ms=6000)
        # 不应抛异常
        c.log_all_windows(current_ms=10_000)


# ---------------------------------------------------------------------------
# regime state machine
# ---------------------------------------------------------------------------

class TestPressureRegime:
    def _make(self) -> PressureStatsCollector:
        return PressureStatsCollector(price_sample_interval_ms=0)

    def test_regime_effective_when_recent_correlations_align(self):
        c = self._make()
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )

        regime = c._evaluate_regime("BTC", "LONG")
        assert regime is not None
        assert regime.state == "effective"
        assert regime.score >= 4
        assert regime.active_attempts_corr is not None and regime.active_attempts_corr > 0
        assert regime.passive_triggers_corr is not None and regime.passive_triggers_corr < 0

    def test_short_regime_effective_uses_side_adjusted_returns(self):
        c = self._make()
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "SHORT",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"-{i + 1}.00"),
            )

        regime = c._evaluate_regime("BTC", "SHORT")
        assert regime is not None
        assert regime.state == "effective"
        assert regime.score >= 4
        assert regime.active_attempts_corr is not None and regime.active_attempts_corr > 0
        assert regime.passive_triggers_corr is not None and regime.passive_triggers_corr < 0

    def test_regime_state_transitions_failed_and_recovering(self):
        tracker = RegimeTracker(state="effective")

        state, prev = PressureStatsCollector._advance_regime_state(tracker, score=0)
        assert prev == "effective"
        assert state == "degrading"

        state, prev = PressureStatsCollector._advance_regime_state(tracker, score=0)
        assert prev == "degrading"
        assert state == "failed"

        state, prev = PressureStatsCollector._advance_regime_state(tracker, score=4)
        assert prev == "failed"
        assert state == "recovering"

        state, prev = PressureStatsCollector._advance_regime_state(tracker, score=4)
        assert prev == "recovering"
        assert state == "effective"

    def test_log_all_windows_emits_pressure_regime_after_min_samples(self, monkeypatch):
        events: list[tuple[str, dict]] = []

        def fake_log_event(event_type: str, *, level: str | None = None, **fields):
            events.append((event_type, fields))

        monkeypatch.setattr(pressure_stats_module, "log_event", fake_log_event)

        c = self._make()
        for i in range(11):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )

        base_ts = 11 * 300_000 + 1_000
        for _ in range(22):
            c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts)
        for _ in range(9):
            c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=base_ts + 100)
        for _ in range(17):
            c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts + 200)
        for _ in range(10):
            c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=base_ts + 300)
        for _ in range(3):
            c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=base_ts + 400)
        c.record_price("BTC", Decimal("100"), ts_ms=base_ts + 500)
        c.record_price("BTC", Decimal("112"), ts_ms=base_ts + 600)
        c.log_all_windows(current_ms=12 * 300_000)

        regime_events = [fields for event_type, fields in events if event_type == "pressure_regime"]
        assert regime_events
        assert regime_events[-1]["window"] == "5m"
        assert regime_events[-1]["corr_basis"] == "side_adjusted_same_window"
        assert regime_events[-1]["regime"] in {"effective", "degrading", "failed", "recovering"}

    def test_log_all_windows_respects_configured_regime_window_and_samples(self, monkeypatch):
        events: list[tuple[str, dict]] = []

        def fake_log_event(event_type: str, *, level: str | None = None, **fields):
            events.append((event_type, fields))

        monkeypatch.setattr(pressure_stats_module, "log_event", fake_log_event)

        c = PressureStatsCollector(
            price_sample_interval_ms=0,
            regime_window_ms=60_000,
            regime_samples=4,
        )
        for i in range(3):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 60_000,
                active_triggers=10 + i,
                passive_triggers=40 - i,
                active_attempts=6 + i,
                passive_fill_rate=Decimal(f"0.{30 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )

        base_ts = 3 * 60_000 + 1_000
        for _ in range(10):
            c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts)
        for _ in range(4):
            c.record_trigger("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=base_ts + 100)
        for _ in range(8):
            c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts + 200)
        for _ in range(5):
            c.record_attempt("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=base_ts + 300)
        for _ in range(2):
            c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=base_ts + 400)
        c.record_price("BTC", Decimal("100"), ts_ms=base_ts + 500)
        c.record_price("BTC", Decimal("104"), ts_ms=base_ts + 600)
        c.log_all_windows(current_ms=4 * 60_000)

        regime_events = [fields for event_type, fields in events if event_type == "pressure_regime"]
        assert regime_events
        assert regime_events[-1]["window"] == "1m"
        assert regime_events[-1]["samples"] == 4

    def test_build_periodic_reports_does_not_advance_tracker_when_reusing_regime_entries(self):
        c = self._make()
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )
        tracker = c._get_regime_tracker(c._key("BTC", "LONG"))
        tracker.state = "failed"
        tracker.strong_streak = 0
        tracker.weak_streak = 0

        regime = c._evaluate_regime("BTC", "LONG")
        assert regime is not None
        assert tracker.state == "recovering"
        assert tracker.strong_streak == 1

        base_ts = 12 * 300_000
        c.record_trigger("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts)
        c.record_attempt("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=base_ts + 1)
        c.record_outcome("BTC", "LONG", is_active=True, is_filled=True, ts_ms=base_ts + 2)

        reports = c.build_periodic_reports(
            base_ts + 2,
            target_keys={("BTC", "LONG")},
            regime_entries=[
                RegimeLogEntry(
                    symbol="BTC",
                    side="LONG",
                    window_label="5m",
                    regime=regime.state,
                    prev_regime=regime.prev_state,
                    score=regime.score,
                    samples=regime.samples,
                    active_attempts_corr=regime.active_attempts_corr,
                    active_triggers_corr=regime.active_triggers_corr,
                    passive_triggers_corr=regime.passive_triggers_corr,
                    passive_fill_rate_corr=regime.passive_fill_rate_corr,
                )
            ],
        )

        assert len(reports) == 1
        assert reports[0].regime_entry is not None
        assert reports[0].regime_entry.regime == "recovering"
        assert tracker.state == "recovering"
        assert tracker.strong_streak == 1

    def test_regime_history_buffer_expands_to_match_configured_samples(self):
        c = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=60)
        key = c._key("BTC", "LONG")
        assert c._get_regime_snapshots(key).maxlen == 60

        for i in range(60):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=200 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + (i % 70):03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )

        regime = c._evaluate_regime("BTC", "LONG")
        assert regime is not None
        assert regime.samples == 60

    def test_export_and_restore_regime_state_with_recent_gap(self):
        c = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )
        regime = c._evaluate_regime("BTC", "LONG")
        assert regime is not None

        payload = c.export_regime_state(current_ms=3_600_000)

        restored = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        count = restored.restore_regime_state(payload, current_ms=3_660_000, max_gap_ms=900_000)

        assert count == 12
        restored_regime = restored._evaluate_regime("BTC", "LONG")
        assert restored_regime is not None
        assert restored_regime.samples == 12

    def test_restore_regime_state_skips_stale_payload(self):
        c = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        payload = c.export_regime_state(current_ms=1_000)

        restored = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        count = restored.restore_regime_state(payload, current_ms=1_000_000, max_gap_ms=60_000)

        assert count == 0

    def test_restore_regime_state_uses_latest_snapshot_time_not_save_time(self):
        c = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )
        payload = c.export_regime_state(current_ms=9_000_000)
        assert payload["latest_snapshot_ts_ms"] == 3_600_000

        restored = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        count = restored.restore_regime_state(payload, current_ms=4_260_001, max_gap_ms=600_000)

        assert count == 0

    def test_restore_regime_state_skips_only_stale_keys(self):
        c = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        for i in range(12):
            c._record_regime_snapshot(
                "BTC",
                "LONG",
                ts_ms=10_800_000 + (i + 1) * 300_000,
                active_triggers=10 + i,
                passive_triggers=120 - i,
                active_attempts=5 + i,
                passive_fill_rate=Decimal(f"0.{20 + i:03d}"),
                price_change_pct=Decimal(f"{i + 1}.00"),
            )
            c._record_regime_snapshot(
                "ETH",
                "LONG",
                ts_ms=(i + 1) * 300_000,
                active_triggers=8 + i,
                passive_triggers=140 - i,
                active_attempts=4 + i,
                passive_fill_rate=Decimal(f"0.{30 + i:03d}"),
                price_change_pct=Decimal(f"{i + 2}.00"),
            )
        assert c._evaluate_regime("BTC", "LONG") is not None
        assert c._evaluate_regime("ETH", "LONG") is not None

        payload = c.export_regime_state(current_ms=14_400_000)

        restored = PressureStatsCollector(price_sample_interval_ms=0, regime_samples=12)
        count = restored.restore_regime_state(payload, current_ms=14_460_000, max_gap_ms=900_000)

        assert count == 12
        assert restored.regime_snapshot_keys() == {"BTC|LONG"}
        assert restored._evaluate_regime("BTC", "LONG") is not None
        assert restored._evaluate_regime("ETH", "LONG") is None


class TestStartupPressureRecap:
    def test_analyze_recent_pressure_logs_replays_warmup_context_before_lookback(self):
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "vibe-quant_2026-03-28.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-03-28 09:50:00.000 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=10 | passive_triggers=50 | active_attempts=5 | price_chg=0.10%",
                        "2026-03-28 09:55:00.000 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=12 | passive_triggers=45 | active_attempts=6 | price_chg=0.20%",
                        "2026-03-28 10:05:00.000 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=14 | passive_triggers=40 | active_attempts=7 | price_chg=0.30%",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recaps = analyze_recent_pressure_logs(
                Path(tmpdir),
                current_dt=datetime(2026, 3, 29, 10, 0, 0),
                lookback_hours=24,
                target_keys={("DASH", "LONG")},
                regime_samples=2,
            )

        assert len(recaps) == 1
        recap = recaps[0]
        assert recap.stats_samples == 1
        assert recap.regime_samples == 1
        assert recap.range_start == datetime(2026, 3, 28, 10, 5, 0)
        assert recap.range_end == datetime(2026, 3, 28, 10, 5, 0)
        assert recap.latest_regime == "effective"
        assert recap.latest_regime_ts == datetime(2026, 3, 28, 10, 5, 0)
        assert recap.regime_changes == []

    def test_analyze_recent_pressure_logs_summarizes_current_regime_and_turning_points(self):
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "vibe-quant_2026-03-28.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-03-28 08:19:49.938 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=1 | passive_triggers=9 | active_attempts=1 | price_chg=0.10%",
                        "2026-03-28 08:24:50.005 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=2 | passive_triggers=8 | active_attempts=2 | price_chg=0.20%",
                        "2026-03-28 08:29:50.057 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=3 | passive_triggers=7 | active_attempts=3 | price_chg=0.30%",
                        "2026-03-28 08:34:50.057 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=4 | passive_triggers=6 | active_attempts=4 | price_chg=0.40%",
                        "2026-03-28 08:39:50.057 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=1 | passive_triggers=9 | active_attempts=1 | price_chg=0.50%",
                        "2026-03-28 08:44:50.057 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=0 | passive_triggers=10 | active_attempts=0 | price_chg=0.60%",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recaps = analyze_recent_pressure_logs(
                Path(tmpdir),
                current_dt=datetime(2026, 3, 28, 10, 0, 0),
                lookback_hours=24,
                target_keys={("DASH", "LONG")},
                regime_samples=2,
            )

        assert len(recaps) == 1
        recap = recaps[0]
        assert recap.symbol == "DASH"
        assert recap.side == "LONG"
        assert recap.window_label == "5m"
        assert recap.stats_samples == 6
        assert recap.regime_samples == 5
        assert recap.latest_regime == "degrading"
        assert recap.latest_score == -3
        assert recap.regime_changes[-1] == "03-28 08:44 effective->degrading"
        assert "当前 side 的经验规则在衰减" in recap.interpretation

    def test_analyze_recent_pressure_logs_accepts_omitted_optional_fields(self):
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "vibe-quant_2026-03-28.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-03-28 08:19:49.938 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=10 | passive_triggers=100 | active_attempts=5",
                        "2026-03-28 08:24:50.005 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=12 | passive_triggers=90 | active_attempts=6 | price_chg=0.10%",
                        "2026-03-28 08:29:50.057 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=5m | "
                        "active_triggers=14 | passive_triggers=80 | active_attempts=7 | price_chg=0.20%",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recaps = analyze_recent_pressure_logs(
                Path(tmpdir),
                current_dt=datetime(2026, 3, 28, 10, 0, 0),
                lookback_hours=24,
                target_keys={("DASH", "LONG")},
                regime_samples=2,
            )

        assert len(recaps) == 1
        recap = recaps[0]
        assert recap.stats_samples == 3
        assert recap.regime_samples == 1
        assert recap.latest_regime == "effective"
        assert recap.latest_active_attempts_corr is not None

    def test_analyze_recent_pressure_logs_respects_configured_window_label(self):
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "vibe-quant_2026-03-28.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-03-28 08:19:49.938 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=1m | "
                        "active_triggers=4 | passive_triggers=30 | active_attempts=2 | price_chg=0.20%",
                        "2026-03-28 08:20:49.938 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=LONG | window=1m | "
                        "active_triggers=6 | passive_triggers=20 | active_attempts=3 | price_chg=0.30%",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recaps = analyze_recent_pressure_logs(
                Path(tmpdir),
                current_dt=datetime(2026, 3, 28, 10, 0, 0),
                lookback_hours=24,
                target_keys={("DASH", "LONG")},
                window_label="1m",
                regime_samples=2,
            )

        assert len(recaps) == 1
        recap = recaps[0]
        assert recap.window_label == "1m"
        assert recap.latest_regime == "effective"

    def test_analyze_recent_pressure_logs_uses_side_adjusted_return_for_short(self):
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "vibe-quant_2026-03-28.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-03-28 08:19:49.938 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=SHORT | window=5m | "
                        "active_triggers=10 | passive_triggers=100 | active_attempts=5 | price_chg=-0.10%",
                        "2026-03-28 08:24:50.005 | INFO    | src.utils.logger:log_event:262 | "
                        "[PRESSURE_STATS] 盘口量统计 | symbol=DASH | side=SHORT | window=5m | "
                        "active_triggers=12 | passive_triggers=90 | active_attempts=6 | price_chg=-0.20%",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recaps = analyze_recent_pressure_logs(
                Path(tmpdir),
                current_dt=datetime(2026, 3, 28, 10, 0, 0),
                lookback_hours=24,
                target_keys={("DASH", "SHORT")},
                regime_samples=2,
            )

        assert len(recaps) == 1
        recap = recaps[0]
        assert recap.side == "SHORT"
        assert recap.latest_regime == "effective"
        assert recap.overall_active_attempts_corr is not None and recap.overall_active_attempts_corr > 0
        assert recap.overall_passive_triggers_corr is not None and recap.overall_passive_triggers_corr < 0
