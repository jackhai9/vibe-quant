# Input: Application/ExecutionEngine 与 pytest 夹具
# Output: post-only 拒单重试行为断言
# Pos: 应用层重试逻辑测试
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""Post-only 拒单重试逻辑测试"""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import AsyncMock

import pytest

from src.execution.engine import ExecutionEngine
from src.main import Application
from src.models import (
    ExecutionMode,
    ExecutionState,
    MarketState,
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
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


class DummyExchange:
    def __init__(self, result: OrderResult):
        self.place_order = AsyncMock(return_value=result)


@pytest.mark.asyncio
async def test_post_only_reject_retries_with_aggressive_limit():
    app = Application(Path("config/config.yaml"))
    app._init_run_identity()

    exchange_result = OrderResult(success=True, order_id="abc", status=OrderStatus.NEW)
    app.exchange = DummyExchange(exchange_result)  # type: ignore[assignment]

    engine = ExecutionEngine(
        place_order=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    state = engine.get_state("BTC/USDT:USDT", PositionSide.LONG)
    state.mode = ExecutionMode.MAKER_ONLY
    state.state = ExecutionState.PLACING

    rules = SymbolRules(
        symbol="BTC/USDT:USDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    market_state = MarketState(
        symbol="BTC/USDT:USDT",
        best_bid=Decimal("99.0"),
        best_ask=Decimal("100.0"),
        last_trade_price=Decimal("99.5"),
        previous_trade_price=Decimal("99.4"),
        last_update_ms=1000,
        is_ready=True,
    )

    intent = OrderIntent(
        symbol="BTC/USDT:USDT",
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        qty=Decimal("0.01"),
        price=Decimal("100.0"),
        time_in_force=TimeInForce.GTX,
        reduce_only=True,
    )
    result = OrderResult(success=False, error_code="-5022", error_message="post only rejected")

    retry_intent, retry_result, retried = await app._maybe_retry_post_only_reject(
        engine=engine,
        intent=intent,
        result=result,
        rules=rules,
        market_state=market_state,
    )

    assert retried is True
    assert retry_result.success is True
    assert retry_intent.time_in_force == TimeInForce.GTC
    assert retry_intent.price == Decimal("99.0")
    assert engine.get_state("BTC/USDT:USDT", PositionSide.LONG).mode == ExecutionMode.AGGRESSIVE_LIMIT
    exchange = cast(DummyExchange, app.exchange)
    exchange.place_order.assert_called_once()
