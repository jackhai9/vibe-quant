# Input: positions, rules, exchange adapter, external stop orders
# Output: protective stop orders, takeover decisions, and state
# Pos: protective stop manager
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

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
    AlgoOrderUpdate,
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
        risk_levels: Optional[Dict[str, int]] = None,
    ):
        self._exchange = exchange
        self._client_order_id_prefix = client_order_id_prefix
        self._risk_stage = "protective_stop"
        self._risk_levels = dict(risk_levels or {})
        self._states: Dict[tuple[str, PositionSide], ProtectiveStopState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._startup_existing_logged: set[tuple[str, PositionSide]] = set()
        self._startup_existing_external_logged: set[tuple[str, PositionSide]] = set()
        self._external_multi_sig: Dict[tuple[str, PositionSide], tuple[str, ...]] = {}

    def _get_risk_level(self) -> Optional[int]:
        return self._risk_levels.get(self._risk_stage)

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

    def is_own_algo_order(self, symbol: str, algo_id: str) -> bool:
        """检查 algo_id 是否匹配当前已记录的保护止损单。"""
        if not algo_id:
            return False
        for side in (PositionSide.LONG, PositionSide.SHORT):
            state = self._states.get((symbol, side))
            if state and state.order_id and str(state.order_id) == str(algo_id):
                return True
        return False

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

    def is_stop_price_valid(
        self,
        *,
        position_side: PositionSide,
        stop_price: Decimal,
        liquidation_price: Decimal,
        min_dist_ratio: Decimal = Decimal("0.0001"),  # 0.01%
    ) -> bool:
        """
        检查止损价是否有效（能在爆仓前触发）。

        无效条件（含接近爆仓价的情况）：
        - LONG: stop_price <= liquidation_price * (1 + min_dist_ratio)
        - SHORT: stop_price >= liquidation_price * (1 - min_dist_ratio)

        Args:
            position_side: 仓位方向
            stop_price: 止损价
            liquidation_price: 爆仓价
            min_dist_ratio: 最小有效距离比例（默认 0.01%）

        Returns:
            True 如果止损价有效
        """
        if liquidation_price <= Decimal("0") or stop_price <= Decimal("0"):
            return False

        if position_side == PositionSide.LONG:
            # LONG 止损是 SELL stop，价格下跌触发
            # 止损价必须高于爆仓价（这样价格下跌时先触发止损）
            return stop_price > liquidation_price * (Decimal("1") + min_dist_ratio)
        else:
            # SHORT 止损是 BUY stop，价格上涨触发
            # 止损价必须低于爆仓价（这样价格上涨时先触发止损）
            return stop_price < liquidation_price * (Decimal("1") - min_dist_ratio)

    def _extract_order_id(self, order: Dict[str, Any]) -> Optional[str]:
        """提取订单 ID（支持 algo order 的 algoId 和普通订单的 id）"""
        oid = order.get("algoId") or order.get("orderId") or order.get("id")
        if oid:
            return str(oid)
        info = order.get("info")
        if isinstance(info, dict):
            oid = info.get("algoId") or info.get("orderId") or info.get("id")
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

    def _extract_order_type(self, order: Dict[str, Any]) -> Optional[str]:
        order_type_candidates = (
            order.get("orderType"),
            order.get("type"),
            order.get("algoType"),
        )
        order_type = next((x for x in order_type_candidates if isinstance(x, str) and x.strip()), None)
        if order_type is None:
            info = order.get("info")
            if isinstance(info, dict):
                info_candidates = (
                    info.get("orderType"),
                    info.get("type"),
                    info.get("algoType"),
                )
                order_type = next((x for x in info_candidates if isinstance(x, str) and x.strip()), None)
        if order_type is None:
            return None
        return order_type.strip().upper()

    @staticmethod
    def _coerce_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if value == 1:
                return True
            if value == 0:
                return False
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "y"):
                return True
            if v in ("false", "0", "no", "n"):
                return False
        return None

    def _is_close_position_stop(self, order: Dict[str, Any]) -> bool:
        """检查订单是否是 closePosition 止损单（STOP_MARKET + closePosition=true）"""
        info = order.get("info")
        if not isinstance(info, dict):
            info = {}

        close_pos = self._coerce_bool(order.get("closePosition"))
        if close_pos is None:
            close_pos = self._coerce_bool(info.get("closePosition"))
        if close_pos is not True:
            return False

        order_type = self._extract_order_type({**order, "info": info})
        return order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT")

    def _is_reduce_only_stop(self, order: Dict[str, Any]) -> bool:
        """
        检查订单是否是 reduceOnly 的止损/止盈条件单。

        语义：外部接管（不要求 closePosition=True）。
        """
        info = order.get("info")
        if not isinstance(info, dict):
            info = {}

        reduce_only = self._coerce_bool(order.get("reduceOnly"))
        if reduce_only is None:
            reduce_only = self._coerce_bool(info.get("reduceOnly"))
        if reduce_only is not True:
            return False

        order_type = self._extract_order_type({**order, "info": info})
        if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            return False

        # 必须能确定 positionSide，否则不做外部接管判断（避免误伤）
        return self._extract_position_side({**order, "info": info}) is not None

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
                    "risk",
                    symbol=update.symbol,
                    side=side.value,
                    risk_stage=self._risk_stage,
                    risk_level=self._get_risk_level(),
                    reason=f"order_update={update.status.value}",
                    order_id=update.order_id,
                )

    def on_algo_order_update(self, update: AlgoOrderUpdate) -> None:
        """
        处理 Algo Order 更新（ALGO_UPDATE 事件）。

        当我们的保护止损单状态变化时，清理本地状态。
        注：只处理我们自己的订单（由 main.py 在调用前用前缀过滤）。
        """
        # Algo Order 终态
        terminal_statuses = {"CANCELED", "FILLED", "TRIGGERED", "EXPIRED", "REJECTED", "FINISHED"}
        if update.status.upper() not in terminal_statuses:
            return

        for side in (PositionSide.LONG, PositionSide.SHORT):
            key = (update.symbol, side)
            state = self._states.get(key)
            if not state:
                continue
            # 前缀匹配
            if not self._match_client_order_id(update.client_algo_id, update.symbol, side):
                continue
            self._states.pop(key, None)
            log_event(
                "risk",
                symbol=update.symbol,
                side=side.value,
                risk_stage=self._risk_stage,
                risk_level=self._get_risk_level(),
                reason=f"algo_update={update.status}",
                algo_id=update.algo_id,
            )

    async def sync_symbol(
        self,
        *,
        symbol: str,
        rules: SymbolRules,
        positions: Dict[PositionSide, Position],
        enabled: bool,
        dist_to_liq: Decimal,
        external_stop_latch_by_side: Optional[Dict[PositionSide, bool]] = None,
        sync_reason: Optional[str] = None,
    ) -> Dict[PositionSide, bool]:
        """同步某个 symbol 的保护止损（会访问交易所 openOrders 和 openAlgoOrders）。"""
        async with self._get_lock(symbol):
            try:
                # 保护止损依赖“外部 stop/tp 接管”判断。ccxt 可能漏掉 closePosition 的 STOP/TP（例如 origQty=0），
                # 因此这里以 raw openOrders 为主（若不可用则回退 ccxt fetch_open_orders）。
                if hasattr(self._exchange, "fetch_open_orders_raw"):
                    try:
                        open_orders = await getattr(self._exchange, "fetch_open_orders_raw")(symbol)  # type: ignore[misc]
                    except Exception as e:
                        log_error(f"获取 raw openOrders 失败: {e}", symbol=symbol, reason=sync_reason)
                        open_orders = await self._exchange.fetch_open_orders(symbol)
                else:
                    open_orders = await self._exchange.fetch_open_orders(symbol)

                # 查询 algo 挂单（条件订单在 2025-12-09 后迁移到 Algo Service）
                algo_orders = await self._exchange.fetch_open_algo_orders(symbol)

                # 合并所有订单
                all_orders: list[Dict[str, Any]] = []
                for o in list(open_orders):
                    if isinstance(o, dict):
                        all_orders.append(o)
                for o in list(algo_orders):
                    if isinstance(o, dict):
                        all_orders.append(o)
            except Exception as e:
                log_error(f"保护止损同步失败（获取挂单）: {e}", symbol=symbol)
                return {PositionSide.LONG: False, PositionSide.SHORT: False}

            # 分类订单：我们自己的（前缀匹配）vs 外部的 closePosition 止损单
            orders_by_side: Dict[PositionSide, list[Dict[str, Any]]] = {PositionSide.LONG: [], PositionSide.SHORT: []}
            external_stops_by_side: Dict[PositionSide, bool] = {PositionSide.LONG: False, PositionSide.SHORT: False}
            external_stop_orders_by_side: Dict[PositionSide, list[Dict[str, Any]]] = {
                PositionSide.LONG: [],
                PositionSide.SHORT: [],
            }
            external_latch_by_side = external_stop_latch_by_side or {}
            external_stop_sample_by_side: Dict[PositionSide, Dict[str, Any]] = {}

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
                elif self._is_close_position_stop(order) or self._is_reduce_only_stop(order):
                    # 外部的 closePosition 或 reduceOnly 止损/止盈单
                    external_stops_by_side[ps] = True
                    external_stop_orders_by_side[ps].append(order)
                    external_stop_sample_by_side.setdefault(ps, order)

            # 外部多单告警：同一 symbol+side 出现多张外部 stop/tp（可能来自多端手动设置）
            for side in (PositionSide.LONG, PositionSide.SHORT):
                externals = external_stop_orders_by_side.get(side) or []
                if len(externals) <= 1:
                    continue
                key = (symbol, side)
                ids = tuple(
                    sorted(x for x in (self._extract_order_id(o) for o in externals) if x)
                )
                if ids and self._external_multi_sig.get(key) == ids:
                    continue
                self._external_multi_sig[key] = ids
                log_event(
                    "risk",
                    symbol=symbol,
                    side=side.value,
                    risk_stage=self._risk_stage,
                    risk_level=self._get_risk_level(),
                    reason="external_stop_multiple",
                    count=len(externals),
                    order_ids=ids,
                )

            if sync_reason == "startup":
                for side in (PositionSide.LONG, PositionSide.SHORT):
                    key = (symbol, side)
                    if key in self._startup_existing_logged:
                        continue
                    existing = orders_by_side.get(side) or []
                    if not existing:
                        continue
                    first = existing[0]
                    self._startup_existing_logged.add(key)
                    log_event(
                        "risk",
                        symbol=symbol,
                        side=side.value,
                        risk_stage=self._risk_stage,
                        risk_level=self._get_risk_level(),
                        reason="startup_existing_own_stop",
                        count=len(existing),
                        order_id=self._extract_order_id(first),
                        client_order_id=self._extract_client_order_id(first),
                    )
                for side in (PositionSide.LONG, PositionSide.SHORT):
                    key = (symbol, side)
                    if key in self._startup_existing_external_logged:
                        continue
                    externals = external_stop_orders_by_side.get(side) or []
                    self._startup_existing_external_logged.add(key)
                    if not externals:
                        continue
                    sample = external_stop_sample_by_side.get(side)
                    raw_info = sample.get("info") if isinstance(sample, dict) else None
                    info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
                    working_type = sample.get("workingType") if isinstance(sample, dict) else None
                    if not isinstance(working_type, str):
                        working_type = info.get("workingType")
                    stop_price = self._extract_stop_price(sample) if isinstance(sample, dict) else None
                    log_event(
                        "risk",
                        symbol=symbol,
                        side=side.value,
                        risk_stage=self._risk_stage,
                        risk_level=self._get_risk_level(),
                        reason="startup_existing_external_stop",
                        order_id=self._extract_order_id(sample) if sample else None,
                        client_order_id=self._extract_client_order_id(sample) if sample else None,
                        stop_price=str(stop_price) if stop_price is not None else None,
                        working_type=working_type,
                    )

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
                    external_stop_orders=external_stop_orders_by_side.get(side) or [],
                    external_stop_sample=external_stop_sample_by_side.get(side),
                    has_external_stop_latch=bool(external_latch_by_side.get(side, False)),
                )
            return external_stops_by_side

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
        external_stop_orders: Optional[Sequence[Dict[str, Any]]] = None,
        external_stop_sample: Optional[Dict[str, Any]] = None,
        has_external_stop_latch: bool = False,
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
                    await self._exchange.cancel_algo_order(symbol, order_id)
                except Exception as e:
                    log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)

        has_position = position is not None and abs(position.position_amt) > Decimal("0")

        # 未启用或无仓位：确保无保护止损单
        if (not enabled) or (not has_position):
            if keep_order is not None:
                order_id = self._extract_order_id(keep_order)
                if order_id:
                    try:
                        await self._exchange.cancel_algo_order(symbol, order_id)
                        log_event(
                            "risk",
                            symbol=symbol,
                            side=side.value,
                            risk_stage=self._risk_stage,
                            risk_level=self._get_risk_level(),
                            reason="cancel_no_position" if not has_position else "cancel_disabled",
                            order_id=order_id,
                        )
                    except Exception as e:
                        log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)
            self._states.pop((symbol, side), None)
            return

        if position is None:
            return

        # 已有外部 closePosition 止损/止盈单：检查是否有效
        if has_external_stop:
            liq_price = position.liquidation_price if position else None
            orders = list(external_stop_orders or [])
            if not orders and external_stop_sample is not None:
                orders = [external_stop_sample]

            has_unknown_external = False
            valid_external_orders: list[Dict[str, Any]] = []
            invalid_external_orders: list[Dict[str, Any]] = []

            for order in orders:
                stop_price = self._extract_stop_price(order)
                if stop_price is None or liq_price is None or liq_price <= Decimal("0"):
                    # 无法提取止损价时，保守地认为有效（避免误删）
                    has_unknown_external = True
                    continue
                if self.is_stop_price_valid(
                    position_side=side,
                    stop_price=stop_price,
                    liquidation_price=liq_price,
                ):
                    valid_external_orders.append(order)
                else:
                    invalid_external_orders.append(order)

            has_valid_external = bool(valid_external_orders or has_unknown_external)
            invalid_detected = False

            if invalid_external_orders:
                # 无效的外部止损 → 取消并由程序接管
                for invalid_order in invalid_external_orders:
                    invalid_detected = True
                    external_order_id = self._extract_order_id(invalid_order)
                    external_stop_price = self._extract_stop_price(invalid_order)
                    if not external_order_id:
                        continue
                    try:
                        await self._exchange.cancel_algo_order(symbol, external_order_id)
                        log_event(
                            "risk",
                            symbol=symbol,
                            side=side.value,
                            risk_stage=self._risk_stage,
                            risk_level=self._get_risk_level(),
                            reason="cancel_invalid_external_stop",
                            order_id=external_order_id,
                            external_stop_price=str(external_stop_price) if external_stop_price else None,
                            liquidation_price=str(liq_price) if liq_price else None,
                        )
                    except Exception as e:
                        log_error(f"取消无效外部止损失败: {e}", symbol=symbol, order_id=external_order_id)

            if has_valid_external:
                # 有效的外部止损 → 保持原有"外部接管"逻辑（撤掉我们自己的，停止维护）
                if keep_order is not None:
                    order_id = self._extract_order_id(keep_order)
                    if order_id:
                        try:
                            await self._exchange.cancel_algo_order(symbol, order_id)
                            log_event(
                                "risk",
                                symbol=symbol,
                                side=side.value,
                                risk_stage=self._risk_stage,
                                risk_level=self._get_risk_level(),
                                reason="cancel_own_due_to_external_stop",
                                order_id=order_id,
                            )
                        except Exception as e:
                            log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)
                            return
                self._states.pop((symbol, side), None)
                return
            # 仅无效外部止损：不 return，继续由程序挂新止损
            if invalid_detected:
                has_external_stop_latch = False

        # 外部接管锁存：WS 已见外部 stop/tp（cp=True 或 reduceOnly=True）
        # 锁存期间避免对保护止损做“撤旧建新”，减少外部端刚创建但 REST 尚未可见时的竞态与重复单
        if has_external_stop_latch:
            return

        liquidation_price = position.liquidation_price
        if liquidation_price is None or liquidation_price <= Decimal("0"):
            log_event(
                "risk",
                symbol=symbol,
                side=side.value,
                risk_stage=self._risk_stage,
                risk_level=self._get_risk_level(),
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
        # 注意：交易所/ccxt 可能以 float 返回 triggerPrice，直接 Decimal 精确比较会抖动
        if keep_order is not None and existing_stop_price is not None:
            existing_norm = round_to_tick(existing_stop_price, rules.tick_size)
            desired_norm = round_to_tick(desired_stop_price, rules.tick_size)
        else:
            existing_norm = None
            desired_norm = None

        # 只允许“收紧”止损：禁止把 stopPrice 往“更远/更松”方向移动
        # LONG：stopPrice 越高越早触发（更紧），不允许下调
        # SHORT：stopPrice 越低越早触发（更紧），不允许上调
        if (
            keep_order is not None
            and existing_norm is not None
            and desired_norm is not None
            and (
                (side == PositionSide.LONG and desired_norm < existing_norm)
                or (side == PositionSide.SHORT and desired_norm > existing_norm)
            )
        ):
            self._states[(symbol, side)] = ProtectiveStopState(
                symbol=symbol,
                position_side=side,
                client_order_id=existing_cid or desired_cid,
                order_id=existing_order_id,
                stop_price=existing_norm,
            )
            return

        if keep_order is not None and existing_norm is not None and desired_norm is not None and existing_norm == desired_norm:
            self._states[(symbol, side)] = ProtectiveStopState(
                symbol=symbol,
                position_side=side,
                client_order_id=existing_cid or desired_cid,  # 使用现有订单的实际 cid
                order_id=existing_order_id,
                stop_price=existing_norm,
            )
            return

        # stopPrice 不同：撤旧建新（尽量保持系统端始终有单）
        if existing_order_id:
            try:
                await self._exchange.cancel_algo_order(symbol, existing_order_id)
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
            "risk",
            symbol=symbol,
            side=side.value,
            risk_stage=self._risk_stage,
            risk_level=self._get_risk_level(),
            reason="place_or_update",
            order_id=result.order_id,
            price=desired_stop_price,
        )
