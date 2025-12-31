# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
市场数据 WebSocket 模块测试
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import List

from src.ws.market import MarketWSClient, WS_BASE_URL
from src.models import MarketEvent
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class TestMarketWSClientInit:
    """初始化测试"""

    def test_init_default(self):
        """测试默认初始化"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        assert client.symbols == ["BTC/USDT:USDT"]
        assert client.initial_delay_ms == 1000
        assert client.max_delay_ms == 30000
        assert client.multiplier == 2
        assert client.stale_data_ms == 1500
        assert client._running is False
        assert client._ws is None

    def test_init_custom_params(self):
        """测试自定义参数"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
            on_event=events.append,
            initial_delay_ms=500,
            max_delay_ms=60000,
            multiplier=3,
            stale_data_ms=2000,
        )

        assert len(client.symbols) == 2
        assert client.initial_delay_ms == 500
        assert client.max_delay_ms == 60000
        assert client.multiplier == 3
        assert client.stale_data_ms == 2000


class TestSymbolConversion:
    """Symbol 格式转换测试"""

    def test_symbol_to_ws(self):
        """测试 ccxt 格式转 WS 格式"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        assert client._symbol_to_ws("BTC/USDT:USDT") == "btcusdt"
        assert client._symbol_to_ws("ETH/USDT:USDT") == "ethusdt"
        assert client._symbol_to_ws("SOL/USDT:USDT") == "solusdt"

    def test_ws_to_symbol(self):
        """测试 WS 格式转 ccxt 格式"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        assert client._ws_to_symbol("BTCUSDT") == "BTC/USDT:USDT"
        assert client._ws_to_symbol("ETHUSDT") == "ETH/USDT:USDT"


class TestStreamURL:
    """Stream URL 构建测试"""

    def test_build_stream_url_single_symbol(self):
        """测试单 symbol URL 构建"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        url = client._build_stream_url()

        assert url.startswith(WS_BASE_URL)
        assert "btcusdt@bookTicker" in url
        assert "btcusdt@aggTrade" in url
        assert "btcusdt@markPrice@1s" in url

    def test_build_stream_url_multiple_symbols(self):
        """测试多 symbol URL 构建"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
            on_event=events.append,
        )

        url = client._build_stream_url()

        assert url.startswith(WS_BASE_URL)
        assert "btcusdt@bookTicker" in url
        assert "btcusdt@aggTrade" in url
        assert "btcusdt@markPrice@1s" in url
        assert "ethusdt@bookTicker" in url
        assert "ethusdt@aggTrade" in url
        assert "ethusdt@markPrice@1s" in url


class TestParseBookTicker:
    """bookTicker 解析测试"""

    def test_parse_book_ticker_valid(self):
        """测试有效 bookTicker 解析"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        data = {
            "e": "bookTicker",
            "u": 400900217,
            "s": "BTCUSDT",
            "b": "50000.10",
            "B": "1.5",
            "a": "50000.20",
            "A": "2.0",
            "T": 1591097736594,
            "E": 1591097736593,
        }

        event = client._parse_book_ticker(data)

        assert event is not None
        assert event.symbol == "BTC/USDT:USDT"
        assert event.best_bid == Decimal("50000.10")
        assert event.best_ask == Decimal("50000.20")
        assert event.last_trade_price is None
        assert event.event_type == "book_ticker"
        assert event.timestamp_ms == 1591097736594

    def test_parse_book_ticker_invalid_spread(self):
        """测试无效价差（bid >= ask）"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        data = {
            "s": "BTCUSDT",
            "b": "50000.20",  # bid >= ask
            "a": "50000.10",
            "T": 1591097736594,
        }

        event = client._parse_book_ticker(data)
        assert event is None

    def test_parse_book_ticker_unsubscribed_symbol(self):
        """测试未订阅的 symbol"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],  # 只订阅了 BTC
            on_event=events.append,
        )

        data = {
            "s": "ETHUSDT",  # ETH 未订阅
            "b": "3000.10",
            "a": "3000.20",
            "T": 1591097736594,
        }

        event = client._parse_book_ticker(data)
        assert event is None


class TestParseAggTrade:
    """aggTrade 解析测试"""

    def test_parse_agg_trade_valid(self):
        """测试有效 aggTrade 解析"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        data = {
            "e": "aggTrade",
            "E": 1591097736593,
            "s": "BTCUSDT",
            "a": 8589026,
            "p": "50000.15",
            "q": "0.001",
            "f": 1,
            "l": 1,
            "T": 1591097736594,
            "m": True,
        }

        event = client._parse_agg_trade(data)

        assert event is not None
        assert event.symbol == "BTC/USDT:USDT"
        assert event.last_trade_price == Decimal("50000.15")
        assert event.best_bid is None
        assert event.best_ask is None
        assert event.event_type == "agg_trade"
        assert event.timestamp_ms == 1591097736594

    def test_parse_agg_trade_unsubscribed_symbol(self):
        """测试未订阅的 symbol"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        data = {
            "s": "ETHUSDT",  # ETH 未订阅
            "p": "3000.15",
            "T": 1591097736594,
        }

        event = client._parse_agg_trade(data)
        assert event is None


class TestStaleDataDetection:
    """数据陈旧检测测试"""

    def test_is_stale_no_data(self):
        """测试无数据时为陈旧"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
            stale_data_ms=1000,
        )

        # 没有任何更新记录
        assert client.is_stale("BTC/USDT:USDT") is True

    def test_is_stale_recent_data(self):
        """测试最近数据不陈旧"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
            stale_data_ms=1000,
        )

        # 模拟刚刚更新
        from src.utils.helpers import current_time_ms
        client._last_update_ms["BTC/USDT:USDT"] = current_time_ms()

        assert client.is_stale("BTC/USDT:USDT") is False

    def test_is_stale_old_data(self):
        """测试旧数据为陈旧"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
            stale_data_ms=1000,
        )

        # 模拟 2 秒前更新
        from src.utils.helpers import current_time_ms
        client._last_update_ms["BTC/USDT:USDT"] = current_time_ms() - 2000

        assert client.is_stale("BTC/USDT:USDT") is True

    def test_get_last_update_ms(self):
        """测试获取最后更新时间"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        # 无记录返回 0
        assert client.get_last_update_ms("BTC/USDT:USDT") == 0

        # 有记录返回时间戳
        client._last_update_ms["BTC/USDT:USDT"] = 1234567890
        assert client.get_last_update_ms("BTC/USDT:USDT") == 1234567890


class TestReconnection:
    """重连机制测试"""

    def test_reconnect_count_initial(self):
        """测试初始重连次数"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        assert client.reconnect_count == 0

    def test_exponential_backoff_calculation(self):
        """测试指数退避计算"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
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
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
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


class TestReconnectCallback:
    """重连成功回调测试"""

    @pytest.mark.asyncio
    async def test_on_reconnect_called_after_reconnect(self):
        events: List[MarketEvent] = []
        called: List[str] = []

        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
            on_reconnect=called.append,
        )

        # 模拟已发生过重连尝试
        client._reconnect_count = 1

        dummy_ws = MagicMock()
        dummy_ws.close = AsyncMock()
        dummy_ws.closed = False
        dummy_session = MagicMock()
        dummy_session.closed = False
        dummy_session.ws_connect = AsyncMock(return_value=dummy_ws)
        dummy_session.close = AsyncMock()

        with patch("src.ws.market.aiohttp.ClientSession", return_value=dummy_session):
            with patch.object(client, "_receive_loop", new=AsyncMock()):
                await client.connect()
                await client.disconnect()

        assert called == ["market_data"]


class TestConnectionState:
    """连接状态测试"""

    def test_is_connected_no_ws(self):
        """测试无 WS 连接时"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        assert client.is_connected is False


class TestHandleMessage:
    """消息处理测试"""

    @pytest.mark.asyncio
    async def test_handle_message_book_ticker(self):
        """测试处理 bookTicker 消息"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        # 初始化最后更新时间
        from src.utils.helpers import current_time_ms
        client._last_update_ms["BTC/USDT:USDT"] = current_time_ms()

        message = {
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "b": "50000.10",
                "a": "50000.20",
                "T": 1591097736594,
            }
        }

        await client._handle_message(message)

        assert len(events) == 1
        assert events[0].event_type == "book_ticker"
        assert events[0].best_bid == Decimal("50000.10")

    @pytest.mark.asyncio
    async def test_handle_message_agg_trade(self):
        """测试处理 aggTrade 消息"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        from src.utils.helpers import current_time_ms
        client._last_update_ms["BTC/USDT:USDT"] = current_time_ms()

        message = {
            "stream": "btcusdt@aggTrade",
            "data": {
                "s": "BTCUSDT",
                "p": "50000.15",
                "T": 1591097736594,
            }
        }

        await client._handle_message(message)

        assert len(events) == 1
        assert events[0].event_type == "agg_trade"
        assert events[0].last_trade_price == Decimal("50000.15")

    @pytest.mark.asyncio
    async def test_handle_message_unknown_stream(self):
        """测试处理未知流类型"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        message = {
            "stream": "btcusdt@unknown",
            "data": {}
        }

        await client._handle_message(message)

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_handle_message_mark_price(self):
        """测试处理 markPrice 消息"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        message = {
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": 1591097736593,
                "s": "BTCUSDT",
                "p": "50000.25",
            }
        }

        await client._handle_message(message)

        assert len(events) == 1
        assert events[0].event_type == "mark_price"
        assert events[0].mark_price == Decimal("50000.25")

    @pytest.mark.asyncio
    async def test_handle_message_empty_data(self):
        """测试处理空数据"""
        events: List[MarketEvent] = []
        client = MarketWSClient(
            symbols=["BTC/USDT:USDT"],
            on_event=events.append,
        )

        message = {
            "stream": "",
            "data": {}
        }

        await client._handle_message(message)

        assert len(events) == 0
