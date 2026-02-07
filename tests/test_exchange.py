# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
交易所适配器单元测试
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import ccxt.async_support as ccxt
from tempfile import TemporaryDirectory
from pathlib import Path

from src.exchange.adapter import ExchangeAdapter
from src.models import (
    Position,
    PositionSide,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeInForce,
    SymbolRules,
    OrderIntent,
    OrderResult,
)
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class TestExchangeAdapterInit:
    """初始化测试"""

    def test_init(self):
        """测试初始化"""
        adapter = ExchangeAdapter(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
        )
        assert adapter.api_key == "test_key"
        assert adapter.api_secret == "test_secret"
        assert adapter.testnet is True
        assert adapter._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_disables_fetch_currencies_and_sets_proxy(self):
        """initialize() 默认禁用 fetchCurrencies，并透传 aiohttp_proxy。"""
        dummy_exchange = MagicMock()
        dummy_exchange.load_markets = AsyncMock(return_value=None)
        dummy_exchange.markets = {}

        with patch("src.exchange.adapter.ccxt.binanceusdm", return_value=dummy_exchange) as mock_ctor:
            adapter = ExchangeAdapter(
                api_key="test_key",
                api_secret="test_secret",
                testnet=False,
                proxy="http://127.0.0.1:7890",
            )
            await adapter.initialize()

            cfg = mock_ctor.call_args.args[0]
            assert cfg["aiohttp_proxy"] == "http://127.0.0.1:7890"
            assert cfg["options"]["fetchCurrencies"] is False

    @pytest.mark.asyncio
    async def test_initialize_failure_closes_exchange(self):
        """initialize() 失败时显式 close，避免 Unclosed client session。"""
        dummy_exchange = MagicMock()
        dummy_exchange.load_markets = AsyncMock(side_effect=RuntimeError("boom"))
        dummy_exchange.close = AsyncMock(return_value=None)

        with patch("src.exchange.adapter.ccxt.binanceusdm", return_value=dummy_exchange):
            adapter = ExchangeAdapter(api_key="k", api_secret="s", testnet=False)
            with pytest.raises(RuntimeError):
                await adapter.initialize()

            dummy_exchange.close.assert_awaited()
            assert adapter._initialized is False
            assert adapter._exchange is None


class TestRoundingFunctions:
    """规整函数测试"""

    @pytest.fixture
    def adapter_with_rules(self):
        """创建带规则的适配器"""
        adapter = ExchangeAdapter("key", "secret")
        # 手动设置规则
        adapter._rules = {
            "BTC/USDT:USDT": SymbolRules(
                symbol="BTC/USDT:USDT",
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("5"),
            ),
            "ETH/USDT:USDT": SymbolRules(
                symbol="ETH/USDT:USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("5"),
            ),
        }
        return adapter

    def test_round_price(self, adapter_with_rules):
        """测试价格规整"""
        adapter = adapter_with_rules

        # BTC tick_size=0.1
        assert adapter.round_price("BTC/USDT:USDT", Decimal("50000.15")) == Decimal("50000.1")
        assert adapter.round_price("BTC/USDT:USDT", Decimal("50000.05")) == Decimal("50000.0")
        assert adapter.round_price("BTC/USDT:USDT", Decimal("50000.19")) == Decimal("50000.1")

        # ETH tick_size=0.01
        assert adapter.round_price("ETH/USDT:USDT", Decimal("3000.155")) == Decimal("3000.15")
        assert adapter.round_price("ETH/USDT:USDT", Decimal("3000.159")) == Decimal("3000.15")

    def test_round_qty(self, adapter_with_rules):
        """测试数量规整"""
        adapter = adapter_with_rules

        # BTC step_size=0.001
        assert adapter.round_qty("BTC/USDT:USDT", Decimal("0.0015")) == Decimal("0.001")
        assert adapter.round_qty("BTC/USDT:USDT", Decimal("0.0019")) == Decimal("0.001")
        assert adapter.round_qty("BTC/USDT:USDT", Decimal("0.001")) == Decimal("0.001")

        # ETH step_size=0.01
        assert adapter.round_qty("ETH/USDT:USDT", Decimal("0.015")) == Decimal("0.01")
        assert adapter.round_qty("ETH/USDT:USDT", Decimal("0.019")) == Decimal("0.01")

    def test_ensure_min_notional_already_satisfied(self, adapter_with_rules):
        """测试 minNotional 已满足"""
        adapter = adapter_with_rules

        # qty * price = 0.001 * 50000 = 50 >= 5
        result = adapter.ensure_min_notional("BTC/USDT:USDT", Decimal("0.001"), Decimal("50000"))
        assert result == Decimal("0.001")

    def test_ensure_min_notional_needs_increase(self, adapter_with_rules):
        """测试 minNotional 需要增大 qty"""
        adapter = adapter_with_rules

        # qty * price = 0.00001 * 50000 = 0.5 < 5
        # 需要 qty >= 5 / 50000 = 0.0001
        # 按 step_size=0.001 向上取整 = 0.001
        result = adapter.ensure_min_notional("BTC/USDT:USDT", Decimal("0.00001"), Decimal("50000"))
        assert result >= Decimal("0.001")
        assert result * Decimal("50000") >= Decimal("5")

    def test_is_position_complete_zero(self, adapter_with_rules):
        """测试仓位完成 - 零仓位"""
        adapter = adapter_with_rules
        assert adapter.is_position_complete("BTC/USDT:USDT", Decimal("0")) is True

    def test_is_position_complete_below_min_qty(self, adapter_with_rules):
        """测试仓位完成 - 低于 minQty"""
        adapter = adapter_with_rules
        # min_qty = 0.001, 仓位 0.0005 < 0.001
        assert adapter.is_position_complete("BTC/USDT:USDT", Decimal("0.0005")) is True

    def test_is_position_complete_above_min_qty(self, adapter_with_rules):
        """测试仓位未完成 - 高于 minQty"""
        adapter = adapter_with_rules
        assert adapter.is_position_complete("BTC/USDT:USDT", Decimal("0.01")) is False

    def test_get_tradable_qty_normal(self, adapter_with_rules):
        """测试可交易数量 - 正常情况"""
        adapter = adapter_with_rules
        # 0.0155 -> 0.015 (按 step_size 规整)
        result = adapter.get_tradable_qty("BTC/USDT:USDT", Decimal("0.0155"))
        assert result == Decimal("0.015")

    def test_get_tradable_qty_below_min(self, adapter_with_rules):
        """测试可交易数量 - 低于 minQty"""
        adapter = adapter_with_rules
        # 0.0005 < 0.001 (min_qty), 返回 0
        result = adapter.get_tradable_qty("BTC/USDT:USDT", Decimal("0.0005"))
        assert result == Decimal("0")

    def test_get_rules(self, adapter_with_rules):
        """测试获取规则"""
        adapter = adapter_with_rules
        rules = adapter.get_rules("BTC/USDT:USDT")
        assert rules is not None
        assert rules.tick_size == Decimal("0.1")
        assert rules.step_size == Decimal("0.001")

        # 不存在的 symbol
        assert adapter.get_rules("UNKNOWN/USDT:USDT") is None


class TestExtractRules:
    """规则提取测试"""

    def test_extract_rules_from_market(self):
        """测试从 market 数据提取规则"""
        adapter = ExchangeAdapter("key", "secret")

        market = {
            "precision": {
                "price": 0.01,
                "amount": 0.001,
            },
            "limits": {
                "amount": {"min": 0.001},
                "cost": {"min": 5},
            },
            "linear": True,
            "swap": True,
        }

        rules = adapter._extract_rules("BTC/USDT:USDT", market)
        assert rules.symbol == "BTC/USDT:USDT"
        assert rules.tick_size == Decimal("0.01")
        assert rules.step_size == Decimal("0.001")
        assert rules.min_qty == Decimal("0.001")
        assert rules.min_notional == Decimal("5")

    def test_extract_rules_from_binance_filters(self):
        """测试从 Binance filters 提取 minNotional"""
        adapter = ExchangeAdapter("key", "secret")

        market = {
            "precision": {
                "price": 0.01,
                "amount": 0.001,
            },
            "limits": {
                "amount": {"min": 0.001},
                "cost": {},  # 没有 min
            },
            "info": {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "notional": "10"},
                ]
            },
            "linear": True,
            "swap": True,
        }

        rules = adapter._extract_rules("BTC/USDT:USDT", market)
        assert rules.min_notional == Decimal("10")


class TestOrderStatusParsing:
    """订单状态解析测试"""

    def test_parse_order_status(self):
        """测试订单状态解析"""
        adapter = ExchangeAdapter("key", "secret")

        assert adapter._parse_order_status("open") == OrderStatus.NEW
        assert adapter._parse_order_status("new") == OrderStatus.NEW
        assert adapter._parse_order_status("partially_filled") == OrderStatus.PARTIALLY_FILLED
        assert adapter._parse_order_status("filled") == OrderStatus.FILLED
        assert adapter._parse_order_status("closed") == OrderStatus.FILLED
        assert adapter._parse_order_status("canceled") == OrderStatus.CANCELED
        assert adapter._parse_order_status("cancelled") == OrderStatus.CANCELED
        assert adapter._parse_order_status("rejected") == OrderStatus.REJECTED
        assert adapter._parse_order_status("expired") == OrderStatus.EXPIRED
        assert adapter._parse_order_status("unknown") == OrderStatus.NEW  # 默认


class TestAsyncMethods:
    """异步方法测试（使用 mock）"""

    @pytest.fixture
    def mock_exchange(self):
        """创建 mock exchange"""
        mock = MagicMock()
        mock.load_markets = AsyncMock(return_value=None)
        mock.fetch_positions = AsyncMock(return_value=[])
        mock.create_order = AsyncMock(return_value={
            "id": "12345",
            "clientOrderId": "client_123",
            "status": "open",
            "filled": 0,
            "average": None,
        })
        mock.cancel_order = AsyncMock(return_value={
            "id": "12345",
            "status": "canceled",
            "filled": 0,
            "average": None,
        })
        mock.fapiPrivateDeleteAlgoOrder = AsyncMock(return_value={})
        mock.cancel_all_orders = AsyncMock(return_value=[])
        mock.fetch_open_orders = AsyncMock(return_value=[])
        mock.close = AsyncMock()
        mock.markets = {
            "BTC/USDT:USDT": {
                "precision": {"price": 0.1, "amount": 0.001},
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
                "linear": True,
                "swap": True,
            }
        }
        return mock

    @pytest.mark.asyncio
    async def test_initialize(self, mock_exchange):
        """测试初始化"""
        adapter = ExchangeAdapter("key", "secret")

        with patch("src.exchange.adapter.ccxt.binanceusdm", return_value=mock_exchange):
            await adapter.initialize()
            assert adapter._initialized is True

    @pytest.mark.asyncio
    async def test_close(self, mock_exchange):
        """测试关闭"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        await adapter.close()
        assert adapter._initialized is False
        mock_exchange.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_place_order(self, mock_exchange):
        """测试下单"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTX,
            reduce_only=True,
        )

        result = await adapter.place_order(intent)
        assert result.success is True
        assert result.order_id == "12345"
        assert result.status == OrderStatus.NEW
        mock_exchange.create_order.assert_called_once()
        called_params = mock_exchange.create_order.call_args.kwargs.get("params", {})
        assert called_params.get("positionSide") == "LONG"
        assert "reduceOnly" not in called_params

    @pytest.mark.asyncio
    async def test_place_order_should_set_new_client_order_id(self, mock_exchange):
        """测试下单时透传 newClientOrderId（用于标记本程序订单）"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTX,
            reduce_only=True,
            client_order_id="vq-123-1",
        )

        await adapter.place_order(intent)
        called_params = mock_exchange.create_order.call_args.kwargs.get("params", {})
        assert called_params.get("newClientOrderId") == "vq-123-1"

    @pytest.mark.asyncio
    async def test_place_stop_market_close_position(self, mock_exchange):
        """测试保护性止损（STOP_MARKET closePosition）参数透传"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        intent = OrderIntent(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            qty=Decimal("0"),
            order_type=OrderType.STOP_MARKET,
            stop_price=Decimal("101.1"),
            close_position=True,
            reduce_only=True,
            client_order_id="vq-ps-btcusdt-L",
            is_risk=True,
        )

        await adapter.place_order(intent)
        called_params = mock_exchange.create_order.call_args.kwargs.get("params", {})
        assert called_params.get("positionSide") == "LONG"
        assert called_params.get("newClientOrderId") == "vq-ps-btcusdt-L"
        assert called_params.get("stopPrice") == 101.1
        assert called_params.get("workingType") == "MARK_PRICE"
        assert called_params.get("closePosition") is True
        assert "timeInForce" not in called_params

    @pytest.mark.asyncio
    async def test_cancel_order(self, mock_exchange):
        """测试撤单"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        result = await adapter.cancel_order("BTC/USDT:USDT", "12345")
        assert result.success is True
        assert result.order_id == "12345"
        assert result.status == OrderStatus.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_any_order_fallback_to_algo(self, mock_exchange):
        """测试混合撤单回退到 algo"""
        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        mock_exchange.cancel_order = AsyncMock(side_effect=ccxt.OrderNotFound("not found"))
        mock_exchange.fapiPrivateDeleteAlgoOrder = AsyncMock(return_value={})

        result = await adapter.cancel_any_order("BTC/USDT:USDT", "12345")
        assert result.success is True
        assert result.order_id == "12345"
        assert result.status == OrderStatus.CANCELED
        mock_exchange.fapiPrivateDeleteAlgoOrder.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_positions(self, mock_exchange):
        """测试获取仓位"""
        mock_exchange.fetch_positions = AsyncMock(return_value=[
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": 0.01,
                "side": "long",
                "entryPrice": 50000,
                "unrealizedPnl": 10,
                "leverage": 10,
                "liquidationPrice": 45000,
                "markPrice": 50010,
            },
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": 0.005,
                "side": "short",
                "entryPrice": 51000,
                "unrealizedPnl": -5,
                "leverage": 10,
                "liquidationPrice": 56000,
                "markPrice": 50010,
            },
        ])

        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        positions = await adapter.fetch_positions()
        assert len(positions) == 2

        long_pos = [p for p in positions if p.position_side == PositionSide.LONG][0]
        assert long_pos.position_amt == Decimal("0.01")
        assert long_pos.entry_price == Decimal("50000")

        short_pos = [p for p in positions if p.position_side == PositionSide.SHORT][0]
        assert short_pos.position_amt == Decimal("-0.005")  # SHORT 为负

    @pytest.mark.asyncio
    async def test_fetch_positions_handles_none_fields(self, mock_exchange):
        """测试获取仓位 - 兼容 None 字段"""
        mock_exchange.fetch_positions = AsyncMock(return_value=[
            {
                "symbol": "ZEN/USDT:USDT",
                "contracts": 0.1,
                "side": "long",
                "entryPrice": None,
                "unrealizedPnl": None,
                "leverage": None,
                "liquidationPrice": None,
                "markPrice": None,
            },
        ])

        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        positions = await adapter.fetch_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "ZEN/USDT:USDT"
        assert positions[0].entry_price == Decimal("0")
        assert positions[0].unrealized_pnl == Decimal("0")
        assert positions[0].leverage == 1
        assert positions[0].liquidation_price is None
        assert positions[0].mark_price is None

    @pytest.mark.asyncio
    async def test_fetch_leverage_map(self, mock_exchange):
        """测试通过 positionRisk 拉取杠杆映射"""
        mock_exchange.fapiPrivateV2GetPositionRisk = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "leverage": "25"},
            {"symbol": "ETHUSDT", "leverage": 10},
            {"symbol": "XRPUSDT", "leverage": None},
        ])

        adapter = ExchangeAdapter("key", "secret")
        adapter._exchange = mock_exchange
        adapter._initialized = True

        result = await adapter.fetch_leverage_map(["BTC/USDT:USDT", "ETH/USDT:USDT"])
        assert result == {
            "BTC/USDT:USDT": 25,
            "ETH/USDT:USDT": 10,
        }

    @pytest.mark.asyncio
    async def test_ensure_initialized_error(self):
        """测试未初始化时报错"""
        adapter = ExchangeAdapter("key", "secret")

        with pytest.raises(RuntimeError, match="未初始化"):
            await adapter.fetch_positions()
