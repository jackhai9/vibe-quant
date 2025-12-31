# Input: User Data WS 被测模块与 pytest 夹具
# Output: 订单/仓位/杠杆/手续费解析断言
# Pos: User Data WS 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
User Data Stream WebSocket 模块测试
"""

import asyncio
import aiohttp
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import List

from src.ws.user_data import (
    UserDataWSClient,
    REST_BASE_URL,
    REST_TESTNET_URL,
    WS_BASE_URL,
    WS_TESTNET_URL,
    KEEPALIVE_INTERVAL_MS,
)
from src.models import AlgoOrderUpdate, OrderUpdate, OrderSide, PositionSide, OrderStatus, PositionUpdate, LeverageUpdate
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class TestUserDataWSClientInit:
    """初始化测试"""

    def test_init_default(self):
        """测试默认初始化"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="test_key",
            api_secret="test_secret",
            on_order_update=updates.append,
        )

        assert client.api_key == "test_key"
        assert client.api_secret == "test_secret"
        assert client.testnet is False
        assert client.initial_delay_ms == 1000
        assert client.max_delay_ms == 30000
        assert client.multiplier == 2
        assert client._running is False
        assert client._ws is None
        assert client._listen_key is None

    def test_init_testnet(self):
        """测试测试网初始化"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="test_key",
            api_secret="test_secret",
            on_order_update=updates.append,
            testnet=True,
        )

        assert client.testnet is True

    def test_init_custom_params(self):
        """测试自定义参数"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="test_key",
            api_secret="test_secret",
            on_order_update=updates.append,
            initial_delay_ms=500,
            max_delay_ms=60000,
            multiplier=3,
        )

        assert client.initial_delay_ms == 500
        assert client.max_delay_ms == 60000
        assert client.multiplier == 3


class TestURLs:
    """URL 测试"""

    def test_rest_url_mainnet(self):
        """测试主网 REST URL"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            testnet=False,
        )

        assert client._get_rest_url() == REST_BASE_URL

    def test_rest_url_testnet(self):
        """测试测试网 REST URL"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            testnet=True,
        )

        assert client._get_rest_url() == REST_TESTNET_URL

    def test_ws_url_mainnet(self):
        """测试主网 WS URL"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            testnet=False,
        )

        assert client._get_ws_url() == WS_BASE_URL


class TestReconnectCallback:
    """重连成功回调测试"""

    @pytest.mark.asyncio
    async def test_on_reconnect_called_after_reconnect(self):
        updates: List[OrderUpdate] = []
        called: List[str] = []

        client = UserDataWSClient(
            api_key="test_key",
            api_secret="test_secret",
            on_order_update=updates.append,
            on_reconnect=called.append,
        )

        client._reconnect_count = 1

        dummy_ws = MagicMock()
        dummy_ws.close = AsyncMock()
        dummy_ws.closed = False
        dummy_session = MagicMock()
        dummy_session.closed = False
        dummy_session.ws_connect = AsyncMock(return_value=dummy_ws)
        dummy_session.close = AsyncMock()

        with patch.object(client, "_get_listen_key", new=AsyncMock(return_value="listen_key")):
            with patch.object(client, "_close_listen_key", new=AsyncMock()):
                with patch.object(client, "_keepalive_loop", new=AsyncMock()):
                    with patch.object(client, "_receive_loop", new=AsyncMock()):
                        with patch("src.ws.user_data.aiohttp.ClientSession", return_value=dummy_session):
                            await client.connect()
                            await asyncio.wait_for(client.disconnect(), timeout=3.0)

        assert called == ["user_data"]

    def test_ws_url_testnet(self):
        """测试测试网 WS URL"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            testnet=True,
        )

        assert client._get_ws_url() == WS_TESTNET_URL


class TestParseOrderUpdate:
    """ORDER_TRADE_UPDATE 解析测试"""

    def test_parse_order_update_new(self):
        """测试解析 NEW 订单"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "T": 1591097736591,
            "o": {
                "s": "BTCUSDT",
                "c": "client_123",
                "S": "SELL",
                "o": "LIMIT",
                "f": "GTX",
                "q": "0.001",
                "p": "50000",
                "ap": "0",
                "X": "NEW",
                "i": 12345678,
                "z": "0",
                "ps": "LONG",
            }
        }

        result = client._parse_order_update(data)

        assert result is not None
        assert result.symbol == "BTC/USDT:USDT"
        assert result.order_id == "12345678"
        assert result.client_order_id == "client_123"
        assert result.side == OrderSide.SELL
        assert result.position_side == PositionSide.LONG
        assert result.status == OrderStatus.NEW
        assert result.filled_qty == Decimal("0")
        assert result.avg_price == Decimal("0")
        assert result.order_type == "LIMIT"
        assert result.close_position is None
        assert result.reduce_only is None
        assert result.is_maker is None
        assert result.realized_pnl is None

    def test_parse_order_update_filled(self):
        """测试解析 FILLED 订单"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "T": 1591097736591,
            "o": {
                "s": "ETHUSDT",
                "c": "client_456",
                "S": "BUY",
                "X": "FILLED",
                "i": 87654321,
                "z": "0.5",
                "ap": "3000.50",
                "m": True,
                "rp": "-0.1234",
                "n": "0.0001",
                "N": "USDT",
                "ps": "SHORT",
            }
        }

        result = client._parse_order_update(data)

        assert result is not None
        assert result.symbol == "ETH/USDT:USDT"
        assert result.side == OrderSide.BUY
        assert result.position_side == PositionSide.SHORT
        assert result.status == OrderStatus.FILLED
        assert result.filled_qty == Decimal("0.5")
        assert result.avg_price == Decimal("3000.50")
        assert result.order_type is None
        assert result.close_position is None
        assert result.reduce_only is None
        assert result.is_maker is True
        assert result.realized_pnl == Decimal("-0.1234")
        assert result.fee == Decimal("0.0001")
        assert result.fee_asset == "USDT"

    def test_parse_order_update_partially_filled(self):
        """测试解析部分成交订单"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "o": {
                "s": "BTCUSDT",
                "c": "client_789",
                "S": "SELL",
                "X": "PARTIALLY_FILLED",
                "i": 11111111,
                "z": "0.005",
                "ap": "50100.00",
                "ps": "LONG",
            }
        }

        result = client._parse_order_update(data)

        assert result is not None
        assert result.status == OrderStatus.PARTIALLY_FILLED
        assert result.filled_qty == Decimal("0.005")
        assert result.avg_price == Decimal("50100.00")
        assert result.order_type is None
        assert result.close_position is None
        assert result.reduce_only is None
        assert result.is_maker is None
        assert result.realized_pnl is None

    def test_parse_order_update_canceled(self):
        """测试解析取消订单"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "o": {
                "s": "BTCUSDT",
                "c": "client_000",
                "S": "BUY",
                "X": "CANCELED",
                "i": 22222222,
                "z": "0",
                "ap": "0",
                "ps": "SHORT",
            }
        }

        result = client._parse_order_update(data)

        assert result is not None
        assert result.status == OrderStatus.CANCELED

    def test_parse_order_update_close_position(self):
        """测试解析 closePosition 字段（cp）"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "o": {
                "s": "BTCUSDT",
                "c": "client_cp",
                "S": "SELL",
                "o": "STOP_MARKET",
                "X": "NEW",
                "i": 33333333,
                "z": "0",
                "ap": "0",
                "ps": "LONG",
                "cp": True,
                "R": True,
            }
        }

        result = client._parse_order_update(data)

        assert result is not None
        assert result.order_type == "STOP_MARKET"
        assert result.close_position is True
        assert result.reduce_only is True


class TestParseAlgoOrderUpdate:
    """ALGO_UPDATE 解析测试"""

    def test_parse_algo_update_basic(self):
        updates: List[AlgoOrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=lambda _: None,
            on_algo_order_update=updates.append,
        )

        data = {
            "e": "ALGO_UPDATE",
            "T": 1750515742297,
            "E": 1750515742303,
            "o": {
                "caid": "external_caid",
                "aid": 2148719,
                "o": "STOP",
                "s": "BTCUSDT",
                "S": "SELL",
                "ps": "LONG",
                "X": "CANCELED",
                "cp": True,
                "R": True,
            }
        }

        result = client._parse_algo_order_update(data)
        assert result is not None
        assert result.symbol == "BTC/USDT:USDT"
        assert result.algo_id == "2148719"
        assert result.client_algo_id == "external_caid"
        assert result.side == OrderSide.SELL
        assert result.position_side == PositionSide.LONG
        assert result.status == "CANCELED"
        assert result.close_position is True
        assert result.reduce_only is True

    @pytest.mark.asyncio
    async def test_handle_algo_update_calls_callback(self):
        updates: List[AlgoOrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=lambda _: None,
            on_algo_order_update=updates.append,
        )

        message = {
            "e": "ALGO_UPDATE",
            "E": 1750515742303,
            "o": {
                "caid": "external_caid",
                "aid": 2148719,
                "o": "STOP",
                "s": "BTCUSDT",
                "S": "SELL",
                "ps": "LONG",
                "X": "NEW",
                "cp": True,
            },
        }

        await client._handle_message(message)
        assert len(updates) == 1
        assert updates[0].symbol == "BTC/USDT:USDT"

    def test_parse_order_update_empty_data(self):
        """测试解析空订单数据"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {}
        }

        result = client._parse_order_update(data)
        assert result is None

    def test_parse_order_update_no_order_field(self):
        """测试解析缺少订单字段"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        data = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
        }

        result = client._parse_order_update(data)
        assert result is None


class TestParseAccountUpdate:
    """ACCOUNT_UPDATE 解析测试"""

    def test_parse_account_update_positions(self):
        position_updates: List[PositionUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=lambda _: None,
            on_position_update=position_updates.append,
        )

        data = {
            "e": "ACCOUNT_UPDATE",
            "E": 1591097736594,
            "a": {
                "P": [
                    {"s": "BTCUSDT", "pa": "0.1", "ep": "50000", "up": "1.23", "ps": "LONG"},
                    {"s": "ETHUSDT", "pa": "0.2", "ep": "3000", "up": "-0.50", "ps": "SHORT"},
                    {"s": "BNBUSDT", "pa": "0", "ep": "0", "up": "0", "ps": "LONG"},
                ]
            },
        }

        parsed = client._parse_account_update(data)

        assert len(parsed) == 3

        assert parsed[0].symbol == "BTC/USDT:USDT"
        assert parsed[0].position_side == PositionSide.LONG
        assert parsed[0].position_amt == Decimal("0.1")
        assert parsed[0].entry_price == Decimal("50000")
        assert parsed[0].unrealized_pnl == Decimal("1.23")

        assert parsed[1].symbol == "ETH/USDT:USDT"
        assert parsed[1].position_side == PositionSide.SHORT
        assert parsed[1].position_amt == Decimal("-0.2")
        assert parsed[1].entry_price == Decimal("3000")
        assert parsed[1].unrealized_pnl == Decimal("-0.50")

        assert parsed[2].symbol == "BNB/USDT:USDT"
        assert parsed[2].position_side == PositionSide.LONG
        assert parsed[2].position_amt == Decimal("0")
        assert parsed[2].entry_price is None
        assert parsed[2].unrealized_pnl == Decimal("0")


class TestParseAccountConfigUpdate:
    """ACCOUNT_CONFIG_UPDATE 解析测试"""

    def test_parse_account_config_update_leverage(self):
        leverage_updates: List[LeverageUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=lambda _: None,
            on_leverage_update=leverage_updates.append,
        )

        data = {
            "e": "ACCOUNT_CONFIG_UPDATE",
            "E": 1611646737478,
            "ac": {"s": "BTCUSDT", "l": "25"},
        }

        parsed = client._parse_account_config_update(data)
        assert parsed is not None
        assert parsed.symbol == "BTC/USDT:USDT"
        assert parsed.leverage == 25


class TestParseOrderStatus:
    """订单状态解析测试"""

    def test_parse_all_statuses(self):
        """测试所有状态解析"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client._parse_order_status("NEW") == OrderStatus.NEW
        assert client._parse_order_status("PARTIALLY_FILLED") == OrderStatus.PARTIALLY_FILLED
        assert client._parse_order_status("FILLED") == OrderStatus.FILLED
        assert client._parse_order_status("CANCELED") == OrderStatus.CANCELED
        assert client._parse_order_status("REJECTED") == OrderStatus.REJECTED
        assert client._parse_order_status("EXPIRED") == OrderStatus.EXPIRED

    def test_parse_unknown_status(self):
        """测试未知状态默认值"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client._parse_order_status("UNKNOWN") == OrderStatus.NEW


class TestSymbolConversion:
    """Symbol 格式转换测试"""

    def test_ws_to_symbol(self):
        """测试 WS 格式转 ccxt 格式"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client._ws_to_symbol("BTCUSDT") == "BTC/USDT:USDT"
        assert client._ws_to_symbol("ETHUSDT") == "ETH/USDT:USDT"


class TestHandleMessage:
    """消息处理测试"""

    @pytest.mark.asyncio
    async def test_handle_order_trade_update(self):
        """测试处理订单更新消息"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        message = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,
            "o": {
                "s": "BTCUSDT",
                "c": "client_123",
                "S": "SELL",
                "X": "FILLED",
                "i": 12345678,
                "z": "0.001",
                "ap": "50000",
                "ps": "LONG",
            }
        }

        await client._handle_message(message)

        assert len(updates) == 1
        assert updates[0].order_id == "12345678"
        assert updates[0].status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_handle_account_update_calls_position_callback(self):
        """测试账户更新消息触发仓位更新回调"""
        order_updates: List[OrderUpdate] = []
        position_updates: List[PositionUpdate] = []

        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=order_updates.append,
            on_position_update=position_updates.append,
        )

        message = {
            "e": "ACCOUNT_UPDATE",
            "E": 1591097736594,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.1", "ep": "50000", "up": "0", "ps": "LONG"}]},
        }

        await client._handle_message(message)

        assert len(order_updates) == 0
        assert len(position_updates) == 1
        assert position_updates[0].symbol == "BTC/USDT:USDT"
        assert position_updates[0].position_side == PositionSide.LONG
        assert position_updates[0].position_amt == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_handle_account_config_update_calls_leverage_callback(self):
        """测试配置更新消息触发杠杆更新回调"""
        order_updates: List[OrderUpdate] = []
        leverage_updates: List[LeverageUpdate] = []

        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=order_updates.append,
            on_leverage_update=leverage_updates.append,
        )

        message = {
            "e": "ACCOUNT_CONFIG_UPDATE",
            "E": 1611646737478,
            "ac": {"s": "BTCUSDT", "l": "25"},
        }

        await client._handle_message(message)

        assert len(order_updates) == 0
        assert len(leverage_updates) == 1
        assert leverage_updates[0].symbol == "BTC/USDT:USDT"
        assert leverage_updates[0].leverage == 25

    @pytest.mark.asyncio
    async def test_handle_unknown_event_ignored(self):
        """测试未知事件被忽略"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        message = {
            "e": "UNKNOWN_EVENT",
            "data": {}
        }

        await client._handle_message(message)

        assert len(updates) == 0


class TestReconnection:
    """重连机制测试"""

    def test_reconnect_count_initial(self):
        """测试初始重连次数"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client.reconnect_count == 0

    def test_exponential_backoff_calculation(self):
        """测试指数退避计算"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            initial_delay_ms=1000,
            max_delay_ms=30000,
            multiplier=2,
        )

        # 初始延迟
        assert client._current_delay_ms == 1000

        # 模拟计算下一次延迟
        client._current_delay_ms = min(
            client._current_delay_ms * client.multiplier,
            client.max_delay_ms
        )
        assert client._current_delay_ms == 2000

        # 继续倍增
        client._current_delay_ms = min(
            client._current_delay_ms * client.multiplier,
            client.max_delay_ms
        )
        assert client._current_delay_ms == 4000

    def test_exponential_backoff_max_cap(self):
        """测试指数退避上限"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
            initial_delay_ms=10000,
            max_delay_ms=30000,
            multiplier=2,
        )

        # 第一次：10000 * 2 = 20000
        client._current_delay_ms = min(
            client._current_delay_ms * client.multiplier,
            client.max_delay_ms
        )
        assert client._current_delay_ms == 20000

        # 第二次：20000 * 2 = 40000 -> cap to 30000
        client._current_delay_ms = min(
            client._current_delay_ms * client.multiplier,
            client.max_delay_ms
        )
        assert client._current_delay_ms == 30000


class TestConnectionState:
    """连接状态测试"""

    def test_is_connected_no_ws(self):
        """测试无 WS 连接时"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client.is_connected is False

    def test_listen_key_property(self):
        """测试 listenKey 属性"""
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        assert client.listen_key is None

        client._listen_key = "test_listen_key_123"
        assert client.listen_key == "test_listen_key_123"


class TestDisconnectCleanup:
    """断开连接资源释放测试"""

    @pytest.mark.asyncio
    async def test_disconnect_closes_session_even_if_close_listen_key_hangs(self):
        updates: List[OrderUpdate] = []
        client = UserDataWSClient(
            api_key="key",
            api_secret="secret",
            on_order_update=updates.append,
        )

        client._running = True
        client._listen_key = "test_listen_key"
        client._session = aiohttp.ClientSession()
        session_ref = client._session

        client._ws = MagicMock()
        async def slow_ws_close() -> None:
            await asyncio.sleep(10)

        client._ws.close = AsyncMock(side_effect=slow_ws_close)

        async def slow_close_listen_key() -> None:
            await asyncio.sleep(10)

        client._close_listen_key = AsyncMock(side_effect=slow_close_listen_key)

        await asyncio.wait_for(client.disconnect(), timeout=3.0)
        assert session_ref.closed is True
        assert client._session is None


class TestConstants:
    """常量测试"""

    def test_keepalive_interval(self):
        """测试续期间隔"""
        # 30 分钟 = 30 * 60 * 1000 ms
        assert KEEPALIVE_INTERVAL_MS == 30 * 60 * 1000

    def test_rest_urls(self):
        """测试 REST URL 常量"""
        assert REST_BASE_URL == "https://fapi.binance.com"
        assert REST_TESTNET_URL == "https://testnet.binancefuture.com"

    def test_ws_urls(self):
        """测试 WS URL 常量"""
        assert WS_BASE_URL == "wss://fstream.binance.com"
        assert WS_TESTNET_URL == "wss://stream.binancefuture.com"
