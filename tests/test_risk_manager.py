# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
RiskManager 测试
"""

from decimal import Decimal

from src.models import Position, PositionSide
from src.risk.manager import RiskManager


def _pos(
    *,
    symbol: str = "BTC/USDT:USDT",
    side: PositionSide = PositionSide.LONG,
    position_amt: Decimal = Decimal("0.01"),
    mark_price: Decimal | None = Decimal("100"),
    liquidation_price: Decimal | None = Decimal("99"),
) -> Position:
    return Position(
        symbol=symbol,
        position_side=side,
        position_amt=position_amt,
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        leverage=10,
        mark_price=mark_price,
        liquidation_price=liquidation_price,
    )


class TestRiskDistance:
    def test_missing_mark_price(self) -> None:
        rm = RiskManager()
        flag = rm.check_risk(_pos(mark_price=None))
        assert flag.is_triggered is False
        assert flag.dist_to_liq is None
        assert flag.reason == "missing_mark_price"

    def test_missing_liquidation_price(self) -> None:
        rm = RiskManager()
        flag = rm.check_risk(_pos(liquidation_price=None))
        assert flag.is_triggered is False
        assert flag.dist_to_liq is None
        assert flag.reason == "missing_liquidation_price"

    def test_computes_dist(self) -> None:
        rm = RiskManager(liq_distance_threshold=Decimal("0.015"))
        flag = rm.check_risk(_pos(mark_price=Decimal("100"), liquidation_price=Decimal("98")))
        assert flag.dist_to_liq == Decimal("0.02")
        assert flag.is_triggered is False

    def test_triggers_when_below_threshold(self) -> None:
        rm = RiskManager(liq_distance_threshold=Decimal("0.015"))
        flag = rm.check_risk(_pos(mark_price=Decimal("100"), liquidation_price=Decimal("99")))
        assert flag.dist_to_liq == Decimal("0.01")
        assert flag.is_triggered is True
        assert flag.reason == "liq_distance"


class TestGlobalRateLimit:
    def test_order_rate_limit(self) -> None:
        rm = RiskManager(max_orders_per_sec=2, max_cancels_per_sec=8)

        assert rm.can_place_order(current_ms=0) is True
        assert rm.can_place_order(current_ms=100) is True
        assert rm.can_place_order(current_ms=200) is False

        # 1000ms 窗口滑过后允许继续
        assert rm.can_place_order(current_ms=1001) is True

    def test_cancel_rate_limit(self) -> None:
        rm = RiskManager(max_orders_per_sec=5, max_cancels_per_sec=1)

        assert rm.can_cancel_order(current_ms=0) is True
        assert rm.can_cancel_order(current_ms=1) is False
        assert rm.can_cancel_order(current_ms=1001) is True

