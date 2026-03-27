"""PressureStatsCollector 单元测试"""

from decimal import Decimal

import pytest

from src.stats.pressure_stats import PressureStatsCollector, _window_label


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
    def _make(self, **kw) -> PressureStatsCollector:
        defaults = dict(price_sample_interval_ms=0)  # 关闭节流
        defaults.update(kw)
        return PressureStatsCollector(**defaults)

    def test_empty_window(self):
        c = self._make()
        stats = c.compute_window("BTC/USDT:USDT", "LONG", 60_000, 100_000)
        assert stats["active_signals"] == 0
        assert stats["passive_signals"] == 0
        assert stats["passive_fill_rate"] is None
        assert stats["price_change_pct"] is None

    def test_signal_counting(self):
        c = self._make()
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=2000)
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("102"), ts_ms=3000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["active_signals"] == 2
        assert stats["passive_signals"] == 1

    def test_signal_window_filtering(self):
        """窗口外的事件不计入。"""
        c = self._make()
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("101"), ts_ms=5000)

        # 窗口 = 3000ms，current = 6000 → cutoff = 3000 → 只有 ts=5000 计入
        stats = c.compute_window("BTC", "LONG", 3000, 6000)
        assert stats["active_signals"] == 1

    def test_outcome_fill_rate(self):
        c = self._make()
        # 5 passive signals, 2 filled
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=500)
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=600)
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=700)
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=800)
        c.record_signal("BTC", "LONG", is_active=False, mid_price=Decimal("100"), ts_ms=900)
        c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=1000)
        c.record_outcome("BTC", "LONG", is_active=False, is_filled=True, ts_ms=2000)
        # 2 active signals, 1 filled
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3500)
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=3600)
        c.record_outcome("BTC", "LONG", is_active=True, is_filled=True, ts_ms=4000)

        stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        assert stats["passive_fills"] == 2
        assert stats["passive_fill_rate"] == Decimal("0.400")  # 2/5
        assert stats["active_fills"] == 1
        assert stats["active_fill_rate"] == Decimal("0.500")  # 1/2

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
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_signal("BTC", "SHORT", is_active=False, mid_price=Decimal("100"), ts_ms=1000)

        long_stats = c.compute_window("BTC", "LONG", 60_000, 10_000)
        short_stats = c.compute_window("BTC", "SHORT", 60_000, 10_000)

        assert long_stats["active_signals"] == 1
        assert long_stats["passive_signals"] == 0
        assert short_stats["active_signals"] == 0
        assert short_stats["passive_signals"] == 1

    def test_different_symbols(self):
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.record_signal("ETH", "LONG", is_active=True, mid_price=Decimal("50"), ts_ms=1000)

        btc = c.compute_window("BTC", "LONG", 60_000, 10_000)
        eth = c.compute_window("ETH", "LONG", 60_000, 10_000)

        assert btc["active_signals"] == 1
        assert eth["active_signals"] == 1

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
            c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=i * 1000)

        # 只保留最近 5 条
        stats = c.compute_window("BTC", "LONG", 100_000, 20_000)
        assert stats["active_signals"] == 5


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
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=1000)
        c.log_all_windows(current_ms=1_000_000)  # 1000s 之后
        # 1min 窗口应该没有输出（信号在 1s 远超 1min 前）
        # 但 15min 窗口有

    def test_outputs_when_data_present(self):
        c = PressureStatsCollector(price_sample_interval_ms=0)
        c.record_signal("BTC", "LONG", is_active=True, mid_price=Decimal("100"), ts_ms=5000)
        c.record_price("BTC", Decimal("100"), ts_ms=5000)
        c.record_price("BTC", Decimal("101"), ts_ms=6000)
        # 不应抛异常
        c.log_all_windows(current_ms=10_000)
