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
        exchange.cancel_order = AsyncMock(
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_not_called()
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_not_called()
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_called_once_with(symbol, "123")
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
        exchange.cancel_order = AsyncMock(
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
        exchange.cancel_order = AsyncMock(
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
        exchange.cancel_order = AsyncMock(
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_called_once_with(symbol, "123")
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_not_called()
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
        exchange.cancel_order = AsyncMock(
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

        exchange.cancel_order.assert_not_called()
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
