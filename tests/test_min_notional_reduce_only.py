# Input: Application._place_order, reduce-only OrderIntent
# Output: reduce-only min-notional allow-through assertions
# Pos: tests for min-notional handling on reduce-only orders
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

import pytest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from src.main import Application
from src.models import (
    OrderResult,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SymbolRules,
    TimeInForce,
)
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


@pytest.mark.asyncio
async def test_reduce_only_min_notional_allows_order():
    """reduce-only 低于 minNotional 时仍允许下单"""
    app = Application(Path("config.yaml"))
    app.exchange = MagicMock()
    app.exchange.place_order = AsyncMock(
        return_value=OrderResult(success=True, status=OrderStatus.NEW, order_id="1")
    )
    app.exchange.ensure_min_notional = MagicMock()
    app.risk_manager = None

    symbol = "BTC/USDT:USDT"
    app._rules[symbol] = SymbolRules(
        symbol=symbol,
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )

    intent = OrderIntent(
        symbol=symbol,
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        qty=Decimal("1"),
        price=Decimal("1"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTX,
        reduce_only=True,
        client_order_id="test",
    )

    result = await app._place_order(intent)

    assert result.success is True
    assert result.status == OrderStatus.NEW
    assert intent.qty == Decimal("1")
    app.exchange.place_order.assert_awaited_once()
    app.exchange.ensure_min_notional.assert_not_called()
