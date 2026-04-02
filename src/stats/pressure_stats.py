# Input: pressure trigger/attempt/outcome events, price ticks from bookTicker, optional persisted regime snapshot state, and recent structured pressure logs
# Output: windowed statistics correlating trigger frequency, fill rate, and raw price movement, plus rolling side-adjusted pressure regime state, startup recap summaries, and periodic pressure reports
# Pos: side-channel stats collector for orderbook_pressure strategy analysis, side-adjusted regime tracking, short-gap state resume, startup recap, and ongoing report generation
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
orderbook_pressure 策略统计收集器

旁路收集主动/被动 trigger、成功下单、订单结果、价格快照，按窗口聚合输出，
用于探索主动触发频率、被动成交率与价格走势之间的相关性，并基于配置指定的
滚动窗口维护 `effective / degrading / failed / recovering` 的经验性 regime 状态。
`PRESSURE_STATS` 继续输出原始同窗 `price_chg`，而 `PRESSURE_REGIME`、`PRESSURE_RECAP`
和 `PRESSURE_REPORT` 使用按 side 调整后的 same-window return（LONG=`+price_chg`，
SHORT=`-price_chg`）来评估当前经验规则是否仍成立。

不侵入核心交易路径；运行期事件缓冲仍为内存 deque，但 `PRESSURE_REGIME`
会周期性导出最近滚动快照，供短暂停机后的启动恢复。
同时提供最近日志的 startup recap 分析，以及运行中的周期性压力报告，用于在
启动后快速回顾过去 24 小时的 regime 变化、转折时间点和当前经验规则状态，并在运行期持续输出同格式摘要。
"""

import gzip
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, Optional, Sequence, Tuple, cast

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
_RECAP_WARMUP_MAX_DAYS = 7


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


@dataclass
class RegimeLogEntry:
    symbol: str
    side: str
    window_label: str
    regime: PressureRegime
    prev_regime: Optional[PressureRegime]
    score: int
    samples: int
    active_attempts_corr: Optional[float] = None
    active_triggers_corr: Optional[float] = None
    passive_triggers_corr: Optional[float] = None
    passive_fill_rate_corr: Optional[float] = None


@dataclass
class PressureWindowReport:
    window_label: str
    active_triggers: int
    passive_triggers: int
    active_attempts: int
    passive_attempts: int
    active_fills: int
    active_fill_rate: Optional[Decimal]
    passive_fills: int
    passive_fill_rate: Optional[Decimal]
    price_change_pct: Optional[Decimal]


@dataclass
class PressurePeriodicReport:
    symbol: str
    side: str
    as_of_ms: int
    window_reports: List[PressureWindowReport]
    regime_entry: Optional[RegimeLogEntry]


@dataclass
class PressureStatsLogEntry:
    ts: datetime
    symbol: str
    side: str
    window_label: str
    active_triggers: int
    passive_triggers: int
    active_attempts: int
    passive_fill_rate: Optional[float]
    price_change_pct: Optional[float]


@dataclass
class PressureRegimeHistoryEntry:
    ts: datetime
    symbol: str
    side: str
    window_label: str
    regime: PressureRegime
    prev_regime: Optional[PressureRegime]
    score: int
    samples: int
    active_attempts_corr: Optional[float]
    active_triggers_corr: Optional[float]
    passive_triggers_corr: Optional[float]
    passive_fill_rate_corr: Optional[float]


@dataclass
class PressureRecap:
    symbol: str
    side: str
    window_label: str
    range_start: datetime
    range_end: datetime
    stats_samples: int
    regime_samples: int
    overall_active_attempts_corr: Optional[float]
    overall_active_triggers_corr: Optional[float]
    overall_passive_triggers_corr: Optional[float]
    overall_passive_fill_rate_corr: Optional[float]
    latest_regime: Optional[PressureRegime]
    latest_regime_ts: Optional[datetime]
    latest_score: Optional[int]
    latest_regime_samples: Optional[int]
    latest_active_attempts_corr: Optional[float]
    latest_active_triggers_corr: Optional[float]
    latest_passive_triggers_corr: Optional[float]
    latest_passive_fill_rate_corr: Optional[float]
    regime_changes: List[str]
    interpretation: str


_PRESSURE_STATS_MARKER = "[PRESSURE_STATS]"
_PRESSURE_REGIME_MARKER = "[PRESSURE_REGIME]"
_LOG_FILE_RE = re.compile(r"vibe-quant_(?P<date>\d{4}-\d{2}-\d{2})\.log(?:\.gz)?$")


def _parse_percent_or_none(value: str) -> Optional[float]:
    raw = value.strip()
    if raw in {"", "None", "-"}:
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    return float(raw)


def _parse_log_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")


def _short_symbol(symbol: str) -> str:
    return symbol.split("/")[0]


def _side_adjust_price_change(side: str, price_change_pct: float) -> float:
    return -price_change_pct if side.strip().upper() == "SHORT" else price_change_pct


def _iter_recent_log_paths(log_dir: Path, since: datetime, until: datetime) -> List[Path]:
    paths: List[Path] = []
    start_date = since.date()
    end_date = until.date()
    for path in sorted(log_dir.glob("vibe-quant_*.log*")):
        match = _LOG_FILE_RE.fullmatch(path.name)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group("date"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date <= file_date <= end_date:
            paths.append(path)
    return paths


def _parse_structured_log_fields(line: str, marker: str) -> Optional[tuple[datetime, dict[str, str]]]:
    if marker not in line:
        return None
    try:
        ts = _parse_log_ts(line[:19])
    except ValueError:
        return None

    payload = line.split(marker, 1)[1].strip()
    fields: dict[str, str] = {}
    for part in payload.split(" | "):
        segment = part.strip()
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        fields[key.strip()] = value.strip()
    return ts, fields


def _parse_pressure_stats_line(line: str) -> Optional[PressureStatsLogEntry]:
    parsed = _parse_structured_log_fields(line, _PRESSURE_STATS_MARKER)
    if parsed is None:
        return None
    ts, data = parsed
    try:
        return PressureStatsLogEntry(
            ts=ts,
            symbol=data["symbol"].strip(),
            side=data["side"].strip(),
            window_label=data["window"].strip(),
            active_triggers=int(data["active_triggers"]),
            passive_triggers=int(data["passive_triggers"]),
            active_attempts=int(data["active_attempts"]),
            passive_fill_rate=_parse_percent_or_none(data["passive_fill_rate"])
            if "passive_fill_rate" in data
            else None,
            price_change_pct=_parse_percent_or_none(data["price_chg"])
            if "price_chg" in data
            else None,
        )
    except (KeyError, ValueError):
        return None


def _parse_pressure_regime_line(line: str) -> Optional[PressureRegimeHistoryEntry]:
    parsed = _parse_structured_log_fields(line, _PRESSURE_REGIME_MARKER)
    if parsed is None:
        return None
    ts, data = parsed
    valid_states: set[PressureRegime] = {"effective", "degrading", "failed", "recovering"}
    regime = data["regime"].strip()
    prev_regime = data.get("prev_regime")
    prev_regime = prev_regime.strip() if prev_regime else None
    if regime not in valid_states:
        return None
    if prev_regime is not None and prev_regime not in valid_states:
        return None
    regime_typed = cast(PressureRegime, regime)
    prev_regime_typed = cast(Optional[PressureRegime], prev_regime)
    try:
        score_raw = data.get("regime_score", data.get("score"))
        if score_raw is None:
            return None
        return PressureRegimeHistoryEntry(
            ts=ts,
            symbol=data["symbol"].strip(),
            side=data["side"].strip(),
            window_label=data["window"].strip(),
            regime=regime_typed,
            prev_regime=prev_regime_typed,
            score=int(score_raw),
            samples=int(data["samples"]),
            active_attempts_corr=_parse_percent_or_none(data["active_attempts_corr"])
            if "active_attempts_corr" in data
            else None,
            active_triggers_corr=_parse_percent_or_none(data["active_triggers_corr"])
            if "active_triggers_corr" in data
            else None,
            passive_triggers_corr=_parse_percent_or_none(data["passive_triggers_corr"])
            if "passive_triggers_corr" in data
            else None,
            passive_fill_rate_corr=_parse_percent_or_none(data["passive_fill_rate_corr"])
            if "passive_fill_rate_corr" in data
            else None,
        )
    except (KeyError, ValueError):
        return None


def _build_recap_interpretation(
    latest_regime: Optional[PressureRegime],
    *,
    overall_active_attempts_corr: Optional[float],
    overall_passive_triggers_corr: Optional[float],
    overall_passive_fill_rate_corr: Optional[float],
    latest_active_attempts_corr: Optional[float],
    latest_passive_triggers_corr: Optional[float],
    latest_passive_fill_rate_corr: Optional[float],
) -> str:
    base = {
        "effective": "当前 side 的经验规则仍有效",
        "degrading": "当前 side 的经验规则在衰减，警惕 regime shift",
        "failed": "当前 side 的经验规则失效，应优先视为 regime shift 警报",
        "recovering": "当前 side 的经验规则在恢复，但尚未重新稳定",
        None: "当前样本不足，尚未形成可用的 regime 判断",
    }[latest_regime]

    notes: List[str] = []
    if (
        overall_active_attempts_corr is not None
        and latest_active_attempts_corr is not None
        and overall_active_attempts_corr > 0.05
        and latest_active_attempts_corr < 0
    ):
        notes.append("active_attempts 已从正相关转负")
    if (
        overall_passive_triggers_corr is not None
        and latest_passive_triggers_corr is not None
        and overall_passive_triggers_corr < -0.05
        and latest_passive_triggers_corr >= 0
    ):
        notes.append("passive_triggers 已失去反向参考作用")
    if (
        overall_passive_fill_rate_corr is not None
        and latest_passive_fill_rate_corr is not None
        and overall_passive_fill_rate_corr > 0.10
        and latest_passive_fill_rate_corr <= 0
    ):
        notes.append("passive_fill_rate 已不再提供正向确认")

    if not notes:
        return base
    return f"{base}；" + "；".join(notes)


def analyze_recent_pressure_logs(
    log_dir: Path,
    *,
    current_dt: Optional[datetime] = None,
    lookback_hours: int = 24,
    target_keys: Optional[set[tuple[str, str]]] = None,
    window_label: str = "5m",
    regime_samples: int = 12,
) -> List[PressureRecap]:
    """分析最近日志中的 pressure 统计与 regime 变化，生成启动摘要。"""
    until = current_dt or datetime.now()
    since = until - timedelta(hours=lookback_hours)
    scan_since = since - timedelta(days=_RECAP_WARMUP_MAX_DAYS)
    warmup_target = max(_REGIME_HISTORY_MAX, regime_samples * 4)
    stats_by_key: Dict[tuple[str, str], List[PressureStatsLogEntry]] = {}
    warmup_stats_by_key: Dict[tuple[str, str], Deque[PressureStatsLogEntry]] = {}

    for path in _iter_recent_log_paths(log_dir, scan_since, until):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if "[PRESSURE_STATS]" in line:
                    entry = _parse_pressure_stats_line(line)
                    if entry is None or entry.window_label != window_label or not (scan_since <= entry.ts <= until):
                        continue
                    key = (entry.symbol, entry.side)
                    if target_keys is not None and key not in target_keys:
                        continue
                    if entry.ts < since:
                        warmup_stats_by_key.setdefault(key, deque(maxlen=warmup_target)).append(entry)
                    else:
                        stats_by_key.setdefault(key, []).append(entry)

    summaries: List[PressureRecap] = []
    for key in sorted(stats_by_key):
        stats_entries = stats_by_key.get(key, [])
        if not stats_entries:
            continue

        stats_entries = sorted(stats_entries, key=lambda entry: entry.ts)
        replay_stats_entries = list(warmup_stats_by_key.get(key, ())) + stats_entries
        replayed_regime_entries = PressureStatsCollector.build_regime_history_from_stats_entries(
            symbol=key[0],
            side=key[1],
            window_label=window_label,
            stats_entries=replay_stats_entries,
            regime_samples=regime_samples,
        )
        regime_entries = [entry for entry in replayed_regime_entries if entry.ts >= since]

        overall_active_attempts_corr: Optional[float] = None
        overall_active_triggers_corr: Optional[float] = None
        overall_passive_triggers_corr: Optional[float] = None
        overall_passive_fill_rate_corr: Optional[float] = None
        price_stats_entries = [entry for entry in stats_entries if entry.price_change_pct is not None]
        if len(price_stats_entries) >= 2:
            price_changes = [
                _side_adjust_price_change(key[1], cast(float, entry.price_change_pct))
                for entry in price_stats_entries
            ]
            overall_active_attempts_corr = PressureStatsCollector._corr(
                [float(entry.active_attempts) for entry in price_stats_entries],
                price_changes,
            )
            overall_active_triggers_corr = PressureStatsCollector._corr(
                [float(entry.active_triggers) for entry in price_stats_entries],
                price_changes,
            )
            overall_passive_triggers_corr = PressureStatsCollector._corr(
                [float(entry.passive_triggers) for entry in price_stats_entries],
                price_changes,
            )
            passive_fill_pairs = [
                (
                    entry.passive_fill_rate,
                    _side_adjust_price_change(key[1], cast(float, entry.price_change_pct)),
                )
                for entry in price_stats_entries
                if entry.passive_fill_rate is not None and entry.price_change_pct is not None
            ]
            if len(passive_fill_pairs) >= max(6, len(price_stats_entries) // 2):
                overall_passive_fill_rate_corr = PressureStatsCollector._corr(
                    [cast(float, pair[0]) for pair in passive_fill_pairs],
                    [pair[1] for pair in passive_fill_pairs],
                )

        latest_regime_entry = replayed_regime_entries[-1] if replayed_regime_entries else None
        regime_changes: List[str] = []
        prev_state: Optional[PressureRegime] = None
        for entry in replayed_regime_entries:
            if prev_state == entry.regime:
                continue
            if entry.ts < since:
                prev_state = entry.regime
                continue
            transition = (
                f"{entry.ts:%m-%d %H:%M} "
                f"{entry.prev_regime or '-'}->{entry.regime}"
            )
            regime_changes.append(transition)
            prev_state = entry.regime

        interpretation = _build_recap_interpretation(
            latest_regime_entry.regime if latest_regime_entry else None,
            overall_active_attempts_corr=overall_active_attempts_corr,
            overall_passive_triggers_corr=overall_passive_triggers_corr,
            overall_passive_fill_rate_corr=overall_passive_fill_rate_corr,
            latest_active_attempts_corr=latest_regime_entry.active_attempts_corr if latest_regime_entry else None,
            latest_passive_triggers_corr=latest_regime_entry.passive_triggers_corr if latest_regime_entry else None,
            latest_passive_fill_rate_corr=latest_regime_entry.passive_fill_rate_corr if latest_regime_entry else None,
        )

        summaries.append(
            PressureRecap(
                symbol=key[0],
                side=key[1],
                window_label=window_label,
                range_start=stats_entries[0].ts,
                range_end=stats_entries[-1].ts,
                stats_samples=len(stats_entries),
                regime_samples=len(regime_entries),
                overall_active_attempts_corr=overall_active_attempts_corr,
                overall_active_triggers_corr=overall_active_triggers_corr,
                overall_passive_triggers_corr=overall_passive_triggers_corr,
                overall_passive_fill_rate_corr=overall_passive_fill_rate_corr,
                latest_regime=latest_regime_entry.regime if latest_regime_entry else None,
                latest_regime_ts=latest_regime_entry.ts if latest_regime_entry else None,
                latest_score=latest_regime_entry.score if latest_regime_entry else None,
                latest_regime_samples=latest_regime_entry.samples if latest_regime_entry else None,
                latest_active_attempts_corr=latest_regime_entry.active_attempts_corr if latest_regime_entry else None,
                latest_active_triggers_corr=latest_regime_entry.active_triggers_corr if latest_regime_entry else None,
                latest_passive_triggers_corr=latest_regime_entry.passive_triggers_corr if latest_regime_entry else None,
                latest_passive_fill_rate_corr=latest_regime_entry.passive_fill_rate_corr if latest_regime_entry else None,
                regime_changes=regime_changes,
                interpretation=interpretation,
            )
        )

    return summaries


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
        self._regime_history_max = max(_REGIME_HISTORY_MAX, regime_samples)

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
            buf = deque(maxlen=self._regime_history_max)
            self._regime_snapshots[key] = buf
        return buf

    def _get_regime_tracker(self, key: str) -> RegimeTracker:
        tracker = self._regime_trackers.get(key)
        if tracker is None:
            tracker = RegimeTracker()
            self._regime_trackers[key] = tracker
        return tracker

    def export_regime_state(self, current_ms: int) -> dict[str, Any]:
        """导出可持久化的 regime 快照与状态机状态。"""
        snapshots: dict[str, list[dict[str, Any]]] = {}
        latest_snapshot_ts_ms: Optional[int] = None
        for key, buf in self._regime_snapshots.items():
            if not buf:
                continue
            last_ts = buf[-1].ts_ms
            latest_snapshot_ts_ms = last_ts if latest_snapshot_ts_ms is None else max(latest_snapshot_ts_ms, last_ts)
            snapshots[key] = [
                {
                    "ts_ms": item.ts_ms,
                    "active_triggers": item.active_triggers,
                    "passive_triggers": item.passive_triggers,
                    "active_attempts": item.active_attempts,
                    "passive_fill_rate": str(item.passive_fill_rate)
                    if item.passive_fill_rate is not None
                    else None,
                    "price_change_pct": str(item.price_change_pct),
                }
                for item in buf
            ]

        trackers: dict[str, dict[str, Any]] = {}
        for key, tracker in self._regime_trackers.items():
            if tracker.state is None and tracker.strong_streak == 0 and tracker.weak_streak == 0:
                continue
            trackers[key] = {
                "state": tracker.state,
                "strong_streak": tracker.strong_streak,
                "weak_streak": tracker.weak_streak,
            }

        return {
            "version": 2,
            "saved_at_ms": current_ms,
            "latest_snapshot_ts_ms": latest_snapshot_ts_ms,
            "regime_window_ms": self._regime_window_ms,
            "regime_samples": self._regime_samples,
            "snapshots": snapshots,
            "trackers": trackers,
        }

    def regime_snapshot_keys(self) -> set[str]:
        """返回当前已持有 regime snapshots 的 key 集合。"""
        return {key for key, buf in self._regime_snapshots.items() if buf}

    def restore_regime_state(
        self,
        payload: dict[str, Any],
        *,
        current_ms: int,
        max_gap_ms: int,
    ) -> int:
        """恢复最近一次 regime 快照与状态机状态，返回恢复的快照条数。"""
        if payload.get("version") != 2:
            return 0

        snapshots_payload = payload.get("snapshots")
        trackers_payload = payload.get("trackers")
        if not isinstance(snapshots_payload, dict) or not isinstance(trackers_payload, dict):
            return 0

        if payload.get("regime_window_ms") != self._regime_window_ms:
            return 0
        if payload.get("regime_samples") != self._regime_samples:
            return 0

        restored = 0
        self._regime_snapshots.clear()
        self._regime_trackers.clear()
        restored_keys: set[str] = set()

        for key, items in snapshots_payload.items():
            if not isinstance(key, str) or not isinstance(items, list):
                continue
            latest_snapshot_ts_ms: Optional[int] = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                ts_ms = item.get("ts_ms")
                if not isinstance(ts_ms, int):
                    continue
                latest_snapshot_ts_ms = ts_ms if latest_snapshot_ts_ms is None else max(latest_snapshot_ts_ms, ts_ms)
            if latest_snapshot_ts_ms is None:
                continue
            gap_ms = current_ms - latest_snapshot_ts_ms
            if gap_ms < 0 or gap_ms > max_gap_ms:
                continue

            buf = self._get_regime_snapshots(key)
            for item in items[-self._regime_history_max:]:
                if not isinstance(item, dict):
                    continue
                try:
                    snapshot = RegimeSnapshot(
                        ts_ms=int(item["ts_ms"]),
                        active_triggers=int(item["active_triggers"]),
                        passive_triggers=int(item["passive_triggers"]),
                        active_attempts=int(item["active_attempts"]),
                        passive_fill_rate=Decimal(str(item["passive_fill_rate"]))
                        if item.get("passive_fill_rate") is not None
                        else None,
                        price_change_pct=Decimal(str(item["price_change_pct"])),
                    )
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue
                buf.append(snapshot)
                restored += 1
            if buf:
                restored_keys.add(key)

        valid_states = {"effective", "degrading", "failed", "recovering"}
        for key, item in trackers_payload.items():
            if key not in restored_keys or not isinstance(key, str) or not isinstance(item, dict):
                continue
            state = item.get("state")
            if state not in valid_states:
                continue
            try:
                self._regime_trackers[key] = RegimeTracker(
                    state=state,
                    strong_streak=int(item.get("strong_streak", 0)),
                    weak_streak=int(item.get("weak_streak", 0)),
                )
            except (TypeError, ValueError):
                continue

        return restored

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

    @classmethod
    def _compute_regime_correlations(
        cls,
        side: str,
        snapshots: Sequence[RegimeSnapshot],
        *,
        regime_samples: int,
    ) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        adjusted_returns = [
            _side_adjust_price_change(side, float(snapshot.price_change_pct))
            for snapshot in snapshots
        ]
        active_attempts = [float(snapshot.active_attempts) for snapshot in snapshots]
        active_triggers = [float(snapshot.active_triggers) for snapshot in snapshots]
        passive_triggers = [float(snapshot.passive_triggers) for snapshot in snapshots]

        active_attempts_corr = cls._corr(active_attempts, adjusted_returns)
        active_triggers_corr = cls._corr(active_triggers, adjusted_returns)
        passive_triggers_corr = cls._corr(passive_triggers, adjusted_returns)

        pfr_xs: List[float] = []
        pfr_ys: List[float] = []
        for snapshot in snapshots:
            if snapshot.passive_fill_rate is None:
                continue
            pfr_xs.append(float(snapshot.passive_fill_rate))
            pfr_ys.append(_side_adjust_price_change(side, float(snapshot.price_change_pct)))
        passive_fill_rate_corr = cls._corr(pfr_xs, pfr_ys) if len(pfr_xs) >= max(6, regime_samples // 2) else None

        return (
            active_attempts_corr,
            active_triggers_corr,
            passive_triggers_corr,
            passive_fill_rate_corr,
        )

    @classmethod
    def _evaluate_regime_snapshots(
        cls,
        side: str,
        snapshots: Sequence[RegimeSnapshot],
        *,
        tracker: RegimeTracker,
        regime_samples: int,
    ) -> Optional[RegimeEvaluation]:
        if len(snapshots) < regime_samples:
            return None

        (
            active_attempts_corr,
            active_triggers_corr,
            passive_triggers_corr,
            passive_fill_rate_corr,
        ) = cls._compute_regime_correlations(side, snapshots, regime_samples=regime_samples)

        score = cls._score_regime(
            active_attempts_corr=active_attempts_corr,
            active_triggers_corr=active_triggers_corr,
            passive_triggers_corr=passive_triggers_corr,
            passive_fill_rate_corr=passive_fill_rate_corr,
        )
        state, prev_state = cls._advance_regime_state(tracker, score)
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
        tracker = self._get_regime_tracker(key)
        return self._evaluate_regime_snapshots(
            side,
            snapshots,
            tracker=tracker,
            regime_samples=self._regime_samples,
        )

    @classmethod
    def build_regime_history_from_stats_entries(
        cls,
        *,
        symbol: str,
        side: str,
        window_label: str,
        stats_entries: Sequence[PressureStatsLogEntry],
        regime_samples: int,
    ) -> List[PressureRegimeHistoryEntry]:
        snapshots: Deque[RegimeSnapshot] = deque(maxlen=max(_REGIME_HISTORY_MAX, regime_samples))
        tracker = RegimeTracker()
        history: List[PressureRegimeHistoryEntry] = []

        for entry in sorted(stats_entries, key=lambda item: item.ts):
            if entry.price_change_pct is None:
                continue
            snapshots.append(
                RegimeSnapshot(
                    ts_ms=int(entry.ts.timestamp() * 1000),
                    active_triggers=entry.active_triggers,
                    passive_triggers=entry.passive_triggers,
                    active_attempts=entry.active_attempts,
                    passive_fill_rate=Decimal(str(entry.passive_fill_rate))
                    if entry.passive_fill_rate is not None
                    else None,
                    price_change_pct=Decimal(str(entry.price_change_pct)),
                )
            )
            evaluation = cls._evaluate_regime_snapshots(
                side,
                list(snapshots),
                tracker=tracker,
                regime_samples=regime_samples,
            )
            if evaluation is None:
                continue
            history.append(
                PressureRegimeHistoryEntry(
                    ts=entry.ts,
                    symbol=symbol,
                    side=side,
                    window_label=window_label,
                    regime=evaluation.state,
                    prev_regime=evaluation.prev_state,
                    score=evaluation.score,
                    samples=evaluation.samples,
                    active_attempts_corr=evaluation.active_attempts_corr,
                    active_triggers_corr=evaluation.active_triggers_corr,
                    passive_triggers_corr=evaluation.passive_triggers_corr,
                    passive_fill_rate_corr=evaluation.passive_fill_rate_corr,
                )
            )

        return history

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

    @staticmethod
    def _window_total_events(stats: dict[str, Any]) -> int:
        return (
            int(stats["active_triggers"])
            + int(stats["passive_triggers"])
            + int(stats["active_attempts"])
            + int(stats["passive_attempts"])
            + int(stats["active_fills"])
            + int(stats["passive_fills"])
        )

    def build_periodic_reports(
        self,
        current_ms: int,
        *,
        target_keys: Optional[set[tuple[str, str]]] = None,
        regime_entries: Optional[Sequence[RegimeLogEntry]] = None,
    ) -> List[PressurePeriodicReport]:
        """构造当前周期的多窗口 pressure 报告。"""
        reports: List[PressurePeriodicReport] = []
        regime_by_key: Dict[tuple[str, str], RegimeLogEntry] = {}
        if regime_entries:
            regime_by_key = {
                (entry.symbol, entry.side): entry
                for entry in regime_entries
            }
        seen_keys: set[str] = set()
        seen_keys.update(self._triggers.keys())
        seen_keys.update(self._attempts.keys())
        seen_keys.update(self._outcomes.keys())

        for key in sorted(seen_keys):
            parts = key.split("|", 1)
            if len(parts) != 2:
                continue
            symbol, side = parts
            key_tuple = (symbol, side)
            short_key_tuple = (_short_symbol(symbol), side)
            if target_keys is not None and key_tuple not in target_keys and short_key_tuple not in target_keys:
                continue

            window_reports: List[PressureWindowReport] = []
            has_any_activity = False
            for window_ms in self._windows_ms:
                stats = self.compute_window(symbol, side, window_ms, current_ms)
                total_events = self._window_total_events(stats)
                if total_events > 0:
                    has_any_activity = True
                window_reports.append(
                    PressureWindowReport(
                        window_label=cast(str, stats["window_label"]),
                        active_triggers=cast(int, stats["active_triggers"]),
                        passive_triggers=cast(int, stats["passive_triggers"]),
                        active_attempts=cast(int, stats["active_attempts"]),
                        passive_attempts=cast(int, stats["passive_attempts"]),
                        active_fills=cast(int, stats["active_fills"]),
                        active_fill_rate=cast(Optional[Decimal], stats["active_fill_rate"]),
                        passive_fills=cast(int, stats["passive_fills"]),
                        passive_fill_rate=cast(Optional[Decimal], stats["passive_fill_rate"]),
                        price_change_pct=cast(Optional[Decimal], stats["price_change_pct"]),
                    )
                )

            if not has_any_activity:
                continue

            regime_entry = regime_by_key.get((symbol, side))
            if regime_entry is None:
                regime = self._evaluate_regime(symbol, side)
                if regime is not None:
                    regime_entry = RegimeLogEntry(
                        symbol=symbol,
                        side=side,
                        window_label=_window_label(self._regime_window_ms),
                        regime=regime.state,
                        prev_regime=regime.prev_state,
                        score=regime.score,
                        samples=regime.samples,
                        active_attempts_corr=regime.active_attempts_corr,
                        active_triggers_corr=regime.active_triggers_corr,
                        passive_triggers_corr=regime.passive_triggers_corr,
                        passive_fill_rate_corr=regime.passive_fill_rate_corr,
                    )

            reports.append(
                PressurePeriodicReport(
                    symbol=_short_symbol(symbol),
                    side=side,
                    as_of_ms=current_ms,
                    window_reports=window_reports,
                    regime_entry=regime_entry,
                )
            )

        return reports

    # ------------------------------------------------------------------
    # 日志输出
    # ------------------------------------------------------------------

    def log_all_windows(self, current_ms: int) -> List[RegimeLogEntry]:
        """遍历所有 symbol:side，对每个窗口输出结构化日志，并返回 regime 结果。"""
        regime_entries: List[RegimeLogEntry] = []
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
                total_events = self._window_total_events(stats)
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
            regime_total_events = self._window_total_events(regime_stats)
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

            window_label = _window_label(self._regime_window_ms)
            log_event(
                "pressure_regime",
                symbol=symbol,
                side=side,
                window=window_label,
                corr_basis="side_adjusted_same_window",
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
            regime_entries.append(
                RegimeLogEntry(
                    symbol=symbol,
                    side=side,
                    window_label=window_label,
                    regime=regime.state,
                    prev_regime=regime.prev_state,
                    score=regime.score,
                    samples=regime.samples,
                    active_attempts_corr=regime.active_attempts_corr,
                    active_triggers_corr=regime.active_triggers_corr,
                    passive_triggers_corr=regime.passive_triggers_corr,
                    passive_fill_rate_corr=regime.passive_fill_rate_corr,
                )
            )

        return regime_entries
