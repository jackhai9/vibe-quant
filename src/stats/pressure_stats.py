# Input: signal events, order outcomes, price ticks from bookTicker
# Output: windowed statistics correlating active trigger rate, passive fill rate, and price movement
# Pos: side-channel stats collector for orderbook_pressure strategy analysis
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
orderbook_pressure 策略统计收集器

旁路收集主动/被动信号、订单成交、价格快照，按窗口聚合输出，
用于探索主动触发率、被动成交率、价格走势三者之间的相关性。

不侵入核心交易路径，纯内存 deque，进程重启后清零。
"""

from collections import deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Deque, Dict, List, Literal, Optional, Tuple

from src.utils.logger import log_event

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

SignalType = Literal["active", "passive"]
# (timestamp_ms, signal_type, mid_price)
SignalRecord = Tuple[int, SignalType, Decimal]
# (timestamp_ms, signal_type, filled)
OutcomeRecord = Tuple[int, SignalType, bool]
# (timestamp_ms, mid_price)
PriceTick = Tuple[int, Decimal]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_MAX_EVENTS = 20_000
_PRICE_SAMPLE_INTERVAL_MS = 5_000  # 价格采样间隔
_DEFAULT_WINDOWS_MS: List[int] = [
    60_000,     # 1 min
    300_000,    # 5 min
    900_000,    # 15 min
]


def _window_label(ms: int) -> str:
    """将毫秒窗口转为可读标签：60000 → '1m', 300000 → '5m'。"""
    seconds = ms // 1000
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class PressureStatsCollector:
    """orderbook_pressure 旁路统计收集器。

    三类事件分别存入 deque 环形缓冲区（keyed by ``symbol:side``）：
    - signals:     信号触发（含 active/passive 分类和当时 mid-price）
    - outcomes:    订单终态（filled/cancelled，含 active/passive 分类）
    - price_ticks: 定期采样 mid-price（per symbol）
    """

    def __init__(
        self,
        *,
        max_events: int = _MAX_EVENTS,
        price_sample_interval_ms: int = _PRICE_SAMPLE_INTERVAL_MS,
        windows_ms: Optional[List[int]] = None,
    ) -> None:
        self._max_events = max_events
        self._price_sample_interval_ms = price_sample_interval_ms
        self._windows_ms = windows_ms or list(_DEFAULT_WINDOWS_MS)

        # keyed by "SYMBOL:SIDE"
        self._signals: Dict[str, Deque[SignalRecord]] = {}
        self._outcomes: Dict[str, Deque[OutcomeRecord]] = {}

        # price ticks keyed by symbol only (与 side 无关)
        self._price_ticks: Dict[str, Deque[PriceTick]] = {}
        self._last_price_sample_ms: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _key(symbol: str, side: str) -> str:
        return f"{symbol}|{side}"

    def _get_signals(self, key: str) -> Deque[SignalRecord]:
        buf = self._signals.get(key)
        if buf is None:
            buf = deque(maxlen=self._max_events)
            self._signals[key] = buf
        return buf

    def _get_outcomes(self, key: str) -> Deque[OutcomeRecord]:
        buf = self._outcomes.get(key)
        if buf is None:
            buf = deque(maxlen=self._max_events)
            self._outcomes[key] = buf
        return buf

    def _get_price_ticks(self, symbol: str) -> Deque[PriceTick]:
        buf = self._price_ticks.get(symbol)
        if buf is None:
            buf = deque(maxlen=self._max_events)
            self._price_ticks[symbol] = buf
        return buf

    # ------------------------------------------------------------------
    # 公开方法：事件记录
    # ------------------------------------------------------------------

    def record_signal(
        self,
        symbol: str,
        side: str,
        is_active: bool,
        mid_price: Decimal,
        ts_ms: int,
    ) -> None:
        """记录 pressure 信号触发。"""
        sig_type: SignalType = "active" if is_active else "passive"
        key = self._key(symbol, side)
        self._get_signals(key).append((ts_ms, sig_type, mid_price))

    def record_outcome(
        self,
        symbol: str,
        side: str,
        is_active: bool,
        is_filled: bool,
        ts_ms: int,
    ) -> None:
        """记录 pressure 订单终态（成交或撤单）。"""
        sig_type: SignalType = "active" if is_active else "passive"
        key = self._key(symbol, side)
        self._get_outcomes(key).append((ts_ms, sig_type, is_filled))

    def record_price(
        self,
        symbol: str,
        mid_price: Decimal,
        ts_ms: int,
    ) -> None:
        """采样 mid-price（内部按 sample_interval 节流）。"""
        last = self._last_price_sample_ms.get(symbol, 0)
        if ts_ms - last < self._price_sample_interval_ms:
            return
        self._last_price_sample_ms[symbol] = ts_ms
        self._get_price_ticks(symbol).append((ts_ms, mid_price))

    # ------------------------------------------------------------------
    # 窗口聚合
    # ------------------------------------------------------------------

    def compute_window(
        self,
        symbol: str,
        side: str,
        window_ms: int,
        current_ms: int,
    ) -> dict:
        """计算单个窗口的统计指标。"""
        key = self._key(symbol, side)
        cutoff = current_ms - window_ms

        # --- 信号统计 ---
        active_signals = 0
        passive_signals = 0
        for ts, sig_type, _ in self._get_signals(key):
            if ts >= cutoff:
                if sig_type == "active":
                    active_signals += 1
                else:
                    passive_signals += 1

        # --- 成交统计 ---
        active_fills = 0
        passive_fills = 0
        passive_outcomes_total = 0
        active_outcomes_total = 0
        for ts, sig_type, filled in self._get_outcomes(key):
            if ts >= cutoff:
                if sig_type == "active":
                    active_outcomes_total += 1
                    if filled:
                        active_fills += 1
                else:
                    passive_outcomes_total += 1
                    if filled:
                        passive_fills += 1

        # 成交率：fills / signals（当前只在成交时记录 outcome，分母用信号数）
        passive_fill_rate: Optional[Decimal] = None
        if passive_signals > 0:
            passive_fill_rate = (
                Decimal(passive_fills) / Decimal(passive_signals)
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        active_fill_rate: Optional[Decimal] = None
        if active_signals > 0:
            active_fill_rate = (
                Decimal(active_fills) / Decimal(active_signals)
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        # --- 价格变动 ---
        ticks = self._get_price_ticks(symbol)
        price_start: Optional[Decimal] = None
        price_end: Optional[Decimal] = None
        price_change_pct: Optional[Decimal] = None

        # 找窗口内最早和最晚的价格快照
        for ts, price in ticks:
            if ts >= cutoff:
                if price_start is None:
                    price_start = price
                price_end = price

        if price_start is not None and price_end is not None and price_start > 0:
            price_change_pct = (
                (price_end - price_start) / price_start * Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return {
            "window_ms": window_ms,
            "window_label": _window_label(window_ms),
            "active_signals": active_signals,
            "passive_signals": passive_signals,
            "active_fills": active_fills,
            "active_fill_rate": active_fill_rate,
            "passive_fills": passive_fills,
            "passive_fill_rate": passive_fill_rate,
            "price_start": price_start,
            "price_end": price_end,
            "price_change_pct": price_change_pct,
        }

    # ------------------------------------------------------------------
    # 日志输出
    # ------------------------------------------------------------------

    def log_all_windows(self, current_ms: int) -> None:
        """遍历所有 symbol:side，对每个窗口输出一条结构化日志。"""
        seen_keys: set[str] = set()
        seen_keys.update(self._signals.keys())
        seen_keys.update(self._outcomes.keys())

        for key in sorted(seen_keys):
            parts = key.split("|", 1)
            if len(parts) != 2:
                continue
            symbol, side = parts

            for window_ms in self._windows_ms:
                stats = self.compute_window(symbol, side, window_ms, current_ms)

                # 窗口内无任何事件则跳过
                total_events = (
                    stats["active_signals"]
                    + stats["passive_signals"]
                    + stats["active_fills"]
                    + stats["passive_fills"]
                )
                if total_events == 0:
                    continue

                log_event(
                    "pressure_stats",
                    symbol=symbol,
                    side=side,
                    window=stats["window_label"],
                    active_sig=stats["active_signals"],
                    passive_sig=stats["passive_signals"],
                    active_fills=stats["active_fills"],
                    active_fill_rate=stats["active_fill_rate"],
                    passive_fills=stats["passive_fills"],
                    passive_fill_rate=stats["passive_fill_rate"],
                    price_chg=f"{stats['price_change_pct']}%"
                    if stats["price_change_pct"] is not None
                    else None,
                )
