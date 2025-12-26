# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
订单清理行为测试：只撤销本次运行挂单（按 {CLIENT_ORDER_PREFIX}-{run_id}- 前缀）。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, call
from typing import Any, cast

import pytest

from src.main import Application
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class DummyExchange:
    def __init__(self, orders):
        self.fetch_open_orders = AsyncMock(return_value=orders)
        self.cancel_order = AsyncMock(return_value=None)
        self.cancel_any_order = AsyncMock(return_value=None)


class DummyConfigLoader:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def get_symbols(self):
        return list(self._symbols)


@pytest.mark.asyncio
async def test_cancel_own_orders_should_only_cancel_instance_prefix():
    app = Application(Path("config/config.yaml"))
    app._init_run_identity()
    prefix = app._client_order_id_prefix

    exchange = DummyExchange(
        [
            {"id": "1", "symbol": "BTC/USDT:USDT", "clientOrderId": f"{prefix}abc"},
            {"id": "2", "symbol": "BTC/USDT:USDT", "clientOrderId": "manual-xyz"},
            {"id": "3", "symbol": "ETH/USDT:USDT"},
            {"id": "4", "symbol": "ETH/USDT:USDT", "clientOrderId": f"{prefix}def"},
        ]
    )
    app.exchange = cast(Any, exchange)

    await app._cancel_own_orders(reason="test")

    exchange.fetch_open_orders.assert_called_once()
    assert exchange.cancel_any_order.call_count == 2
    exchange.cancel_any_order.assert_any_call("BTC/USDT:USDT", "1")
    exchange.cancel_any_order.assert_any_call("ETH/USDT:USDT", "4")


@pytest.mark.asyncio
async def test_cancel_own_orders_should_fetch_per_symbol_when_symbols_known():
    app = Application(Path("config/config.yaml"))
    app._init_run_identity()
    prefix = app._client_order_id_prefix

    symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    app.config_loader = cast(Any, DummyConfigLoader(symbols))

    orders_by_symbol = {
        "BTC/USDT:USDT": [
            {"id": "1", "symbol": "BTC/USDT:USDT", "clientOrderId": f"{prefix}abc"},
            {"id": "2", "symbol": "BTC/USDT:USDT", "clientOrderId": "manual-xyz"},
        ],
        "ETH/USDT:USDT": [
            {"id": "3", "symbol": "ETH/USDT:USDT"},
            {"id": "4", "symbol": "ETH/USDT:USDT", "clientOrderId": f"{prefix}def"},
        ],
    }

    async def fetch_open_orders(symbol: str | None = None):
        if symbol is None:
            return []
        return list(orders_by_symbol.get(symbol, []))

    exchange = DummyExchange([])
    exchange.fetch_open_orders = AsyncMock(side_effect=fetch_open_orders)
    app.exchange = cast(Any, exchange)

    await app._cancel_own_orders(reason="test")

    assert exchange.fetch_open_orders.call_args_list == [call("BTC/USDT:USDT"), call("ETH/USDT:USDT")]
    assert exchange.cancel_any_order.call_count == 2
    exchange.cancel_any_order.assert_any_call("BTC/USDT:USDT", "1")
    exchange.cancel_any_order.assert_any_call("ETH/USDT:USDT", "4")
