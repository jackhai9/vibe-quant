# Input: Position, MarketEvent, config
# Output: RiskFlag and risk triggers (per-symbol threshold)
# Pos: risk manager
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
风险管理模块

职责：
- 计算强平距离 dist_to_liq
- 风险兜底触发
- 数据陈旧检测（备用：当前主链路使用 MarketWSClient.is_stale）
- 全局限速

输入：
- Position
- MarketEvent
- 配置

输出：
- RiskFlag
"""

from decimal import Decimal
from typing import Dict, Optional

from src.models import Position, PositionSide, RiskFlag
from src.risk.rate_limiter import SlidingWindowRateLimiter


class RiskManager:
    """风险管理器"""

    def __init__(
        self,
        liq_distance_threshold: Decimal = Decimal("0.015"),
        stale_data_ms: int = 1500,
        max_orders_per_sec: int = 5,
        max_cancels_per_sec: int = 8,
    ):
        """
        初始化风险管理器

        Args:
            liq_distance_threshold: 强平距离阈值
            stale_data_ms: 数据陈旧阈值
            max_orders_per_sec: 每秒最大下单数
            max_cancels_per_sec: 每秒最大撤单数
        """
        self.liq_distance_threshold = liq_distance_threshold
        self.stale_data_ms = stale_data_ms
        self.max_orders_per_sec = max_orders_per_sec
        self.max_cancels_per_sec = max_cancels_per_sec

        self._last_update_ms: Dict[str, int] = {}  # symbol -> last update time
        self._order_limiter = SlidingWindowRateLimiter(max_events=max_orders_per_sec, window_ms=1000)
        self._cancel_limiter = SlidingWindowRateLimiter(max_events=max_cancels_per_sec, window_ms=1000)

    def update_market_time(self, symbol: str, timestamp_ms: int) -> None:
        """
        更新市场数据时间戳

        Args:
            symbol: 交易对
            timestamp_ms: 时间戳
        """
        self._last_update_ms[symbol] = timestamp_ms

    def is_data_stale(self, symbol: str, current_ms: int) -> bool:
        """
        检查数据是否陈旧

        Args:
            symbol: 交易对
            current_ms: 当前时间戳

        Returns:
            True 如果数据陈旧
        """
        last_update = self._last_update_ms.get(symbol, 0)
        return (current_ms - last_update) > self.stale_data_ms

    def check_risk(
        self,
        position: Position,
        liq_distance_threshold: Optional[Decimal] = None,
    ) -> RiskFlag:
        """
        检查仓位风险

        Args:
            position: 仓位信息
            liq_distance_threshold: 强平距离阈值（可选，覆盖默认值）

        Returns:
            RiskFlag
        """
        threshold = liq_distance_threshold if liq_distance_threshold is not None else self.liq_distance_threshold
        mark_price = position.mark_price
        liquidation_price = position.liquidation_price

        if mark_price is None or mark_price <= Decimal("0"):
            return RiskFlag(
                symbol=position.symbol,
                position_side=position.position_side,
                is_triggered=False,
                dist_to_liq=None,
                reason="missing_mark_price",
            )

        if liquidation_price is None or liquidation_price <= Decimal("0"):
            return RiskFlag(
                symbol=position.symbol,
                position_side=position.position_side,
                is_triggered=False,
                dist_to_liq=None,
                reason="missing_liquidation_price",
            )

        dist_to_liq = abs(mark_price - liquidation_price) / mark_price
        is_triggered = dist_to_liq <= threshold
        reason = "liq_distance_breach" if is_triggered else None

        return RiskFlag(
            symbol=position.symbol,
            position_side=position.position_side,
            is_triggered=is_triggered,
            dist_to_liq=dist_to_liq,
            reason=reason,
        )

    def can_place_order(self, current_ms: Optional[int] = None) -> bool:
        """
        检查是否可以下单（限速，调用即占用配额）

        Returns:
            True 如果可以下单
        """
        return self._order_limiter.try_acquire(current_ms=current_ms)

    def can_cancel_order(self, current_ms: Optional[int] = None) -> bool:
        """
        检查是否可以撤单（限速，调用即占用配额）

        Returns:
            True 如果可以撤单
        """
        return self._cancel_limiter.try_acquire(current_ms=current_ms)
