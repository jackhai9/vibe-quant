# Input: pressure trigger/attempt/outcome events, price ticks from bookTicker
# Output: windowed statistics correlating trigger frequency, fill rate, and price movement, plus rolling pressure regime state
# Pos: side-channel stats collector for orderbook_pressure strategy analysis and regime tracking
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
orderbook_pressure 策略统计收集器

旁路收集主动/被动 trigger、成功下单、订单结果、价格快照，按窗口聚合输出，
用于探索主动触发频率、被动成交率与价格走势之间的相关性，并基于配置指定的
滚动窗口维护 `effective / degrading / failed / recovering` 的经验性 regime 状态。

不侵入核心交易路径，纯内存 deque，进程重启后清零。
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt
from typing import Deque, Dict, List, Literal, Optional, Tuple

from src.utils.logger import log_event

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

PressurePath = Literal["active", "passive"]
# (timestamp_ms, path, mid_price)
TriggerRecord = Tuple[int, PressurePath, Decimal]
# (timestamp_ms, path, mid_price)
AttemptRecord = Tuple[int, PressurePath, Decimal]
# (timestamp_ms, path, filled)
OutcomeRecord = Tuple[int, PressurePath, bool]
# (timestamp_ms, mid_price)
PriceTick = Tuple[int, Decimal]
PressureRegime = Literal["effective", "degrading", "failed", "recovering"]

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
_REGIME_HISTORY_MAX = 48


@dataclass
class RegimeSnapshot:
    ts_ms: int
    active_triggers: int
    passive_triggers: int
    active_attempts: int
    passive_fill_rate: Optional[Decimal]
    price_change_pct: Decimal


@dataclass
class RegimeTracker:
    state: Optional[PressureRegime] = None
    strong_streak: int = 0
    weak_streak: int = 0


@dataclass
class RegimeEvaluation:
    state: PressureRegime
    score: int
    samples: int
    active_attempts_corr: Optional[float]
    active_triggers_corr: Optional[float]
    passive_triggers_corr: Optional[float]
    passive_fill_rate_corr: Optional[float]
    prev_state: Optional[PressureRegime]


def _window_label(ms: int) -> str:
    """将毫秒窗口转为可读标签：60000 → '1m', 300000 → '5m'。"""
    seconds = ms // 1000
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class PressureStatsCollector:
    """orderbook_pressure 旁路统计收集器。

    四类事件分别存入 deque 环形缓冲区（keyed by ``symbol:side``）：
    - triggers:    trigger 边沿（含 active/passive 分类和当时 mid-price）
    - attempts:    成功下单（含 active/passive 分类和当时 mid-price）
    - outcomes:    订单结果（filled/cancelled，含 active/passive 分类）
    - price_ticks: 定期采样 mid-price（per symbol）
    """

    def __init__(
        self,
        *,
        max_events: int = _MAX_EVENTS,
        price_sample_interval_ms: int = _PRICE_SAMPLE_INTERVAL_MS,
        windows_ms: Optional[List[int]] = None,
        regime_window_ms: int = 300_000,
        regime_samples: int = 12,
    ) -> None:
        self._max_events = max_events
        self._price_sample_interval_ms = price_sample_interval_ms
        self._windows_ms = windows_ms or list(_DEFAULT_WINDOWS_MS)
        self._regime_window_ms = regime_window_ms
        self._regime_samples = regime_samples

        # keyed by "SYMBOL:SIDE"
        self._triggers: Dict[str, Deque[TriggerRecord]] = {}
        self._attempts: Dict[str, Deque[AttemptRecord]] = {}
        self._outcomes: Dict[str, Deque[OutcomeRecord]] = {}

        # price ticks keyed by symbol only (与 side 无关)
        self._price_ticks: Dict[str, Deque[PriceTick]] = {}
        self._last_price_sample_ms: Dict[str, int] = {}
        self._regime_snapshots: Dict[str, Deque[RegimeSnapshot]] = {}
        self._regime_trackers: Dict[str, RegimeTracker] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _key(symbol: str, side: str) -> str:
        return f"{symbol}|{side}"

    def _get_triggers(self, key: str) -> Deque[TriggerRecord]:
        buf = self._triggers.get(key)
        if buf is None:
            buf = deque(maxlen=self._max_events)
            self._triggers[key] = buf
        return buf

    def _get_attempts(self, key: str) -> Deque[AttemptRecord]:
        buf = self._attempts.get(key)
        if buf is None:
            buf = deque(maxlen=self._max_events)
            self._attempts[key] = buf
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

    def _get_regime_snapshots(self, key: str) -> Deque[RegimeSnapshot]:
        buf = self._regime_snapshots.get(key)
        if buf is None:
            buf = deque(maxlen=_REGIME_HISTORY_MAX)
            self._regime_snapshots[key] = buf
        return buf

    def _get_regime_tracker(self, key: str) -> RegimeTracker:
        tracker = self._regime_trackers.get(key)
        if tracker is None:
            tracker = RegimeTracker()
            self._regime_trackers[key] = tracker
        return tracker

    @staticmethod
    def _corr(xs: List[float], ys: List[float]) -> Optional[float]:
        if len(xs) != len(ys) or len(xs) < 2:
            return None
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        std_x = sqrt(sum((x - mean_x) ** 2 for x in xs))
        std_y = sqrt(sum((y - mean_y) ** 2 for y in ys))
        if std_x == 0 or std_y == 0:
            return None
        return cov / (std_x * std_y)

    def _record_regime_snapshot(
        self,
        symbol: str,
        side: str,
        *,
        ts_ms: int,
        active_triggers: int,
        passive_triggers: int,
        active_attempts: int,
        passive_fill_rate: Optional[Decimal],
        price_change_pct: Decimal,
    ) -> None:
        key = self._key(symbol, side)
        buf = self._get_regime_snapshots(key)
        if buf and buf[-1].ts_ms == ts_ms:
            buf[-1] = RegimeSnapshot(
                ts_ms=ts_ms,
                active_triggers=active_triggers,
                passive_triggers=passive_triggers,
                active_attempts=active_attempts,
                passive_fill_rate=passive_fill_rate,
                price_change_pct=price_change_pct,
            )
            return
        buf.append(
            RegimeSnapshot(
                ts_ms=ts_ms,
                active_triggers=active_triggers,
                passive_triggers=passive_triggers,
                active_attempts=active_attempts,
                passive_fill_rate=passive_fill_rate,
                price_change_pct=price_change_pct,
            )
        )

    @staticmethod
    def _score_regime(
        *,
        active_attempts_corr: Optional[float],
        active_triggers_corr: Optional[float],
        passive_triggers_corr: Optional[float],
        passive_fill_rate_corr: Optional[float],
    ) -> int:
        score = 0

        if active_attempts_corr is not None:
            if active_attempts_corr >= 0.15:
                score += 2
            elif active_attempts_corr >= 0.05:
                score += 1
            elif active_attempts_corr < 0:
                score -= 1

        if active_triggers_corr is not None:
            if active_triggers_corr >= 0.10:
                score += 1
            elif active_triggers_corr < 0:
                score -= 1

        if passive_triggers_corr is not None:
            if passive_triggers_corr <= -0.15:
                score += 2
            elif passive_triggers_corr <= -0.05:
                score += 1
            elif passive_triggers_corr > 0:
                score -= 1

        if passive_fill_rate_corr is not None:
            if passive_fill_rate_corr >= 0.10:
                score += 1
            elif passive_fill_rate_corr <= -0.10:
                score -= 1

        return score

    @staticmethod
    def _advance_regime_state(tracker: RegimeTracker, score: int) -> tuple[PressureRegime, Optional[PressureRegime]]:
        prev_state = tracker.state

        if score >= 4:
            tracker.strong_streak += 1
            tracker.weak_streak = 0
            if prev_state in (None, "effective"):
                tracker.state = "effective"
            else:
                tracker.state = "recovering" if tracker.strong_streak < 2 else "effective"
            return tracker.state, prev_state

        if score <= 0:
            tracker.weak_streak += 1
            tracker.strong_streak = 0
            if prev_state in ("effective", "recovering") and tracker.weak_streak < 2:
                tracker.state = "degrading"
            else:
                tracker.state = "failed"
            return tracker.state, prev_state

        tracker.strong_streak = 0
        tracker.weak_streak = 0
        if prev_state in ("failed", "recovering"):
            tracker.state = "recovering"
        else:
            tracker.state = "degrading"
        return tracker.state, prev_state

    def _evaluate_regime(self, symbol: str, side: str) -> Optional[RegimeEvaluation]:
        key = self._key(symbol, side)
        snapshots = list(self._get_regime_snapshots(key))
        if len(snapshots) < self._regime_samples:
            return None

        price_changes = [float(s.price_change_pct) for s in snapshots]
        active_attempts = [float(s.active_attempts) for s in snapshots]
        active_triggers = [float(s.active_triggers) for s in snapshots]
        passive_triggers = [float(s.passive_triggers) for s in snapshots]

        active_attempts_corr = self._corr(active_attempts, price_changes)
        active_triggers_corr = self._corr(active_triggers, price_changes)
        passive_triggers_corr = self._corr(passive_triggers, price_changes)

        pfr_xs: List[float] = []
        pfr_ys: List[float] = []
        for snapshot in snapshots:
            if snapshot.passive_fill_rate is None:
                continue
            pfr_xs.append(float(snapshot.passive_fill_rate))
            pfr_ys.append(float(snapshot.price_change_pct))
        passive_fill_rate_corr = self._corr(pfr_xs, pfr_ys) if len(pfr_xs) >= max(6, self._regime_samples // 2) else None

        score = self._score_regime(
            active_attempts_corr=active_attempts_corr,
            active_triggers_corr=active_triggers_corr,
            passive_triggers_corr=passive_triggers_corr,
            passive_fill_rate_corr=passive_fill_rate_corr,
        )
        tracker = self._get_regime_tracker(key)
        state, prev_state = self._advance_regime_state(tracker, score)
        return RegimeEvaluation(
            state=state,
            score=score,
            samples=len(snapshots),
            active_attempts_corr=active_attempts_corr,
            active_triggers_corr=active_triggers_corr,
            passive_triggers_corr=passive_triggers_corr,
            passive_fill_rate_corr=passive_fill_rate_corr,
            prev_state=prev_state,
        )

    # ------------------------------------------------------------------
    # 公开方法：事件记录
    # ------------------------------------------------------------------

    def record_trigger(
        self,
        symbol: str,
        side: str,
        is_active: bool,
        mid_price: Decimal,
        ts_ms: int,
    ) -> None:
        """记录 pressure trigger 边沿。"""
        path: PressurePath = "active" if is_active else "passive"
        key = self._key(symbol, side)
        self._get_triggers(key).append((ts_ms, path, mid_price))

    def record_attempt(
        self,
        symbol: str,
        side: str,
        is_active: bool,
        mid_price: Decimal,
        ts_ms: int,
    ) -> None:
        """记录 pressure 成功下单事件。"""
        path: PressurePath = "active" if is_active else "passive"
        key = self._key(symbol, side)
        self._get_attempts(key).append((ts_ms, path, mid_price))

    def record_outcome(
        self,
        symbol: str,
        side: str,
        is_active: bool,
        is_filled: bool,
        ts_ms: int,
    ) -> None:
        """记录 pressure 订单结果（当前主流程记录首次成交）。"""
        path: PressurePath = "active" if is_active else "passive"
        key = self._key(symbol, side)
        self._get_outcomes(key).append((ts_ms, path, is_filled))

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

        # --- trigger 统计 ---
        active_triggers = 0
        passive_triggers = 0
        for ts, path, _ in self._get_triggers(key):
            if ts >= cutoff:
                if path == "active":
                    active_triggers += 1
                else:
                    passive_triggers += 1

        # --- 成功下单统计 ---
        active_attempts = 0
        passive_attempts = 0
        for ts, path, _ in self._get_attempts(key):
            if ts >= cutoff:
                if path == "active":
                    active_attempts += 1
                else:
                    passive_attempts += 1

        # --- 成交统计 ---
        active_fills = 0
        passive_fills = 0
        for ts, path, filled in self._get_outcomes(key):
            if ts >= cutoff:
                if path == "active":
                    if filled:
                        active_fills += 1
                else:
                    if filled:
                        passive_fills += 1

        # 成交率：fills / attempts（当前只在首次成交时记录 filled outcome）
        passive_fill_rate: Optional[Decimal] = None
        if passive_attempts > 0:
            passive_fill_rate = (
                Decimal(passive_fills) / Decimal(passive_attempts)
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        active_fill_rate: Optional[Decimal] = None
        if active_attempts > 0:
            active_fill_rate = (
                Decimal(active_fills) / Decimal(active_attempts)
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
            "active_triggers": active_triggers,
            "passive_triggers": passive_triggers,
            "active_attempts": active_attempts,
            "passive_attempts": passive_attempts,
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
        seen_keys.update(self._triggers.keys())
        seen_keys.update(self._attempts.keys())
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
                    stats["active_triggers"]
                    + stats["passive_triggers"]
                    + stats["active_attempts"]
                    + stats["passive_attempts"]
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
                    active_triggers=stats["active_triggers"],
                    passive_triggers=stats["passive_triggers"],
                    active_attempts=stats["active_attempts"],
                    passive_attempts=stats["passive_attempts"],
                    active_fills=stats["active_fills"],
                    active_fill_rate=stats["active_fill_rate"],
                    passive_fills=stats["passive_fills"],
                    passive_fill_rate=stats["passive_fill_rate"],
                    price_chg=f"{stats['price_change_pct']}%"
                    if stats["price_change_pct"] is not None
                    else None,
                )

            regime_stats = self.compute_window(symbol, side, self._regime_window_ms, current_ms)
            regime_total_events = (
                regime_stats["active_triggers"]
                + regime_stats["passive_triggers"]
                + regime_stats["active_attempts"]
                + regime_stats["passive_attempts"]
                + regime_stats["active_fills"]
                + regime_stats["passive_fills"]
            )
            if regime_total_events == 0 or regime_stats["price_change_pct"] is None:
                continue

            self._record_regime_snapshot(
                symbol,
                side,
                ts_ms=current_ms,
                active_triggers=regime_stats["active_triggers"],
                passive_triggers=regime_stats["passive_triggers"],
                active_attempts=regime_stats["active_attempts"],
                passive_fill_rate=regime_stats["passive_fill_rate"],
                price_change_pct=regime_stats["price_change_pct"],
            )
            regime = self._evaluate_regime(symbol, side)
            if regime is None:
                continue

            log_event(
                "pressure_regime",
                symbol=symbol,
                side=side,
                window=_window_label(self._regime_window_ms),
                regime=regime.state,
                prev_regime=regime.prev_state if regime.prev_state != regime.state else None,
                regime_score=regime.score,
                samples=regime.samples,
                active_attempts_corr=round(regime.active_attempts_corr, 3)
                if regime.active_attempts_corr is not None
                else None,
                active_triggers_corr=round(regime.active_triggers_corr, 3)
                if regime.active_triggers_corr is not None
                else None,
                passive_triggers_corr=round(regime.passive_triggers_corr, 3)
                if regime.passive_triggers_corr is not None
                else None,
                passive_fill_rate_corr=round(regime.passive_fill_rate_corr, 3)
                if regime.passive_fill_rate_corr is not None
                else None,
            )
