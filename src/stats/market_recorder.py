# Input: MarketEvent stream and recorder lifecycle calls
# Output: sampled market data JSONL files for offline replay
# Pos: non-blocking market data recorder for bookTicker/depth10/aggTrade
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
Market data recorder.

Records bookTicker, depth10, and aggTrade into daily JSONL files without
blocking the trading path. Rotation, gzip compression, and retention cleanup
run in the background writer flow.
"""

import asyncio
import gzip
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Coroutine, Optional, TextIO

from src.models import MarketEvent
from src.utils.logger import get_logger, log_error


class MarketDataRecorder:
    """Non-blocking recorder for raw market events."""

    def __init__(
        self,
        *,
        log_dir: Path,
        book_sample_interval_ms: int = 500,
        depth_sample_interval_ms: int = 1000,
        retention_days: int = 14,
        queue_max_size: int = 50_000,
    ) -> None:
        self._log_dir = log_dir
        self._book_sample_interval_ms = book_sample_interval_ms
        self._depth_sample_interval_ms = depth_sample_interval_ms
        self._retention_days = retention_days
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max_size)
        self._writer_task: Optional[asyncio.Task[None]] = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._file: Optional[TextIO] = None
        self._current_date: Optional[date] = None
        self._current_path: Optional[Path] = None
        self._accepting = False
        self._queue_full_warned = False
        self._last_book_sample_ms: dict[str, int] = {}
        self._last_depth_sample_ms: dict[str, int] = {}

    async def start(self) -> None:
        """Start the background writer."""
        if self._writer_task is not None and not self._writer_task.done():
            return

        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._accepting = True
        self._writer_task = asyncio.create_task(self._writer_loop())

    def record(self, event: MarketEvent) -> None:
        """Sample and enqueue supported market events."""
        if not self._accepting:
            return

        payload = self._build_payload(event)
        if payload is None:
            return

        try:
            self._queue.put_nowait(payload)
            self._queue_full_warned = False
        except asyncio.QueueFull:
            if not self._queue_full_warned:
                self._queue_full_warned = True
                get_logger().warning(
                    "MarketDataRecorder queue full, dropping incoming market events"
                )

    async def close(self) -> None:
        """Stop receiving, flush queue, and close all resources."""
        self._accepting = False

        if self._writer_task is not None:
            try:
                await self._writer_task
            finally:
                self._writer_task = None

        if self._background_tasks:
            tasks = list(self._background_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()

        self._close_current_file()

    def _build_payload(self, event: MarketEvent) -> Optional[dict[str, Any]]:
        if event.event_type == "book_ticker":
            if (
                event.best_bid is None
                or event.best_ask is None
                or event.best_bid_qty is None
                or event.best_ask_qty is None
            ):
                return None
            if not self._should_sample(self._last_book_sample_ms, event.symbol, event.timestamp_ms, self._book_sample_interval_ms):
                return None
            return {
                "ts": event.timestamp_ms,
                "type": "book_ticker",
                "sym": event.symbol,
                "bid": str(event.best_bid),
                "bid_qty": str(event.best_bid_qty),
                "ask": str(event.best_ask),
                "ask_qty": str(event.best_ask_qty),
            }

        if event.event_type == "depth":
            if event.bid_levels is None or event.ask_levels is None:
                return None
            if not self._should_sample(self._last_depth_sample_ms, event.symbol, event.timestamp_ms, self._depth_sample_interval_ms):
                return None
            return {
                "ts": event.timestamp_ms,
                "type": "depth",
                "sym": event.symbol,
                "bids": [[str(price), str(qty)] for price, qty in event.bid_levels],
                "asks": [[str(price), str(qty)] for price, qty in event.ask_levels],
            }

        if event.event_type == "agg_trade":
            if event.last_trade_price is None or event.trade_qty is None or event.is_buyer_maker is None:
                return None
            return {
                "ts": event.timestamp_ms,
                "type": "agg_trade",
                "sym": event.symbol,
                "p": str(event.last_trade_price),
                "q": str(event.trade_qty),
                "m": event.is_buyer_maker,
            }

        return None

    @staticmethod
    def _should_sample(
        last_samples: dict[str, int],
        symbol: str,
        ts_ms: int,
        interval_ms: int,
    ) -> bool:
        last_ms = last_samples.get(symbol)
        if last_ms is not None and ts_ms - last_ms < interval_ms:
            return False
        last_samples[symbol] = ts_ms
        return True

    async def _writer_loop(self) -> None:
        while self._accepting or not self._queue.empty():
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            try:
                await self._write_payload(payload)
            except Exception as exc:
                log_error(f"MarketDataRecorder writer error: {exc}")
            finally:
                self._queue.task_done()

    async def _write_payload(self, payload: dict[str, Any]) -> None:
        target_date = self._local_date(payload["ts"])
        await self._rotate_if_needed(target_date)
        if self._file is None:
            raise RuntimeError("MarketDataRecorder file handle not initialized")

        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        self._file.write(f"{line}\n")

    async def _rotate_if_needed(self, target_date: date) -> None:
        if self._current_date == target_date and self._file is not None:
            return

        old_path = self._current_path
        self._close_current_file()
        self._current_date = target_date
        self._current_path = self._file_path_for_date(target_date)
        self._file = self._current_path.open("a", encoding="utf-8", buffering=1)

        if old_path is not None and old_path != self._current_path:
            self._spawn_background_task(self._compress_and_prune(old_path))

    def _spawn_background_task(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _compress_and_prune(self, path: Path) -> None:
        try:
            if path.exists():
                await asyncio.to_thread(self._gzip_file, path)
            await asyncio.to_thread(self._prune_expired_files)
        except Exception as exc:
            log_error(f"MarketDataRecorder rotate/compress error: {exc}")

    @staticmethod
    def _gzip_file(path: Path) -> None:
        gz_path = path.with_suffix(f"{path.suffix}.gz")
        with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        path.unlink(missing_ok=True)

    def _prune_expired_files(self) -> None:
        cutoff = datetime.now().date() - timedelta(days=self._retention_days)
        for gz_path in self._log_dir.glob("market_data_*.jsonl.gz"):
            try:
                day = datetime.strptime(gz_path.name[len("market_data_"):-len(".jsonl.gz")], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                gz_path.unlink(missing_ok=True)

    def _close_current_file(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
        finally:
            self._file.close()
            self._file = None

    @staticmethod
    def _local_date(ts_ms: int) -> date:
        return datetime.fromtimestamp(ts_ms / 1000).date()

    def _file_path_for_date(self, target_date: date) -> Path:
        return self._log_dir / f"market_data_{target_date.isoformat()}.jsonl"
