"""
交易所适配器模块（ccxt）

职责：
- 封装 ccxt REST API 调用
- 拉取 markets 规则（tickSize, stepSize, minQty, minNotional）
- 读取 Hedge 模式 LONG/SHORT 仓位
- 下单（LIMIT，positionSide）
- 撤单

输入：
- 配置（API 密钥、symbol 列表）
- OrderIntent

输出：
- SymbolRules
- Position
- OrderResult
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any, cast

import ccxt.async_support as ccxt

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
from src.utils import round_to_tick, round_to_step, round_up_to_step
from src.utils.logger import get_logger, log_error


class ExchangeAdapter:
    """交易所适配器（ccxt 封装）"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, proxy: Optional[str] = None):
        """
        初始化交易所适配器

        Args:
            api_key: API Key（从环境变量读取）
            api_secret: API Secret（从环境变量读取）
            testnet: 是否使用测试网
            proxy: HTTP 代理地址，如 "http://127.0.0.1:7890"
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.proxy = proxy

        # ccxt 交易所实例
        self._exchange: Optional[ccxt.binanceusdm] = None

        # 缓存的交易规则
        self._rules: Dict[str, SymbolRules] = {}

        # 是否已初始化
        self._initialized = False

    @staticmethod
    def _safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
        """安全转换为 Decimal（None/异常返回默认值）"""
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    @staticmethod
    def _safe_int(value: Any, default: int = 1) -> int:
        """安全转换为 int（None/异常返回默认值）"""
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

    @property
    def exchange(self) -> ccxt.binanceusdm:
        """获取交易所实例（确保已初始化）"""
        if not self._initialized or self._exchange is None:
            raise RuntimeError("ExchangeAdapter 未初始化，请先调用 initialize()")
        return self._exchange

    async def initialize(self) -> None:
        """
        初始化交易所连接

        - 创建 ccxt 实例
        - 加载 markets
        - 设置 Hedge 模式
        """
        if self._initialized:
            return

        logger = get_logger()

        # 创建 ccxt 实例
        exchange_config: Dict[str, Any] = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "hedgeMode": True,  # Hedge 模式
            },
        }

        # 配置代理
        if self.proxy:
            exchange_config["aiohttp_proxy"] = self.proxy
            logger.info(f"使用代理: {self.proxy}")

        self._exchange = ccxt.binanceusdm(exchange_config)  # type: ignore[arg-type]

        # 使用测试网
        if self.testnet:
            self._exchange.set_sandbox_mode(True)
            logger.info("使用 Binance 测试网")

        # 加载 markets
        await self._exchange.load_markets()
        markets = self._exchange.markets
        logger.info(f"加载 markets 完成，共 {len(markets) if markets else 0} 个交易对")

        self._initialized = True

    async def close(self) -> None:
        """关闭交易所连接"""
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
            self._initialized = False

    def _ensure_initialized(self) -> None:
        """确保已初始化"""
        if not self._initialized or not self._exchange:
            raise RuntimeError("ExchangeAdapter 未初始化，请先调用 initialize()")

    async def load_markets(self) -> Dict[str, SymbolRules]:
        """
        加载交易规则

        Returns:
            symbol -> SymbolRules 映射
        """
        self._ensure_initialized()
        logger = get_logger()

        # 刷新 markets
        await self.exchange.load_markets(reload=True)

        self._rules.clear()

        markets = self.exchange.markets or {}
        for symbol, market in markets.items():
            # 只处理 USDT 本位永续
            if not market.get("linear") or not market.get("swap"):
                continue

            try:
                rules = self._extract_rules(symbol, market)
                self._rules[symbol] = rules
            except Exception as e:
                logger.warning(f"提取 {symbol} 规则失败: {e}")

        logger.info(f"提取交易规则完成，共 {len(self._rules)} 个 USDT 本位永续")
        return self._rules

    def _extract_rules(self, symbol: str, market: dict) -> SymbolRules:
        """
        从 market 数据提取交易规则

        Args:
            symbol: 交易对符号
            market: ccxt market 数据

        Returns:
            SymbolRules
        """
        # 精度信息
        precision = market.get("precision", {})
        limits = market.get("limits", {})

        # tick_size (价格精度)
        tick_size = Decimal(str(precision.get("price", "0.01")))

        # step_size (数量精度)
        step_size = Decimal(str(precision.get("amount", "0.001")))

        # min_qty (最小数量)
        amount_limits = limits.get("amount", {})
        min_qty = Decimal(str(amount_limits.get("min", "0.001")))

        # min_notional (最小名义价值)
        # ccxt 可能在 limits.cost.min 或 info 中
        cost_limits = limits.get("cost", {})
        min_notional = Decimal(str(cost_limits.get("min", "5")))

        # 如果 ccxt 没有提供，尝试从 info 中获取
        info = market.get("info", {})
        if min_notional == Decimal("5"):
            # Binance filters 中查找 MIN_NOTIONAL
            filters = info.get("filters", [])
            for f in filters:
                if f.get("filterType") == "MIN_NOTIONAL":
                    min_notional = Decimal(str(f.get("notional", "5")))
                    break

        return SymbolRules(
            symbol=symbol,
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
        )

    def get_rules(self, symbol: str) -> Optional[SymbolRules]:
        """
        获取指定 symbol 的交易规则

        Args:
            symbol: 交易对符号

        Returns:
            SymbolRules 或 None
        """
        return self._rules.get(symbol)

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        获取当前持仓（Hedge 模式）

        Args:
            symbol: 可选，指定 symbol；None 表示获取所有

        Returns:
            Position 列表（LONG 和 SHORT 分开）
        """
        self._ensure_initialized()
        logger = get_logger()

        try:
            # 获取仓位
            if symbol:
                positions = await self.exchange.fetch_positions([symbol])
            else:
                positions = await self.exchange.fetch_positions()

            result = []
            for pos in positions:
                # 跳过空仓位
                contracts = self._safe_decimal(pos.get("contracts", 0))
                if contracts == 0:
                    continue

                # 解析方向
                side_str = pos.get("side", "")
                if side_str == "long":
                    position_side = PositionSide.LONG
                    position_amt = contracts
                elif side_str == "short":
                    position_side = PositionSide.SHORT
                    position_amt = -contracts  # SHORT 为负数
                else:
                    continue

                position = Position(
                    symbol=str(pos.get("symbol", "")),
                    position_side=position_side,
                    position_amt=position_amt,
                    entry_price=self._safe_decimal(pos.get("entryPrice", 0)),
                    unrealized_pnl=self._safe_decimal(pos.get("unrealizedPnl", 0)),
                    leverage=self._safe_int(pos.get("leverage", 1), default=1),
                    liquidation_price=(
                        self._safe_decimal(pos.get("liquidationPrice"))
                        if self._safe_decimal(pos.get("liquidationPrice")) != 0
                        else None
                    ),
                    mark_price=(
                        self._safe_decimal(pos.get("markPrice"))
                        if self._safe_decimal(pos.get("markPrice")) != 0
                        else None
                    ),
                )
                result.append(position)

            logger.debug(f"获取仓位完成，共 {len(result)} 个有效仓位")
            return result

        except Exception as e:
            log_error(f"获取仓位失败: {e}", symbol=symbol)
            raise

    async def place_order(self, intent: OrderIntent) -> OrderResult:
        """
        下单

        Args:
            intent: 下单意图

        Returns:
            OrderResult
        """
        self._ensure_initialized()
        logger = get_logger()

        try:
            # 构建参数
            # Binance USDT 永续 Hedge 模式下，传 positionSide 时 reduceOnly 会被交易所拒绝
            # （报错：Parameter 'reduceonly' sent when not required.），因此这里不下发 reduceOnly。
            # Reduce-only 语义由 positionSide + side + qty<=position 来保证。
            params: Dict[str, Any] = {"positionSide": intent.position_side.value}

            # 归属标记：用于只撤销本程序挂单（避免误撤手动订单）
            if intent.client_order_id:
                params["newClientOrderId"] = intent.client_order_id

            amount: float | None
            price: float | None

            if intent.order_type == OrderType.LIMIT:
                # 时间限制
                params["timeInForce"] = intent.time_in_force.value
                amount = float(intent.qty)
                price = float(intent.price) if intent.price else None
            elif intent.order_type == OrderType.STOP_MARKET:
                # 保护性止损：交易所端条件单，使用 markPrice 触发
                if intent.stop_price is None:
                    raise ValueError("STOP_MARKET requires stop_price")
                params["stopPrice"] = float(intent.stop_price)
                params["workingType"] = "MARK_PRICE"
                if intent.close_position:
                    params["closePosition"] = True
                    amount = None
                else:
                    amount = float(intent.qty)
                price = None
            else:
                amount = float(intent.qty)
                price = float(intent.price) if intent.price else None

            # 下单
            order = await self.exchange.create_order(
                symbol=intent.symbol,
                type=intent.order_type.value.lower(),  # type: ignore
                side=intent.side.value.lower(),  # type: ignore
                amount=amount,  # type: ignore[arg-type]
                price=price,
                params=params,
            )

            # 解析结果
            status = self._parse_order_status(str(order.get("status", "")))

            result = OrderResult(
                success=True,
                order_id=str(order.get("id", "")),
                client_order_id=order.get("clientOrderId"),
                status=status,
                filled_qty=Decimal(str(order.get("filled", 0))),
                avg_price=Decimal(str(order.get("average", 0))) if order.get("average") else Decimal("0"),
            )

            logger.debug(
                f"下单成功: {intent.symbol} {intent.side.value} {intent.qty} @ {intent.price}, "
                f"order_id={result.order_id}"
            )
            return result

        except ccxt.InsufficientFunds as e:
            log_error(f"余额不足: {e}", symbol=intent.symbol)
            return OrderResult(
                success=False,
                order_id=None,
                status=OrderStatus.REJECTED,
                error_message=f"余额不足: {e}",
            )
        except ccxt.InvalidOrder as e:
            log_error(f"无效订单: {e}", symbol=intent.symbol)
            return OrderResult(
                success=False,
                order_id=None,
                status=OrderStatus.REJECTED,
                error_message=f"无效订单: {e}",
            )
        except Exception as e:
            log_error(f"下单失败: {e}", symbol=intent.symbol)
            return OrderResult(
                success=False,
                order_id=None,
                status=OrderStatus.REJECTED,
                error_message=str(e),
            )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        """
        撤单（自动尝试普通订单和 algo 订单）

        Args:
            symbol: 交易对
            order_id: 订单 ID

        Returns:
            OrderResult
        """
        self._ensure_initialized()
        logger = get_logger()

        # 先尝试撤普通订单
        try:
            order = await self.exchange.cancel_order(order_id, symbol)

            status = self._parse_order_status(str(order.get("status", "")))

            result = OrderResult(
                success=True,
                order_id=str(order.get("id", "")),
                status=status,
                filled_qty=Decimal(str(order.get("filled", 0))),
                avg_price=Decimal(str(order.get("average", 0))) if order.get("average") else Decimal("0"),
            )

            logger.debug(f"撤单成功: {symbol} order_id={order_id}")
            return result

        except ccxt.OrderNotFound:
            # 普通订单不存在，尝试撤 algo 订单
            return await self.cancel_algo_order(symbol, order_id)
        except Exception as e:
            # 其他错误，也尝试撤 algo 订单
            logger.debug(f"撤普通订单失败: {e}，尝试撤 algo 订单")
            return await self.cancel_algo_order(symbol, order_id)

    async def cancel_algo_order(self, symbol: str, algo_id: str) -> OrderResult:
        """
        撤销 algo 订单（条件订单）

        Args:
            symbol: 交易对
            algo_id: Algo 订单 ID

        Returns:
            OrderResult
        """
        self._ensure_initialized()
        logger = get_logger()

        try:
            params = {
                "symbol": symbol.replace("/", "").replace(":USDT", ""),
                "algoId": algo_id,
            }
            response = await self.exchange.fapiPrivateDeleteAlgoOrder(params)

            result = OrderResult(
                success=True,
                order_id=algo_id,
                status=OrderStatus.CANCELED,
            )

            logger.debug(f"撤 algo 订单成功: {symbol} algo_id={algo_id}")
            return result

        except Exception as e:
            log_error(f"撤 algo 订单失败: {e}", symbol=symbol, order_id=algo_id)
            return OrderResult(
                success=False,
                order_id=algo_id,
                status=None,
                error_message=str(e),
            )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderResult]:
        """
        撤销所有挂单

        Args:
            symbol: 可选，指定 symbol；None 表示撤销所有

        Returns:
            OrderResult 列表
        """
        self._ensure_initialized()
        logger = get_logger()

        results = []

        try:
            if symbol:
                # 撤销指定 symbol 的所有挂单
                orders = await self.exchange.cancel_all_orders(symbol)
                for order in orders:
                    order_dict = cast(Dict[str, Any], order)
                    results.append(OrderResult(
                        success=True,
                        order_id=str(order_dict.get("id", "")),
                        status=OrderStatus.CANCELED,
                    ))
            else:
                # 获取所有挂单然后逐个撤销
                open_orders = await self.exchange.fetch_open_orders()
                for order in open_orders:
                    order_dict = cast(Dict[str, Any], order)
                    try:
                        result = await self.cancel_order(order_dict["symbol"], str(order_dict["id"]))
                        results.append(result)
                    except Exception as e:
                        results.append(OrderResult(
                            success=False,
                            order_id=str(order_dict.get("id", "")),
                            status=None,
                            error_message=str(e),
                        ))

            logger.info(f"批量撤单完成，共 {len(results)} 个订单")
            return results

        except Exception as e:
            log_error(f"批量撤单失败: {e}", symbol=symbol)
            raise

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前挂单

        Args:
            symbol: 可选，指定 symbol

        Returns:
            挂单列表
        """
        self._ensure_initialized()

        if symbol:
            orders = await self.exchange.fetch_open_orders(symbol)
        else:
            orders = await self.exchange.fetch_open_orders()

        # 转换为 dict 列表
        return [cast(Dict[str, Any], order) for order in orders]

    async def fetch_open_algo_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前 algo 挂单（条件订单：STOP_MARKET, TAKE_PROFIT_MARKET 等）

        Args:
            symbol: 可选，指定 symbol

        Returns:
            algo 挂单列表
        """
        self._ensure_initialized()

        try:
            params = {}
            if symbol:
                params["symbol"] = symbol.replace("/", "").replace(":USDT", "")

            # 调用 Binance fapi/v1/openAlgoOrders 接口
            response = await self.exchange.fapiPrivateGetOpenAlgoOrders(params)

            # 响应可能是数组（直接返回订单列表）或字典（包含 data 字段）
            if isinstance(response, list):
                return response
            if isinstance(response, dict):
                # 兼容旧格式或其他可能的响应结构
                return response.get("data", response.get("orders", []))
            return []
        except Exception as e:
            logger = get_logger()
            logger.warning(f"获取 algo 挂单失败: {e}, symbol={symbol}")
            return []

    def _parse_order_status(self, status_str: str) -> OrderStatus:
        """解析订单状态"""
        status_map = {
            "open": OrderStatus.NEW,
            "new": OrderStatus.NEW,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
            "cancelled": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
        }
        return status_map.get(status_str.lower(), OrderStatus.NEW)

    def round_price(self, symbol: str, price: Decimal) -> Decimal:
        """
        按 tickSize 规整价格

        Args:
            symbol: 交易对
            price: 原始价格

        Returns:
            规整后的价格
        """
        rules = self._rules.get(symbol)
        if not rules:
            return price
        return round_to_tick(price, rules.tick_size)

    def round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """
        按 stepSize 规整数量

        Args:
            symbol: 交易对
            qty: 原始数量

        Returns:
            规整后的数量
        """
        rules = self._rules.get(symbol)
        if not rules:
            return qty
        return round_to_step(qty, rules.step_size)

    def ensure_min_notional(self, symbol: str, qty: Decimal, price: Decimal) -> Decimal:
        """
        确保满足 minNotional 要求，必要时增大 qty

        Args:
            symbol: 交易对
            qty: 原始数量
            price: 价格

        Returns:
            调整后的数量
        """
        rules = self._rules.get(symbol)
        if not rules:
            return qty

        notional = qty * price

        if notional >= rules.min_notional:
            return qty

        # 增大 qty 直至满足 minNotional
        min_qty_for_notional = rules.min_notional / price
        adjusted_qty = round_up_to_step(min_qty_for_notional, rules.step_size)

        # 确保不低于 min_qty
        if adjusted_qty < rules.min_qty:
            adjusted_qty = rules.min_qty

        return adjusted_qty

    def is_position_complete(self, symbol: str, position_amt: Decimal) -> bool:
        """
        判断仓位是否已完成（不可再交易）

        条件：规整后为 0 或 abs(position_amt) < minQty

        Args:
            symbol: 交易对
            position_amt: 仓位数量

        Returns:
            True 表示已完成
        """
        rules = self._rules.get(symbol)
        if not rules:
            return abs(position_amt) == 0

        # 按 stepSize 规整
        rounded = round_to_step(abs(position_amt), rules.step_size)

        # 为 0 或小于 minQty
        return rounded == 0 or rounded < rules.min_qty

    def get_tradable_qty(self, symbol: str, position_amt: Decimal) -> Decimal:
        """
        获取可交易数量

        Args:
            symbol: 交易对
            position_amt: 仓位数量

        Returns:
            可交易数量（规整后，且不小于 minQty）
        """
        rules = self._rules.get(symbol)
        abs_amt = abs(position_amt)

        if not rules:
            return abs_amt

        # 按 stepSize 规整
        rounded = round_to_step(abs_amt, rules.step_size)

        # 小于 minQty 则返回 0
        if rounded < rules.min_qty:
            return Decimal("0")

        return rounded
