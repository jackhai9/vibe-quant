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
    ):
        self._exchange = exchange
        self._client_order_id_prefix = client_order_id_prefix
        self._states: Dict[tuple[str, PositionSide], ProtectiveStopState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._startup_existing_logged: set[tuple[str, PositionSide]] = set()
        self._startup_existing_external_logged: set[tuple[str, PositionSide]] = set()
        self._external_multi_sig: Dict[tuple[str, PositionSide], tuple[str, ...]] = {}
        self._skip_external_log_state: Dict[tuple[str, PositionSide], tuple[int, Optional[str], str]] = {}

    def _should_log_skip_external(
        self,
        *,
        symbol: str,
        side: PositionSide,
        reason: str,
        external_order_id: Optional[str],
        throttle_ms: int,
        now_ms: int,
    ) -> bool:
        if throttle_ms <= 0:
            return True
        key = (symbol, side)
        prev = self._skip_external_log_state.get(key)
        if prev is None:
            self._skip_external_log_state[key] = (now_ms, external_order_id, reason)
            return True
        prev_ms, prev_oid, prev_reason = prev
        # 外部单切换/原因切换：立刻打印
        if prev_oid != external_order_id or prev_reason != reason:
            self._skip_external_log_state[key] = (now_ms, external_order_id, reason)
            return True
        if now_ms - prev_ms >= throttle_ms:
            self._skip_external_log_state[key] = (now_ms, external_order_id, reason)
            return True
        return False

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

    def _summarize_order_brief(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raw_info = order.get("info")
        info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        cp = self._coerce_bool(order.get("closePosition"))
        if cp is None:
            cp = self._coerce_bool(info.get("closePosition"))
        ro = self._coerce_bool(order.get("reduceOnly"))
        if ro is None:
            ro = self._coerce_bool(info.get("reduceOnly"))
        stop_price = self._extract_stop_price(order)

        wt = order.get("workingType")
        if not isinstance(wt, str):
            wt = info.get("workingType")

        tif = order.get("timeInForce")
        if not isinstance(tif, str):
            tif = info.get("timeInForce")

        ps_obj = self._extract_position_side(order)
        return {
            "source": order.get("_vq_source"),
            "order_id": self._extract_order_id(order),
            "client_id": self._extract_client_order_id(order),
            "type": self._extract_order_type(order),
            "ps": (ps_obj.value if ps_obj else None),
            "cp": cp,
            "reduceOnly": ro,
            "workingType": wt,
            "timeInForce": tif,
            "stop_price": str(stop_price) if stop_price is not None else None,
        }

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
                    "protective_stop",
                    event_cn="保护止损",
                    symbol=update.symbol,
                    side=side.value,
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
                "protective_stop",
                event_cn="保护止损",
                symbol=update.symbol,
                side=side.value,
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
        skip_external_log_throttle_s: float = 2.0,
        sync_reason: Optional[str] = None,
    ) -> Dict[PositionSide, bool]:
        """同步某个 symbol 的保护止损（会访问交易所 openOrders 和 openAlgoOrders）。"""
        async with self._get_lock(symbol):
            try:
                # 查询普通挂单和 algo 挂单（条件订单在 2025-12-09 后迁移到 Algo Service）
                open_orders = await self._exchange.fetch_open_orders(symbol)
                algo_orders = await self._exchange.fetch_open_algo_orders(symbol)
                # 启动时兜底：ccxt 可能漏掉 closePosition 的 STOP/TP（例如 origQty=0），用 raw 接口补一次
                raw_open_orders: list[Dict[str, Any]] = []
                use_raw_open = sync_reason == "startup" or (
                    isinstance(sync_reason, str) and sync_reason.startswith("external_takeover")
                )
                if use_raw_open and hasattr(self._exchange, "fetch_open_orders_raw"):
                    try:
                        raw_open_orders = await getattr(self._exchange, "fetch_open_orders_raw")(symbol)  # type: ignore[misc]
                    except Exception as e:
                        log_error(f"启动外部止损扫描失败（raw openOrders）: {e}", symbol=symbol)
                # 合并所有订单，并标记来源（用于启动排障）
                all_orders: list[Dict[str, Any]] = []
                for o in list(open_orders):
                    if isinstance(o, dict):
                        all_orders.append({**o, "_vq_source": "open"})
                for o in list(raw_open_orders):
                    if isinstance(o, dict):
                        all_orders.append({**o, "_vq_source": "raw_open"})
                for o in list(algo_orders):
                    if isinstance(o, dict):
                        all_orders.append({**o, "_vq_source": "algo"})
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

            # DEBUG: 打印 algo 订单原始数据
            from src.utils.logger import get_logger
            logger = get_logger()
            if algo_orders:
                for order in algo_orders:
                    cid = self._extract_client_order_id(order)
                    ps = self._extract_position_side(order)
                    is_cp = self._is_close_position_stop(order)
                    cp_top = order.get("closePosition") if isinstance(order, dict) else None
                    cp_info = (
                        order.get("info", {}).get("closePosition")
                        if isinstance(order, dict) and isinstance(order.get("info"), dict)
                        else None
                    )
                    ot_top = order.get("orderType") if isinstance(order, dict) else None
                    ot_info = (
                        order.get("info", {}).get("orderType")
                        if isinstance(order, dict) and isinstance(order.get("info"), dict)
                        else None
                    )
                    prefix_l = self._build_client_order_id_prefix(symbol, PositionSide.LONG)
                    prefix_s = self._build_client_order_id_prefix(symbol, PositionSide.SHORT)
                    logger.info(
                        f"[DEBUG] algo_order: cid={cid}, ps={ps}, is_closePosition={is_cp}, "
                        f"closePosition(top/info)={cp_top}/{cp_info}, orderType(top/info)={ot_top}/{ot_info}, "
                        f"prefix_L={prefix_l}, prefix_S={prefix_s}, "
                        f"match_L={cid and cid.startswith(prefix_l)}, match_S={cid and cid.startswith(prefix_s)}, "
                        f"raw_keys={list(order.keys()) if isinstance(order, dict) else 'not_dict'}"
                    )

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
                brief = []
                for o in externals[:5]:
                    raw_info = o.get("info")
                    info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
                    brief.append(
                        {
                            "order_id": self._extract_order_id(o),
                            "client_id": self._extract_client_order_id(o),
                            "type": self._extract_order_type(o),
                            "cp": self._coerce_bool(o.get("closePosition")) or self._coerce_bool(info.get("closePosition")),
                            "reduceOnly": self._coerce_bool(o.get("reduceOnly")) or self._coerce_bool(info.get("reduceOnly")),
                            "stop_price": str(self._extract_stop_price(o)) if self._extract_stop_price(o) else None,
                        }
                    )
                log_event(
                    "protective_stop",
                    event_cn="保护止损",
                    symbol=symbol,
                    side=side.value,
                    reason="external_stop_multiple",
                    count=len(externals),
                    sample=brief,
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
                        "protective_stop",
                        event_cn="保护止损",
                        symbol=symbol,
                        side=side.value,
                        reason="startup_existing_own_stop",
                        count=len(existing),
                        order_id=self._extract_order_id(first),
                        client_order_id=self._extract_client_order_id(first),
                    )
                for side in (PositionSide.LONG, PositionSide.SHORT):
                    key = (symbol, side)
                    if key in self._startup_existing_external_logged:
                        continue
                    # 启动排障：无论是否存在外部单，都输出一次外部 stop/tp 摘要（有仓位时）
                    pos = positions.get(side)
                    has_pos = pos is not None and abs(pos.position_amt) > Decimal("0")
                    if not has_pos:
                        continue
                    externals = external_stop_orders_by_side.get(side) or []
                    sample = external_stop_sample_by_side.get(side)
                    self._startup_existing_external_logged.add(key)
                    brief = [self._summarize_order_brief(o) for o in externals[:5]]
                    log_event(
                        "protective_stop",
                        event_cn="保护止损",
                        symbol=symbol,
                        side=side.value,
                        reason="startup_external_stop_snapshot",
                        count=len(externals),
                        sample=brief,
                        order_id=self._extract_order_id(sample) if sample else None,
                        client_order_id=self._extract_client_order_id(sample) if sample else None,
                    )
                    if externals:
                        log_event(
                            "protective_stop",
                            event_cn="保护止损",
                            symbol=symbol,
                            side=side.value,
                            reason="startup_existing_external_stop",
                            count=len(externals),
                            order_id=self._extract_order_id(sample) if sample else None,
                            client_order_id=self._extract_client_order_id(sample) if sample else None,
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
                    external_stop_sample=external_stop_sample_by_side.get(side),
                    has_external_stop_latch=bool(external_latch_by_side.get(side, False)),
                    skip_external_log_throttle_ms=max(0, int(skip_external_log_throttle_s * 1000)),
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
        external_stop_sample: Optional[Dict[str, Any]] = None,
        has_external_stop_latch: bool = False,
        skip_external_log_throttle_ms: int = 0,
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

        # 已有外部 closePosition 止损/止盈单：外部接管（撤掉我们自己的，且停止维护）
        if has_external_stop:
            external_order_id = self._extract_order_id(external_stop_sample) if external_stop_sample else None
            external_client_order_id = (
                self._extract_client_order_id(external_stop_sample) if external_stop_sample else None
            )
            external_stop_price = self._extract_stop_price(external_stop_sample) if external_stop_sample else None
            external_order_type = (
                self._extract_order_type(external_stop_sample) if isinstance(external_stop_sample, dict) else None
            )

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
                            reason="cancel_own_due_to_external_stop",
                            order_id=order_id,
                        )
                    except Exception as e:
                        log_error(f"保护止损撤单失败: {e}", symbol=symbol, order_id=order_id)
                        return
            self._states.pop((symbol, side), None)
            now_ms = int(time.time() * 1000)
            if self._should_log_skip_external(
                symbol=symbol,
                side=side,
                reason="skip_external_stop",
                external_order_id=external_order_id,
                throttle_ms=skip_external_log_throttle_ms,
                now_ms=now_ms,
            ):
                log_event(
                    "protective_stop",
                    event_cn="保护止损",
                    symbol=symbol,
                    side=side.value,
                    reason="skip_external_stop",
                    external_order_id=external_order_id,
                    external_client_order_id=external_client_order_id,
                    external_order_type=external_order_type,
                    external_stop_price=str(external_stop_price) if external_stop_price is not None else None,
                )
            return

        # 外部接管锁存：WS 已见外部 stop/tp（cp=True 或 reduceOnly=True）
        # 锁存期间避免对保护止损做“撤旧建新”，减少外部端刚创建但 REST 尚未可见时的竞态与重复单
        if has_external_stop_latch:
            now_ms = int(time.time() * 1000)
            if keep_order is None:
                if self._should_log_skip_external(
                    symbol=symbol,
                    side=side,
                    reason="skip_external_stop_latch",
                    external_order_id=None,
                    throttle_ms=skip_external_log_throttle_ms,
                    now_ms=now_ms,
                ):
                    log_event(
                        "protective_stop",
                        event_cn="保护止损",
                        symbol=symbol,
                        side=side.value,
                        reason="skip_external_stop_latch",
                    )
            else:
                if self._should_log_skip_external(
                    symbol=symbol,
                    side=side,
                    reason="skip_external_stop_latch_keep",
                    external_order_id=self._extract_order_id(keep_order),
                    throttle_ms=skip_external_log_throttle_ms,
                    now_ms=now_ms,
                ):
                    log_event(
                        "protective_stop",
                        event_cn="保护止损",
                        symbol=symbol,
                        side=side.value,
                        reason="skip_external_stop_latch_keep",
                        order_id=self._extract_order_id(keep_order),
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
