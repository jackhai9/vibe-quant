"""
仓位保护性止损（交易所端条件单）

目标：
- 为每个有持仓的 symbol + positionSide 维护一个 STOP_MARKET 条件单
- 使用 markPrice 触发，尽量在接近强平前自动平仓（防程序崩溃/休眠/断网）

实现策略：
- clientOrderId 使用前缀 + 时间戳（前缀跨 run 一致，便于识别；时间戳避免重复）
- 仅在持仓存在时维护；仓位归零后自动撤销（避免误触发开仓）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from src.exchange.adapter import ExchangeAdapter
from src.models import (
    OrderIntent,
    OrderSide,
    OrderType,
    OrderStatus,
    OrderUpdate,
    Position,
    PositionSide,
    SymbolRules,
)
from src.utils.helpers import round_to_tick, round_up_to_tick, symbol_to_ws_stream
from src.utils.logger import log_event, log_error


@dataclass
class ProtectiveStopState:
    symbol: str
    position_side: PositionSide
    client_order_id: str
    order_id: Optional[str] = None
    stop_price: Optional[Decimal] = None


class ProtectiveStopManager:
    """保护性止损管理器（按 symbol + positionSide 维护 1 张条件单）。"""

    def __init__(
        self,
        exchange: ExchangeAdapter,
        *,
        client_order_id_prefix: str,
    ):
        self._exchange = exchange
        self._client_order_id_prefix = client_order_id_prefix
        self._states: Dict[tuple[str, PositionSide], ProtectiveStopState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        lock = self._locks.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[symbol] = lock
        return lock

    def _build_client_order_id_prefix(self, symbol: str, position_side: PositionSide) -> str:
        """生成 clientOrderId 前缀（用于识别属于本程序的保护止损单）。"""
        ws_symbol = symbol_to_ws_stream(symbol)
        side_code = "L" if position_side == PositionSide.LONG else "S"
        prefix = f"{self._client_order_id_prefix}{ws_symbol}-{side_code}"
        if len(prefix) >= 30:
            # 极少数超长 symbol：退化为 hash
            prefix = f"{self._client_order_id_prefix}{hash(ws_symbol) & 0xfffffff:07x}-{side_code}"
        return prefix

    def build_client_order_id(self, symbol: str, position_side: PositionSide) -> str:
        """生成唯一的 clientOrderId（前缀 + 时间戳，Binance 要求 clientOrderId 7 天内唯一）。"""
        prefix = self._build_client_order_id_prefix(symbol, position_side)
        ts = int(time.time() * 1000) % 100000  # 5位时间戳后缀
        cid = f"{prefix}-{ts}"
        if len(cid) > 36:
            # Binance clientOrderId 限制 36 字符
            cid = cid[:36]
        return cid

    def _match_client_order_id(self, cid: str, symbol: str, position_side: PositionSide) -> bool:
        """检查 clientOrderId 是否属于指定 symbol+side 的保护止损单。"""
        prefix = self._build_client_order_id_prefix(symbol, position_side)
        return cid.startswith(prefix)

    def compute_stop_price(
        self,
        *,
        position_side: PositionSide,
        liquidation_price: Decimal,
        dist_to_liq: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        """
        按 dist_to_liq 反推 stopPrice（触发时 dist_to_liq ≈ dist_to_liq）。

        dist_to_liq = abs(mark_price - liquidation_price) / mark_price

        LONG: mark_price 下跌接近 liquidation_price -> 触发 SELL stop
          mark = liq / (1 - dist)
        SHORT: mark_price 上涨接近 liquidation_price -> 触发 BUY stop
          mark = liq / (1 + dist)

        规整策略（更早触发更安全）：
        - LONG（SELL stop）：stopPrice 向上规整（更高 -> 更早触发）
        - SHORT（BUY stop）：stopPrice 向下规整（更低 -> 更早触发）
        """
        if liquidation_price <= Decimal("0"):
            raise ValueError("liquidation_price must be > 0")
        if dist_to_liq <= Decimal("0") or dist_to_liq >= Decimal("1"):
            raise ValueError("dist_to_liq must be in (0, 1)")

        if position_side == PositionSide.LONG:
            raw = liquidation_price / (Decimal("1") - dist_to_liq)
            return round_up_to_tick(raw, tick_size)
        raw = liquidation_price / (Decimal("1") + dist_to_liq)
        return round_to_tick(raw, tick_size)

    def _extract_order_id(self, order: Dict[str, Any]) -> Optional[str]:
        """提取订单 ID（支持 algo order 的 algoId 和普通订单的 id）"""
        oid = order.get("algoId") or order.get("id")
        if oid:
            return str(oid)
        info = order.get("info")
        if isinstance(info, dict):
            oid = info.get("algoId") or info.get("id")
            if oid:
                return str(oid)
        return None

    def _extract_client_order_id(self, order: Dict[str, Any]) -> Optional[str]:
        # 支持 algo order 的 clientAlgoId 字段
        cid = order.get("clientAlgoId") or order.get("clientOrderId")
        if cid:
            return str(cid)
        info = order.get("info")
        if isinstance(info, dict):
            cid = info.get("clientAlgoId") or info.get("clientOrderId")
            if cid:
                return str(cid)
        return None

    def _extract_position_side(self, order: Dict[str, Any]) -> Optional[PositionSide]:
        info = order.get("info")
        if isinstance(info, dict):
            ps = info.get("positionSide")
            if ps == "LONG":
                return PositionSide.LONG
            if ps == "SHORT":
                return PositionSide.SHORT
        ps2 = order.get("positionSide")
        if ps2 == "LONG":
            return PositionSide.LONG
        if ps2 == "SHORT":
            return PositionSide.SHORT
        return None

    def _extract_stop_price(self, order: Dict[str, Any]) -> Optional[Decimal]:
        # 支持 algo order 的 triggerPrice 字段
        sp = order.get("triggerPrice") or order.get("stopPrice")
        if sp is None:
            info = order.get("info")
            if isinstance(info, dict):
                sp = info.get("triggerPrice") or info.get("stopPrice")
        if sp is None:
            return None
        try:
            value = Decimal(str(sp))
        except Exception:
            return None
        return value if value > Decimal("0") else None

    def _is_close_position_stop(self, order: Dict[str, Any]) -> bool:
        """检查订单是否是 closePosition 止损单（STOP_MARKET + closePosition=true）"""
        info = order.get("info")
        if not isinstance(info, dict):
            return False
        # 检查 closePosition 字段
        close_pos = info.get("closePosition")
        if close_pos in (True, "true", "TRUE"):
            # 再确认是止损类型
            order_type = info.get("type", "").upper()
            if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                return True
        return False

    async def on_order_update(self, update: OrderUpdate) -> None:
        """处理订单更新：当保护止损成交/撤销后，清理本地状态并触发一次同步。"""
        for side in (PositionSide.LONG, PositionSide.SHORT):
            key = (update.symbol, side)
            state = self._states.get(key)
            if not state or not update.client_order_id:
                continue
            # 使用前缀匹配（因为 clientOrderId 现在包含时间戳后缀）
            if not self._match_client_order_id(update.client_order_id, update.symbol, side):
                continue
            if update.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                self._states.pop(key, None)
                log_event(
                    "protective_stop",
                    event_cn="保护止损",
                    symbol=update.symbol,
                    side=side.value,
                    reason=f"order_update={update.status.value}",
                    order_id=update.order_id,
                )

    async def sync_symbol(
        self,
        *,
        symbol: str,
        rules: SymbolRules,
        positions: Dict[PositionSide, Position],
        enabled: bool,
        dist_to_liq: Decimal,
    ) -> None:
        """同步某个 symbol 的保护止损（会访问交易所 openOrders 和 openAlgoOrders）。"""
        async with self._get_lock(symbol):
            try:
                # 查询普通挂单和 algo 挂单（条件订单在 2025-12-09 后迁移到 Algo Service）
                open_orders = await self._exchange.fetch_open_orders(symbol)
                algo_orders = await self._exchange.fetch_open_algo_orders(symbol)
                # 合并所有订单
                all_orders = list(open_orders) + list(algo_orders)
            except Exception as e:
                log_error(f"保护止损同步失败（获取挂单）: {e}", symbol=symbol)
                return

            # 分类订单：我们自己的（前缀匹配）vs 外部的 closePosition 止损单
            orders_by_side: Dict[PositionSide, list[Dict[str, Any]]] = {PositionSide.LONG: [], PositionSide.SHORT: []}
            external_stops_by_side: Dict[PositionSide, bool] = {PositionSide.LONG: False, PositionSide.SHORT: False}

            for order in all_orders:
                if not isinstance(order, dict):
                    continue
                ps = self._extract_position_side(order)
                if ps is None:
                    continue

                cid = self._extract_client_order_id(order)
                if cid and self._match_client_order_id(cid, symbol, ps):
                    # 我们自己的订单
                    orders_by_side[ps].append(order)
                elif self._is_close_position_stop(order):
                    # 外部的 closePosition 止损单
                    external_stops_by_side[ps] = True

            for side in (PositionSide.LONG, PositionSide.SHORT):
                await self._sync_side(
                    symbol=symbol,
                    side=side,
                    rules=rules,
                    position=positions.get(side),
                    enabled=enabled,
                    dist_to_liq=dist_to_liq,
                    existing_orders=orders_by_side.get(side) or [],
                    has_external_stop=external_stops_by_side.get(side, False),
                )

    async def _sync_side(
        self,
        *,
        symbol: str,
        side: PositionSide,
        rules: SymbolRules,
        position: Optional[Position],
        enabled: bool,
        dist_to_liq: Decimal,
        existing_orders: Sequence[Dict[str, Any]],
        has_external_stop: bool = False,
    ) -> None:
        desired_cid = self.build_client_order_id(symbol, side)

        # 多余的重复单先撤掉（理论上不应出现）
        keep_order: Optional[Dict[str, Any]] = None
        for order in existing_orders:
            if keep_order is None:
                keep_order = order
                continue
            order_id = self._extract_order_id(order)
            if order_id:
                try:
                    await self._exchange.cancel_order(symbol, order_id)
                except Exception as e:
                    log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)

        has_position = position is not None and abs(position.position_amt) > Decimal("0")

        # 未启用或无仓位：确保无保护止损单
        if (not enabled) or (not has_position):
            if keep_order is not None:
                order_id = self._extract_order_id(keep_order)
                if order_id:
                    try:
                        await self._exchange.cancel_order(symbol, order_id)
                        log_event(
                            "protective_stop",
                            event_cn="保护止损",
                            symbol=symbol,
                            side=side.value,
                            reason="cancel_no_position" if not has_position else "cancel_disabled",
                            order_id=order_id,
                        )
                    except Exception as e:
                        log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)
            self._states.pop((symbol, side), None)
            return

        if position is None:
            return

        # 已有外部 closePosition 止损单：跳过，不重复下单
        if has_external_stop and keep_order is None:
            log_event(
                "protective_stop",
                event_cn="保护止损",
                symbol=symbol,
                side=side.value,
                reason="skip_external_stop",
            )
            return

        liquidation_price = position.liquidation_price
        if liquidation_price is None or liquidation_price <= Decimal("0"):
            log_event(
                "protective_stop",
                event_cn="保护止损",
                symbol=symbol,
                side=side.value,
                reason="skip_missing_liquidation_price",
            )
            return

        try:
            desired_stop_price = self.compute_stop_price(
                position_side=side,
                liquidation_price=liquidation_price,
                dist_to_liq=dist_to_liq,
                tick_size=rules.tick_size,
            )
        except Exception as e:
            log_error(f"保护止损 stopPrice 计算失败: {e}", symbol=symbol, side=side.value)
            return

        existing_stop_price = self._extract_stop_price(keep_order) if keep_order is not None else None
        existing_order_id = self._extract_order_id(keep_order) if keep_order is not None else None
        existing_cid = self._extract_client_order_id(keep_order) if keep_order is not None else None

        # stopPrice 相同：更新本地缓存即可
        if keep_order is not None and existing_stop_price is not None and existing_stop_price == desired_stop_price:
            self._states[(symbol, side)] = ProtectiveStopState(
                symbol=symbol,
                position_side=side,
                client_order_id=existing_cid or desired_cid,  # 使用现有订单的实际 cid
                order_id=existing_order_id,
                stop_price=existing_stop_price,
            )
            return

        # stopPrice 不同：撤旧建新（尽量保持系统端始终有单）
        if existing_order_id:
            try:
                await self._exchange.cancel_order(symbol, existing_order_id)
            except Exception as e:
                log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=existing_order_id)
                # 撤单失败：不继续建新，避免重复
                return

        order_side = OrderSide.SELL if side == PositionSide.LONG else OrderSide.BUY
        intent = OrderIntent(
            symbol=symbol,
            side=order_side,
            position_side=side,
            qty=Decimal("0"),
            order_type=OrderType.STOP_MARKET,
            stop_price=desired_stop_price,
            close_position=True,
            reduce_only=True,
            client_order_id=desired_cid,
            is_risk=True,
        )

        result = await self._exchange.place_order(intent)
        if not result.success or not result.order_id:
            log_error(
                f"保护止损下单失败: {result.error_message}",
                symbol=symbol,
                side=side.value,
            )
            return

        self._states[(symbol, side)] = ProtectiveStopState(
            symbol=symbol,
            position_side=side,
            client_order_id=desired_cid,
            order_id=result.order_id,
            stop_price=desired_stop_price,
        )

        log_event(
            "protective_stop",
            event_cn="保护止损",
            symbol=symbol,
            side=side.value,
            reason="place_or_update",
            order_id=result.order_id,
            price=desired_stop_price,
        )
