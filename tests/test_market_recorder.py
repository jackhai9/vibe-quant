# Input: MarketDataRecorder and synthetic MarketEvent values
# Output: pytest assertions for recorder sampling, rotation, and shutdown safety
# Pos: recorder unit tests
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
MarketDataRecorder 单元测试
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from src.models import MarketEvent
from src.stats.market_recorder import MarketDataRecorder
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class TestMarketDataRecorder:
    @staticmethod
    def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
        return int(datetime(year, month, day, hour, 0, 0).timestamp() * 1000)

    @pytest.mark.asyncio
    async def test_record_samples_book_depth_and_agg_trade(self, tmp_path: Path):
        recorder = MarketDataRecorder(
            log_dir=tmp_path,
            book_sample_interval_ms=500,
            depth_sample_interval_ms=1000,
        )
        ts = self._ts(2026, 3, 24)

        await recorder.start()
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts,
                best_bid=Decimal("10.0"),
                best_ask=Decimal("10.2"),
                best_bid_qty=Decimal("5"),
                best_ask_qty=Decimal("6"),
                event_type="book_ticker",
            )
        )
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts + 100,
                best_bid=Decimal("10.1"),
                best_ask=Decimal("10.3"),
                best_bid_qty=Decimal("7"),
                best_ask_qty=Decimal("8"),
                event_type="book_ticker",
            )
        )
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts + 200,
                bid_levels=[(Decimal("10.0"), Decimal("5"))],
                ask_levels=[(Decimal("10.2"), Decimal("6"))],
                event_type="depth",
            )
        )
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts + 500,
                bid_levels=[(Decimal("9.9"), Decimal("4"))],
                ask_levels=[(Decimal("10.3"), Decimal("7"))],
                event_type="depth",
            )
        )
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts + 300,
                last_trade_price=Decimal("10.1"),
                trade_qty=Decimal("0.5"),
                is_buyer_maker=True,
                event_type="agg_trade",
            )
        )
        await recorder.close()

        files = sorted(tmp_path.glob("market_data_*.jsonl"))
        assert len(files) == 1

        lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        assert [line["type"] for line in lines] == ["book_ticker", "depth", "agg_trade"]
        assert lines[0]["bid_qty"] == "5"
        assert lines[1]["bids"] == [["10.0", "5"]]
        assert lines[2]["q"] == "0.5"
        assert lines[2]["m"] is True

    @pytest.mark.asyncio
    async def test_rotate_compress_and_prune(self, tmp_path: Path):
        stale = tmp_path / "market_data_2000-01-01.jsonl.gz"
        stale.write_text("stale", encoding="utf-8")

        recorder = MarketDataRecorder(
            log_dir=tmp_path,
            book_sample_interval_ms=0,
            depth_sample_interval_ms=0,
            retention_days=14,
        )
        ts_day1 = self._ts(2026, 3, 24)
        ts_day2 = self._ts(2026, 3, 25)

        await recorder.start()
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts_day1,
                best_bid=Decimal("10.0"),
                best_ask=Decimal("10.2"),
                best_bid_qty=Decimal("5"),
                best_ask_qty=Decimal("6"),
                event_type="book_ticker",
            )
        )
        recorder.record(
            MarketEvent(
                symbol="DASH/USDT:USDT",
                timestamp_ms=ts_day2,
                best_bid=Decimal("10.1"),
                best_ask=Decimal("10.3"),
                best_bid_qty=Decimal("7"),
                best_ask_qty=Decimal("8"),
                event_type="book_ticker",
            )
        )
        await recorder.close()

        assert not stale.exists()
        assert (tmp_path / "market_data_2026-03-24.jsonl.gz").exists()
        assert not (tmp_path / "market_data_2026-03-24.jsonl").exists()
        assert (tmp_path / "market_data_2026-03-25.jsonl").exists()

    @pytest.mark.asyncio
    async def test_queue_full_warning_is_deduped(self, tmp_path: Path):
        recorder = MarketDataRecorder(log_dir=tmp_path)
        recorder._accepting = True
        recorder._queue = MagicMock()
        recorder._queue.put_nowait.side_effect = asyncio.QueueFull()
        event = MarketEvent(
            symbol="DASH/USDT:USDT",
            timestamp_ms=self._ts(2026, 3, 24),
            best_bid=Decimal("10.0"),
            best_ask=Decimal("10.2"),
            best_bid_qty=Decimal("5"),
            best_ask_qty=Decimal("6"),
            event_type="book_ticker",
        )

        with patch("src.stats.market_recorder.get_logger") as get_logger_mock:
            logger = MagicMock()
            get_logger_mock.return_value = logger
            recorder.record(event)
            recorder.record(event)

        logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_without_start_is_safe(self, tmp_path: Path):
        recorder = MarketDataRecorder(log_dir=tmp_path)
        await recorder.close()
