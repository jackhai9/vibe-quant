# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果与保护止损回归验证
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
保护性止损（ProtectiveStopManager）单元测试
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.exchange.adapter import ExchangeAdapter
from src.models import (
    AlgoOrderUpdate,
    OrderIntent,
    OrderResult,
    OrderStatus,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
    SymbolRules,
)
from src.risk.protective_stop import ProtectiveStopManager


class TestProtectiveStopPrice:
    def test_compute_stop_price_rounding(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        tick = Decimal("0.1")
        liq = Decimal("100")
        dist = Decimal("0.01")

        long_stop = mgr.compute_stop_price(
            position_side=PositionSide.LONG,
            liquidation_price=liq,
            dist_to_liq=dist,
            tick_size=tick,
        )
        # 100/0.99=101.0101..., LONG 采用向上规整
        assert long_stop == Decimal("101.1")

        short_stop = mgr.compute_stop_price(
            position_side=PositionSide.SHORT,
            liquidation_price=liq,
            dist_to_liq=dist,
            tick_size=tick,
        )
        # 100/1.01=99.0099..., SHORT 采用向下规整
        assert short_stop == Decimal("99.0")


@pytest.mark.asyncio
class TestProtectiveStopSync:
    async def test_sync_places_order_when_missing(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_called_once()
        intent: OrderIntent = exchange.place_order.call_args.args[0]
        assert intent.order_type == OrderType.STOP_MARKET
        assert intent.close_position is True
        assert intent.stop_price == Decimal("101.1")
        assert intent.is_risk is True

    async def test_sync_does_not_relax_long_stop_price(self):
        """LONG 只允许收紧：stopPrice 不允许下调（更松/更远）。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-L-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        # dist 变小会让 desired stopPrice 更低（更松）；应跳过更新
        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.005"),
        )

        exchange.cancel_algo_order.assert_not_called()
        exchange.place_order.assert_not_called()

    async def test_sync_does_not_relax_short_stop_price(self):
        """SHORT 只允许收紧：stopPrice 不允许上调（更松/更远）。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-S-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "SHORT",
                    "closePosition": True,
                    "triggerPrice": "99.0",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        # dist 变小会让 desired stopPrice 更高（更松）；应跳过更新
        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.005"),
        )

        exchange.cancel_algo_order.assert_not_called()
        exchange.place_order.assert_not_called()

    async def test_sync_cancels_order_when_no_position(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        symbol = "BTC/USDT:USDT"
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        cid = mgr.build_client_order_id(symbol, PositionSide.LONG)

        exchange.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "123",
                    "clientOrderId": cid,
                    "stopPrice": "101.1",
                    "info": {"positionSide": "LONG", "clientOrderId": cid, "stopPrice": "101.1"},
                }
            ]
        )
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "123",
                    "clientOrderId": cid,
                    "stopPrice": "101.1",
                    "info": {"positionSide": "LONG", "clientOrderId": cid, "stopPrice": "101.1"},
                }
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="123", status=OrderStatus.CANCELED)
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )

        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions={},  # 无仓位
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.cancel_algo_order.assert_called_once_with(symbol, "123")
        exchange.place_order.assert_not_called()

    async def test_sync_skips_when_external_close_position_algo_exists(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "external-stop-abc",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_not_called()

    async def test_sync_skips_when_external_reduce_only_stop_exists(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "ext-1",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "info": {"positionSide": "SHORT", "reduceOnly": True},
                }
            ]
        )
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "ext-1",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "info": {"positionSide": "SHORT", "reduceOnly": True},
                }
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_not_called()

    async def test_sync_logs_when_multiple_external_stops_exist(self, monkeypatch):
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(
            return_value=[
                {"id": "ext-1", "type": "stop_market", "reduceOnly": True, "info": {"positionSide": "SHORT"}},
                {"id": "ext-2", "type": "stop_market", "reduceOnly": True, "info": {"positionSide": "SHORT"}},
            ]
        )
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {"id": "ext-1", "type": "stop_market", "reduceOnly": True, "info": {"positionSide": "SHORT"}},
                {"id": "ext-2", "type": "stop_market", "reduceOnly": True, "info": {"positionSide": "SHORT"}},
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        assert any(e.get("reason") == "external_stop_multiple" and e.get("count") == 2 for e in events)
        exchange.place_order.assert_not_called()

    async def test_sync_startup_logs_existing_external_stop(self, monkeypatch):
        """启动同步时，若已存在外部 closePosition 条件单，应打印一次可读日志。"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "external-stop-abc",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
            sync_reason="startup",
        )

        assert any(e.get("reason") == "startup_existing_external_stop" for e in events)
        exchange.place_order.assert_not_awaited()
        exchange.place_order.assert_not_called()

    async def test_sync_cancels_own_order_when_external_close_position_exists(self):
        exchange = MagicMock(spec=ExchangeAdapter)
        symbol = "BTC/USDT:USDT"
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        own_cid = mgr.build_client_order_id(symbol, PositionSide.LONG)

        exchange.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "123",
                    "clientOrderId": own_cid,
                    "stopPrice": "101.1",
                    "info": {"positionSide": "LONG", "clientOrderId": own_cid, "stopPrice": "101.1"},
                }
            ]
        )
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "123",
                    "clientOrderId": own_cid,
                    "stopPrice": "101.1",
                    "info": {"positionSide": "LONG", "clientOrderId": own_cid, "stopPrice": "101.1"},
                }
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "external-stop-abc",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="123", status=OrderStatus.CANCELED)
        )

        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.cancel_algo_order.assert_called_once_with(symbol, "123")
        exchange.place_order.assert_not_called()

    async def test_sync_does_not_churn_on_float_trigger_price(self):
        """交易所若以 float 返回 triggerPrice，需按 tick 归一化避免反复撤旧建新。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])

        # 模拟 ccxt/交易所返回 float 抖动：8.267 -> 8.266999999999999
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-S-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "SHORT",
                    "closePosition": True,
                    "triggerPrice": 8.266999999999999,
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.001"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("8.0"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                # 使 desired_stop_price=8.267：liq = 8.267 * (1 + 0.015)
                liquidation_price=Decimal("8.391005"),
                mark_price=Decimal("8.1"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.015"),
        )

        exchange.cancel_algo_order.assert_not_called()
        exchange.place_order.assert_not_called()

    async def test_sync_skips_when_ws_external_hint_active(self):
        """外部接管锁存时，不应下我们自己的保护止损。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
            external_stop_latch_by_side={PositionSide.LONG: True},
        )

        exchange.place_order.assert_not_called()

    async def test_sync_does_not_modify_existing_order_during_ws_hint(self):
        """外部接管锁存时，已有我们自己的保护止损单应短暂保留，不撤不建。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        cid = mgr.build_client_order_id(symbol, PositionSide.SHORT)

        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": cid,
                    "orderType": "STOP_MARKET",
                    "positionSide": "SHORT",
                    "closePosition": True,
                    "triggerPrice": "99.0",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
            external_stop_latch_by_side={PositionSide.SHORT: True},
        )

        exchange.cancel_algo_order.assert_not_called()
        exchange.place_order.assert_not_called()


class TestOnAlgoOrderUpdate:
    """测试 on_algo_order_update 方法（清理本地状态）。"""

    def test_clears_state_on_canceled(self):
        """Algo Order 被撤销时，应清理本地 _states。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"

        # 模拟已有本地状态
        cid = mgr.build_client_order_id(symbol, PositionSide.LONG)
        from src.risk.protective_stop import ProtectiveStopState
        mgr._states[(symbol, PositionSide.LONG)] = ProtectiveStopState(
            symbol=symbol,
            position_side=PositionSide.LONG,
            client_order_id=cid,
            order_id="123",
        )

        # 模拟 ALGO_UPDATE: CANCELED
        update = AlgoOrderUpdate(
            symbol=symbol,
            algo_id="123",
            client_algo_id=cid,
            side=OrderSide.SELL,
            status="CANCELED",
            timestamp_ms=1234567890,
        )

        mgr.on_algo_order_update(update)

        # 状态应被清理
        assert (symbol, PositionSide.LONG) not in mgr._states

    def test_clears_state_on_triggered(self):
        """Algo Order 被触发时，应清理本地 _states。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "ETH/USDT:USDT"

        cid = mgr.build_client_order_id(symbol, PositionSide.SHORT)
        from src.risk.protective_stop import ProtectiveStopState
        mgr._states[(symbol, PositionSide.SHORT)] = ProtectiveStopState(
            symbol=symbol,
            position_side=PositionSide.SHORT,
            client_order_id=cid,
            order_id="456",
        )

        update = AlgoOrderUpdate(
            symbol=symbol,
            algo_id="456",
            client_algo_id=cid,
            side=OrderSide.BUY,
            status="TRIGGERED",
            timestamp_ms=1234567890,
        )

        mgr.on_algo_order_update(update)

        assert (symbol, PositionSide.SHORT) not in mgr._states

    def test_ignores_non_terminal_status(self):
        """非终态（如 NEW）不应清理 _states。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"

        cid = mgr.build_client_order_id(symbol, PositionSide.LONG)
        from src.risk.protective_stop import ProtectiveStopState
        mgr._states[(symbol, PositionSide.LONG)] = ProtectiveStopState(
            symbol=symbol,
            position_side=PositionSide.LONG,
            client_order_id=cid,
            order_id="123",
        )

        update = AlgoOrderUpdate(
            symbol=symbol,
            algo_id="123",
            client_algo_id=cid,
            side=OrderSide.SELL,
            status="NEW",  # 非终态
            timestamp_ms=1234567890,
        )

        mgr.on_algo_order_update(update)

        # 状态应保留
        assert (symbol, PositionSide.LONG) in mgr._states

    def test_ignores_non_matching_prefix(self):
        """不匹配前缀的订单不应清理 _states。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"

        cid = mgr.build_client_order_id(symbol, PositionSide.LONG)
        from src.risk.protective_stop import ProtectiveStopState
        mgr._states[(symbol, PositionSide.LONG)] = ProtectiveStopState(
            symbol=symbol,
            position_side=PositionSide.LONG,
            client_order_id=cid,
            order_id="123",
        )

        # 外部订单（不匹配前缀）
        update = AlgoOrderUpdate(
            symbol=symbol,
            algo_id="999",
            client_algo_id="external-stop-abc",  # 不匹配
            side=OrderSide.SELL,
            status="CANCELED",
            timestamp_ms=1234567890,
        )

        mgr.on_algo_order_update(update)

        # 状态应保留
        assert (symbol, PositionSide.LONG) in mgr._states


class TestStopPriceValidation:
    """止损价有效性检查测试"""

    def test_long_valid_stop_price(self):
        """LONG 止损价高于爆仓价时有效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 101 > 爆仓价 100 * 1.0001 = 100.01
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.LONG,
            stop_price=Decimal("101"),
            liquidation_price=Decimal("100"),
        ) is True

    def test_long_invalid_stop_price_below_liq(self):
        """LONG 止损价低于爆仓价时无效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 99 < 爆仓价 100
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.LONG,
            stop_price=Decimal("99"),
            liquidation_price=Decimal("100"),
        ) is False

    def test_long_invalid_stop_price_too_close(self):
        """LONG 止损价接近爆仓价（< 0.01%）时无效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 100.005 < 100 * 1.0001 = 100.01
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.LONG,
            stop_price=Decimal("100.005"),
            liquidation_price=Decimal("100"),
        ) is False

    def test_short_valid_stop_price(self):
        """SHORT 止损价低于爆仓价时有效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 99 < 爆仓价 100 * 0.9999 = 99.99
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.SHORT,
            stop_price=Decimal("99"),
            liquidation_price=Decimal("100"),
        ) is True

    def test_short_invalid_stop_price_above_liq(self):
        """SHORT 止损价高于爆仓价时无效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 101 > 爆仓价 100
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.SHORT,
            stop_price=Decimal("101"),
            liquidation_price=Decimal("100"),
        ) is False

    def test_short_invalid_stop_price_too_close(self):
        """SHORT 止损价接近爆仓价（< 0.01%）时无效"""
        exchange = MagicMock(spec=ExchangeAdapter)
        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")

        # 止损价 99.995 > 100 * 0.9999 = 99.99
        assert mgr.is_stop_price_valid(
            position_side=PositionSide.SHORT,
            stop_price=Decimal("99.995"),
            liquidation_price=Decimal("100"),
        ) is False


@pytest.mark.asyncio
class TestInvalidExternalStop:
    """无效外部止损场景测试"""

    async def test_cancels_invalid_external_short_stop(self, monkeypatch):
        """SHORT 外部止损价高于爆仓价时，取消外部止损并由程序接管"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "ext-invalid",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "triggerPrice": "110",  # 高于爆仓价 100，无效
                    "info": {"positionSide": "SHORT", "reduceOnly": True, "triggerPrice": "110"},
                }
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="new-1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="ext-invalid", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("90"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),  # 爆仓价
                mark_price=Decimal("95"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        # 应该取消无效的外部止损
        exchange.cancel_algo_order.assert_called()
        # 应该下新的有效止损单
        exchange.place_order.assert_called()
        # 应该有 cancel_invalid_external_stop 日志
        assert any(e.get("reason") == "cancel_invalid_external_stop" for e in events)

    async def test_valid_external_keeps_takeover(self, monkeypatch):
        """存在有效外部止损时保持外部接管（仅清理无效单）"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "ext-invalid",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "triggerPrice": "110",
                    "info": {"positionSide": "SHORT", "reduceOnly": True, "triggerPrice": "110"},
                },
                {
                    "id": "ext-valid",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "triggerPrice": "90",
                    "info": {"positionSide": "SHORT", "reduceOnly": True, "triggerPrice": "90"},
                },
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="new-1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="ext-invalid", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("90"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("95"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.cancel_algo_order.assert_called()
        exchange.place_order.assert_not_called()
        assert any(e.get("reason") == "cancel_invalid_external_stop" for e in events)

    async def test_invalid_external_ignores_latch(self, monkeypatch):
        """无效外部止损在锁存期内也应允许接管"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(
            return_value=[
                {
                    "id": "ext-invalid",
                    "type": "stop_market",
                    "reduceOnly": True,
                    "triggerPrice": "110",
                    "info": {"positionSide": "SHORT", "reduceOnly": True, "triggerPrice": "110"},
                }
            ]
        )
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="new-1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="ext-invalid", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("-0.01"),
                entry_price=Decimal("90"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("95"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
            external_stop_latch_by_side={PositionSide.SHORT: True},
        )

        exchange.cancel_algo_order.assert_called()
        exchange.place_order.assert_called()
        assert any(e.get("reason") == "cancel_invalid_external_stop" for e in events)


@pytest.mark.asyncio
class TestProtectiveStopLiqWrongSide:
    """交叉保证金下爆仓价方向异常：跳过保护止损"""

    async def test_short_liq_below_mark_skips(self):
        """SHORT 持仓但 liq < mark（对冲方向导致），应跳过而非尝试下单"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "DASH/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.001"),
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        # SHORT 持仓，但 liq_price(29.52) < mark_price(31.83)
        # 交叉保证金下 LONG 主导时会出现这种情况
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("1.97"),
                entry_price=Decimal("30.50"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
                liquidation_price=Decimal("29.52"),
                mark_price=Decimal("31.83"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        # 不应尝试下单
        exchange.place_order.assert_not_called()

    async def test_long_liq_above_mark_skips(self):
        """LONG 持仓但 liq > mark（对冲方向导致），应跳过"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "DASH/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.001"),
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        # LONG 持仓，但 liq_price(35.00) > mark_price(31.83)
        # 交叉保证金下 SHORT 主导时的对称情况
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("1.97"),
                entry_price=Decimal("30.50"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
                liquidation_price=Decimal("35.00"),
                mark_price=Decimal("31.83"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_not_called()

    async def test_short_liq_above_mark_proceeds(self):
        """SHORT 持仓且 liq > mark（正常），应正常下单"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        # SHORT 持仓，liq_price(100) > mark_price(95) — 正常情况
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("90"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("95"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_called_once()

    async def test_no_mark_price_still_attempts(self):
        """mark_price 为 None 时不做方向检查，仍尝试下单"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        # mark_price = None，无法判断方向，应正常尝试
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("90"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=None,
            )
        }

        await mgr.sync_symbol(
            symbol=symbol,
            rules=rules,
            positions=positions,
            enabled=True,
            dist_to_liq=Decimal("0.01"),
        )

        exchange.place_order.assert_called_once()

    async def test_wrong_side_log_dedup(self, monkeypatch):
        """同一 symbol+side 方向异常连续 sync 两次，只记录一次 skip_liq_wrong_side"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(return_value=[])
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "DASH/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.001"),
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.SHORT: Position(
                symbol=symbol,
                position_side=PositionSide.SHORT,
                position_amt=Decimal("1.97"),
                entry_price=Decimal("30.50"),
                unrealized_pnl=Decimal("0"),
                leverage=5,
                liquidation_price=Decimal("29.52"),
                mark_price=Decimal("31.83"),
            )
        }

        # 第一次 sync
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )
        # 第二次 sync（方向仍异常）
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )

        wrong_side_events = [e for e in events if e.get("reason") == "skip_liq_wrong_side"]
        assert len(wrong_side_events) == 1  # 只记录一次
        exchange.place_order.assert_not_called()


class TestProtectiveStopAdoptionLog:
    """adoption 路径条件日志: 仅在本地状态缺失时打印 info 日志"""

    @pytest.mark.asyncio
    async def test_adopt_existing_logs_when_no_local_state(self, monkeypatch):
        """本地无状态时发现既有订单(价格一致), 应打 adopt_existing 日志"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-L-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        # _states 为空(无本地状态), 发现既有订单价格一致 -> 应打日志
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )

        adopt_events = [e for e in events if e.get("reason") == "adopt_existing"]
        assert len(adopt_events) == 1
        assert adopt_events[0]["order_id"] == "999"
        exchange.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_adopt_existing_silent_when_local_state_exists(self, monkeypatch):
        """本地已有状态时, adopt_existing 不打日志(避免刷屏)"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-L-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        # 第一次 sync: 无本地状态, 会打 adopt_existing
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )
        events.clear()

        # 第二次 sync: 本地已有状态, 不应再打 adopt_existing
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )

        adopt_events = [e for e in events if e.get("reason") == "adopt_existing"]
        assert len(adopt_events) == 0

    @pytest.mark.asyncio
    async def test_keep_tighter_logs_when_no_local_state(self, monkeypatch):
        """本地无状态时发现更紧的既有订单(拒绝放松), 应打 keep_existing_tighter 日志"""
        events: list[dict] = []

        def fake_log_event(*_args, **kwargs):
            events.append(kwargs)

        monkeypatch.setattr("src.risk.protective_stop.log_event", fake_log_event)

        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            return_value=[
                {
                    "algoId": "999",
                    "clientAlgoId": "vq-ps-btcusdt-L-12345",
                    "orderType": "STOP_MARKET",
                    "positionSide": "LONG",
                    "closePosition": True,
                    "triggerPrice": "101.1",
                }
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="999", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(exchange, client_order_id_prefix="vq-ps-")
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }

        # dist_to_liq=0.005 -> desired_stop 更低(更松), 但既有 101.1(更紧) -> keep_existing_tighter
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions,
            enabled=True, dist_to_liq=Decimal("0.005"),
        )

        keep_events = [e for e in events if e.get("reason") == "keep_existing_tighter"]
        assert len(keep_events) == 1
        assert keep_events[0]["order_id"] == "999"
        exchange.place_order.assert_not_called()
        exchange.cancel_algo_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_allows_loosen_when_liq_improves_enough(self):
        """C: 爆仓价改善超过阈值时，允许放松保护止损（撤旧建新）。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            side_effect=[
                [],  # 第一次：无既有单，直接下单
                [    # 第二次：存在既有单（更紧）
                    {
                        "algoId": "1",
                        "clientAlgoId": "vq-ps-btcusdt-L-12345",
                        "orderType": "STOP_MARKET",
                        "positionSide": "LONG",
                        "closePosition": True,
                        "triggerPrice": "101.1",
                    }
                ],
            ]
        )
        exchange.place_order = AsyncMock(
            side_effect=[
                OrderResult(success=True, order_id="1", status=OrderStatus.NEW),
                OrderResult(success=True, order_id="2", status=OrderStatus.NEW),
            ]
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(
            exchange,
            client_order_id_prefix="vq-ps-",
            allow_loosen_on_liq_improve=True,
            liq_improve_threshold=Decimal("0.005"),
            loosen_cooldown_s=0,
        )
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions_v1 = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }
        positions_v2 = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("98"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions_v1,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions_v2,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )

        exchange.cancel_algo_order.assert_called_once_with(symbol, "1")
        assert exchange.place_order.await_count == 2

    @pytest.mark.asyncio
    async def test_sync_keeps_tighter_when_liq_improve_below_threshold(self):
        """C: 爆仓价改善不足阈值时，仍保持只收紧策略。"""
        exchange = MagicMock(spec=ExchangeAdapter)
        exchange.fetch_open_orders = AsyncMock(return_value=[])
        exchange.fetch_open_orders_raw = AsyncMock(return_value=[])
        exchange.fetch_open_algo_orders = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "algoId": "1",
                        "clientAlgoId": "vq-ps-btcusdt-L-12345",
                        "orderType": "STOP_MARKET",
                        "positionSide": "LONG",
                        "closePosition": True,
                        "triggerPrice": "101.1",
                    }
                ],
            ]
        )
        exchange.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.NEW)
        )
        exchange.cancel_algo_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="1", status=OrderStatus.CANCELED)
        )

        mgr = ProtectiveStopManager(
            exchange,
            client_order_id_prefix="vq-ps-",
            liq_improve_threshold=Decimal("0.005"),
            loosen_cooldown_s=0,
        )
        symbol = "BTC/USDT:USDT"
        rules = SymbolRules(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        positions_v1 = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("100"),
                mark_price=Decimal("110"),
            )
        }
        # 100 -> 99.7 仅改善 0.3%，低于 0.5% 阈值
        positions_v2 = {
            PositionSide.LONG: Position(
                symbol=symbol,
                position_side=PositionSide.LONG,
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=10,
                liquidation_price=Decimal("99.7"),
                mark_price=Decimal("110"),
            )
        }

        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions_v1,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )
        await mgr.sync_symbol(
            symbol=symbol, rules=rules, positions=positions_v2,
            enabled=True, dist_to_liq=Decimal("0.01"),
        )

        exchange.cancel_algo_order.assert_not_called()
        assert exchange.place_order.await_count == 1
