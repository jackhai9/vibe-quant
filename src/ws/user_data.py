# Input: API keys, listenKey, callbacks, reconnect state
# Output: order/position/leverage updates (including maker role and realized pnl)
# Pos: user data WS client (account stream)
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
User Data Stream WebSocket 模块

职责：
- 获取并维护 listenKey
- 连接 User Data Stream
- 监听 ORDER_TRADE_UPDATE / ALGO_UPDATE / ACCOUNT_UPDATE / ACCOUNT_CONFIG_UPDATE 事件
- listenKey 每 30 分钟续期
- 断线自动重连（指数退避）

输入：
- 配置（API 密钥）

输出：
- OrderUpdate（通过回调）
- PositionUpdate（通过回调）
- LeverageUpdate（通过回调）
"""

import asyncio
import json
from decimal import Decimal
from typing import Callable, Dict, Optional, Any, List

import aiohttp
import websockets
from websockets import ClientConnection

from src.models import (
    AlgoOrderUpdate,
    OrderUpdate,
    OrderSide,
    PositionSide,
    OrderStatus,
    PositionUpdate,
    LeverageUpdate,
)
from src.utils.logger import (
    get_logger,
    log_ws_connect,
    log_ws_disconnect,
    log_ws_reconnect,
    log_error,
)
from src.utils.helpers import current_time_ms


# Binance Futures REST API 基础 URL
REST_BASE_URL = "https://fapi.binance.com"
REST_TESTNET_URL = "https://testnet.binancefuture.com"

# Binance Futures WebSocket 基础 URL
WS_BASE_URL = "wss://fstream.binance.com"
WS_TESTNET_URL = "wss://stream.binancefuture.com"

# listenKey 续期间隔（30 分钟）
KEEPALIVE_INTERVAL_MS = 30 * 60 * 1000

# HTTP 默认超时（避免 shutdown 时卡住导致资源未释放）
HTTP_TIMEOUT_S = 10.0
WS_CLOSE_TIMEOUT_S = 1.0
SESSION_CLOSE_TIMEOUT_S = 1.0
LISTEN_KEY_CLOSE_TIMEOUT_S = 1.5
TASK_CANCEL_TIMEOUT_S = 1.0


class UserDataWSClient:
    """User Data Stream WebSocket 客户端"""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        on_order_update: Callable[[OrderUpdate], None],
        on_algo_order_update: Optional[Callable[[AlgoOrderUpdate], None]] = None,
        on_position_update: Optional[Callable[[PositionUpdate], None]] = None,
        on_leverage_update: Optional[Callable[[LeverageUpdate], None]] = None,
        on_reconnect: Optional[Callable[[str], None]] = None,
        testnet: bool = False,
        proxy: Optional[str] = None,
        initial_delay_ms: int = 1000,
        max_delay_ms: int = 30000,
        multiplier: int = 2,
    ):
        """
        初始化 User Data Stream 客户端

        Args:
            api_key: API Key
            api_secret: API Secret
            on_order_update: 收到订单更新时的回调
            on_algo_order_update: 收到 Algo 条件单更新时的回调（ALGO_UPDATE）
            on_position_update: 收到仓位更新时的回调（ACCOUNT_UPDATE）
            on_leverage_update: 收到杠杆更新时的回调（ACCOUNT_CONFIG_UPDATE）
            on_reconnect: WS 重连成功回调（用于触发上层 REST 校准）
            testnet: 是否使用测试网
            proxy: HTTP 代理地址，如 "http://127.0.0.1:7890"
            initial_delay_ms: 重连初始延迟
            max_delay_ms: 重连最大延迟
            multiplier: 重连延迟倍数
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.on_order_update = on_order_update
        self.on_algo_order_update = on_algo_order_update
        self.on_position_update = on_position_update
        self.on_leverage_update = on_leverage_update
        self.on_reconnect = on_reconnect
        self.testnet = testnet
        self.proxy = proxy
        self.initial_delay_ms = initial_delay_ms
        self.max_delay_ms = max_delay_ms
        self.multiplier = multiplier

        # listenKey
        self._listen_key: Optional[str] = None

        # WebSocket 连接
        self._ws: Optional[ClientConnection] = None
        self._running = False

        # 续期任务
        self._keepalive_task: Optional[asyncio.Task[None]] = None

        # 重连相关
        self._current_delay_ms = initial_delay_ms
        self._reconnect_count = 0

        # HTTP session
        self._session: Optional[aiohttp.ClientSession] = None
        self._disconnect_lock = asyncio.Lock()

    def _get_rest_url(self) -> str:
        """获取 REST API URL"""
        return REST_TESTNET_URL if self.testnet else REST_BASE_URL

    def _get_ws_url(self) -> str:
        """获取 WebSocket URL"""
        return WS_TESTNET_URL if self.testnet else WS_BASE_URL

    async def connect(self) -> None:
        """
        获取 listenKey 并建立 WS 连接
        """
        # 修复：只有已连接时才跳过，而非 _running=True 时跳过
        # 否则 _reconnect() 调用 connect() 时会直接返回，导致重连失败
        if self.is_connected:
            return

        self._running = True
        logger = get_logger()

        try:
            was_reconnect = self._reconnect_count > 0
            # 创建 HTTP session（带默认超时）
            if not self._session or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
                self._session = aiohttp.ClientSession(timeout=timeout)

            # 获取 listenKey
            self._listen_key = await self._get_listen_key()
            logger.debug(f"获取 listenKey: {self._listen_key[:20]}...")

            # 构建 WS URL
            ws_url = f"{self._get_ws_url()}/ws/{self._listen_key}"
            logger.debug(f"User Data Stream URL: {ws_url[:50]}...")

            # 建立 WS 连接
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )

            log_ws_connect("user_data")
            self._current_delay_ms = self.initial_delay_ms

            if was_reconnect and self.on_reconnect:
                try:
                    self.on_reconnect("user_data")
                except Exception as e:
                    log_error(f"on_reconnect 回调异常: {e}")

            self._reconnect_count = 0

            # 启动 listenKey 续期任务
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            # 开始接收消息
            await self._receive_loop()

        except asyncio.CancelledError:
            self._running = False
            raise
        except Exception as e:
            log_error(f"User Data Stream 连接失败: {e}")
            if self._running:
                await self._reconnect()
        finally:
            # connect() 被取消/退出时，确保释放 aiohttp session，避免进程退出时报警
            if not self._running:
                try:
                    await asyncio.shield(self.disconnect())
                except Exception:
                    pass

    async def disconnect(self) -> None:
        """
        断开 WS 连接
        """
        async with self._disconnect_lock:
            had_resources = (
                self._ws is not None
                or self._listen_key is not None
                or self._session is not None
                or (self._keepalive_task is not None and not self._keepalive_task.done())
            )

            self._running = False

            # 取消续期任务
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await asyncio.wait_for(self._keepalive_task, timeout=TASK_CANCEL_TIMEOUT_S)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    pass
            self._keepalive_task = None

            # 关闭 WS 连接
            if self._ws:
                try:
                    await asyncio.wait_for(self._ws.close(), timeout=WS_CLOSE_TIMEOUT_S)
                except Exception:
                    pass
                self._ws = None

            # 关闭 listenKey
            if self._listen_key:
                try:
                    await asyncio.wait_for(self._close_listen_key(), timeout=LISTEN_KEY_CLOSE_TIMEOUT_S)
                except Exception as e:
                    get_logger().warning(f"关闭 listenKey 失败: {e}")
                self._listen_key = None

            # 关闭 HTTP session
            if self._session:
                try:
                    await asyncio.wait_for(self._session.close(), timeout=SESSION_CLOSE_TIMEOUT_S)
                except Exception:
                    pass
                self._session = None

            if had_resources:
                log_ws_disconnect("user_data")

    async def _get_listen_key(self) -> str:
        """
        获取 listenKey（REST API）

        Returns:
            listenKey
        """
        if not self._session:
            raise RuntimeError("HTTP session 未初始化")

        url = f"{self._get_rest_url()}/fapi/v1/listenKey"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with self._session.post(url, headers=headers, proxy=self.proxy) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"获取 listenKey 失败: {resp.status} {text}")

            data = await resp.json()
            return str(data.get("listenKey", ""))

    async def _keepalive_listen_key(self) -> None:
        """
        续期 listenKey（REST API）
        """
        if not self._session or not self._listen_key:
            return

        url = f"{self._get_rest_url()}/fapi/v1/listenKey"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with self._session.put(url, headers=headers, proxy=self.proxy) as resp:
            if resp.status != 200:
                text = await resp.text()
                get_logger().warning(f"续期 listenKey 失败: {resp.status} {text}")
            else:
                get_logger().debug("listenKey 续期成功")

    async def _close_listen_key(self) -> None:
        """
        关闭 listenKey（REST API）
        """
        if not self._session or not self._listen_key:
            return

        url = f"{self._get_rest_url()}/fapi/v1/listenKey"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with self._session.delete(url, headers=headers, proxy=self.proxy) as resp:
            if resp.status != 200:
                text = await resp.text()
                get_logger().warning(f"关闭 listenKey 失败: {resp.status} {text}")

    async def _keepalive_loop(self) -> None:
        """
        listenKey 续期循环（每 30 分钟）
        """
        while self._running:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL_MS / 1000.0)
                if self._running:
                    await self._keepalive_listen_key()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"listenKey 续期循环错误: {e}")

    async def _receive_loop(self) -> None:
        """
        接收消息循环
        """
        if not self._ws:
            return

        try:
            async for message in self._ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    log_error(f"JSON 解析错误: {e}")
                except Exception as e:
                    log_error(f"消息处理错误: {e}")

        except websockets.ConnectionClosed as e:
            if self._running:
                log_ws_disconnect("user_data", f"code={e.code}")
                await self._reconnect()
        except Exception as e:
            log_error(f"User Data Stream 接收错误: {e}")
            if self._running:
                await self._reconnect()

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """
        处理 WS 消息

        User Data Stream 事件类型：
        - listenKeyExpired: listenKey 过期
        - ACCOUNT_UPDATE: 账户更新
        - ORDER_TRADE_UPDATE: 订单/交易更新
        - ACCOUNT_CONFIG_UPDATE: 配置更新（如杠杆）
        """
        event_type = data.get("e", "")

        if event_type == "listenKeyExpired":
            get_logger().warning("listenKey 已过期，重新连接...")
            if self._running:
                await self._reconnect()
            return

        if event_type == "ORDER_TRADE_UPDATE":
            order_update = self._parse_order_update(data)
            if order_update:
                self.on_order_update(order_update)
            return

        if event_type == "ALGO_UPDATE":
            algo_update = self._parse_algo_order_update(data)
            if algo_update and self.on_algo_order_update:
                self.on_algo_order_update(algo_update)
            return

        if event_type == "ACCOUNT_UPDATE":
            if not self.on_position_update:
                return
            updates = self._parse_account_update(data)
            for update in updates:
                self.on_position_update(update)
            return

        if event_type == "ACCOUNT_CONFIG_UPDATE":
            if not self.on_leverage_update:
                return
            update = self._parse_account_config_update(data)
            if update:
                self.on_leverage_update(update)
            return

    def _parse_order_update(self, data: Dict[str, Any]) -> Optional[OrderUpdate]:
        """
        解析 ORDER_TRADE_UPDATE 事件

        格式：
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1591097736594,     // event time
            "T": 1591097736591,     // transaction time
            "o": {
                "s": "BTCUSDT",     // symbol
                "c": "client_id",   // client order id
                "S": "SELL",        // side
                "o": "LIMIT",       // order type
                "f": "GTC",         // time in force
                "q": "0.001",       // original qty
                "p": "50000",       // original price
                "ap": "0",          // average price
                "sp": "0",          // stop price
                "x": "NEW",         // execution type
                "X": "NEW",         // order status
                "i": 12345678,      // order id
                "l": "0",           // last filled qty
                "z": "0",           // cumulative filled qty
                "L": "0",           // last filled price
                "n": "0",           // commission
                "N": "USDT",        // commission asset
                "T": 1591097736594, // order trade time
                "t": 0,             // trade id
                "b": "0",           // bids notional
                "a": "0",           // asks notional
                "m": false,         // is maker
                "R": false,         // is reduce only
                "wt": "CONTRACT_PRICE",  // stop price working type
                "ot": "LIMIT",      // original order type
                "ps": "LONG",       // position side
                "cp": false,        // close position
                "rp": "0",          // realized profit
                "pP": false,        // ignore
                "si": 0,            // ignore
                "ss": 0             // ignore
            }
        }
        """
        try:
            order_data = data.get("o", {})
            if not order_data:
                return None

            # 解析 symbol（需要转换为 ccxt 格式）
            ws_symbol = order_data.get("s", "")
            symbol = self._ws_to_symbol(ws_symbol)

            # 解析方向
            side_str = order_data.get("S", "")
            side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

            # 解析持仓方向
            ps_str = order_data.get("ps", "")
            position_side = PositionSide.LONG if ps_str == "LONG" else PositionSide.SHORT

            # 解析状态
            status_str = order_data.get("X", "")
            status = self._parse_order_status(status_str)

            # 解析数量和价格
            filled_qty = Decimal(str(order_data.get("z", "0")))
            avg_price = Decimal(str(order_data.get("ap", "0")))

            order_type = order_data.get("o")
            close_position = order_data.get("cp")
            reduce_only = order_data.get("R")
            is_maker = order_data.get("m")
            realized_pnl_raw = order_data.get("rp")
            realized_pnl: Optional[Decimal] = None
            if realized_pnl_raw is not None:
                try:
                    realized_pnl = Decimal(str(realized_pnl_raw))
                except Exception:
                    realized_pnl = None

            # 时间戳
            timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()

            return OrderUpdate(
                symbol=symbol,
                order_id=str(order_data.get("i", "")),
                client_order_id=str(order_data.get("c", "")),
                side=side,
                position_side=position_side,
                status=status,
                filled_qty=filled_qty,
                avg_price=avg_price,
                timestamp_ms=timestamp_ms,
                order_type=str(order_type) if order_type is not None else None,
                close_position=bool(close_position) if isinstance(close_position, bool) else None,
                reduce_only=bool(reduce_only) if isinstance(reduce_only, bool) else None,
                is_maker=bool(is_maker) if isinstance(is_maker, bool) else None,
                realized_pnl=realized_pnl,
            )

        except Exception as e:
            log_error(f"解析 ORDER_TRADE_UPDATE 失败: {e}")
            return None

    def _parse_algo_order_update(self, data: Dict[str, Any]) -> Optional[AlgoOrderUpdate]:
        """
        解析 ALGO_UPDATE 事件（Algo Service 条件单更新）

        参考 Binance 文档：User Data Streams - Event Algo Order Update
        """
        try:
            order_data = data.get("o", {})
            if not isinstance(order_data, dict) or not order_data:
                return None

            ws_symbol = order_data.get("s", "")
            symbol = self._ws_to_symbol(ws_symbol)

            side_str = order_data.get("S", "")
            side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

            ps_str = order_data.get("ps", "")
            position_side: Optional[PositionSide]
            if ps_str == "LONG":
                position_side = PositionSide.LONG
            elif ps_str == "SHORT":
                position_side = PositionSide.SHORT
            else:
                position_side = None

            algo_id = str(order_data.get("aid", ""))
            client_algo_id = str(order_data.get("caid", ""))
            status = str(order_data.get("X", ""))

            order_type = order_data.get("o")
            close_position = order_data.get("cp")
            reduce_only = order_data.get("R")

            timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()

            return AlgoOrderUpdate(
                symbol=symbol,
                algo_id=algo_id,
                client_algo_id=client_algo_id,
                side=side,
                status=status,
                timestamp_ms=timestamp_ms,
                order_type=str(order_type) if order_type is not None else None,
                position_side=position_side,
                close_position=bool(close_position) if isinstance(close_position, bool) else None,
                reduce_only=bool(reduce_only) if isinstance(reduce_only, bool) else None,
            )
        except Exception as e:
            log_error(f"解析 ALGO_UPDATE 失败: {e}")
            return None

    def _parse_account_update(self, data: Dict[str, Any]) -> List[PositionUpdate]:
        """
        解析 ACCOUNT_UPDATE 事件（仓位变更）

        格式（简化，部分字段）：
        {
          "e": "ACCOUNT_UPDATE",
          "E": 1591097736594,
          "a": {
            "P": [
              {
                "s": "BTCUSDT",
                "pa": "0.001",
                "ep": "50000",
                "up": "0.12",
                "ps": "LONG"
              }
            ]
          }
        }
        """
        account = data.get("a")
        if not isinstance(account, dict):
            return []

        raw_positions = account.get("P")
        if not isinstance(raw_positions, list):
            return []

        timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()

        updates: List[PositionUpdate] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue

            ws_symbol = str(raw.get("s", "")).strip()
            if not ws_symbol:
                continue
            symbol = self._ws_to_symbol(ws_symbol)

            ps_str = str(raw.get("ps", "")).upper()
            if ps_str not in ("LONG", "SHORT"):
                continue
            position_side = PositionSide.LONG if ps_str == "LONG" else PositionSide.SHORT

            position_amt = Decimal(str(raw.get("pa", "0")))
            if position_side == PositionSide.LONG:
                position_amt = abs(position_amt)
            else:
                position_amt = -abs(position_amt)

            entry_price = Decimal(str(raw.get("ep", "0")))
            unrealized_pnl = Decimal(str(raw.get("up", "0")))

            updates.append(
                PositionUpdate(
                    symbol=symbol,
                    position_side=position_side,
                    position_amt=position_amt,
                    entry_price=entry_price if entry_price > Decimal("0") else None,
                    unrealized_pnl=unrealized_pnl,
                    timestamp_ms=timestamp_ms,
                )
            )

        return updates

    def _parse_account_config_update(self, data: Dict[str, Any]) -> Optional[LeverageUpdate]:
        """
        解析 ACCOUNT_CONFIG_UPDATE 事件（杠杆变更）

        格式（简化）：
        {
          "e": "ACCOUNT_CONFIG_UPDATE",
          "E": 1611646737478,
          "T": 1611646737476,
          "ac": {
            "s": "BTCUSDT",
            "l": "25"
          }
        }
        """
        config = data.get("ac")
        if not isinstance(config, dict):
            return None

        ws_symbol = str(config.get("s", "")).strip()
        if not ws_symbol:
            return None
        symbol = self._ws_to_symbol(ws_symbol)

        raw_leverage = config.get("l")
        if raw_leverage is None:
            return None

        try:
            leverage = int(raw_leverage)
        except (TypeError, ValueError):
            try:
                leverage = int(float(raw_leverage))
            except (TypeError, ValueError):
                return None

        if leverage <= 0:
            return None

        timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()
        return LeverageUpdate(symbol=symbol, leverage=leverage, timestamp_ms=timestamp_ms)

    def _ws_to_symbol(self, ws_symbol: str) -> str:
        """
        将 WS 格式 symbol 转换为 ccxt 格式

        "BTCUSDT" -> "BTC/USDT:USDT"
        """
        from src.utils.helpers import ws_stream_to_symbol
        return ws_stream_to_symbol(ws_symbol)

    def _parse_order_status(self, status_str: str) -> OrderStatus:
        """解析订单状态"""
        status_map = {
            "NEW": OrderStatus.NEW,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        return status_map.get(status_str, OrderStatus.NEW)

    async def _reconnect(self) -> None:
        """
        断线重连（指数退避）
        """
        if not self._running:
            return

        self._reconnect_count += 1
        delay_s = self._current_delay_ms / 1000.0

        log_ws_reconnect("user_data", self._reconnect_count)
        get_logger().info(f"将在 {delay_s:.1f}s 后重连...")

        # 等待
        await asyncio.sleep(delay_s)

        # 计算下次延迟
        self._current_delay_ms = min(
            self._current_delay_ms * self.multiplier,
            self.max_delay_ms
        )

        # 清理旧连接
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # 取消续期任务
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        # 重新连接（重新获取 listenKey）
        self._listen_key = None
        if self._running:
            await self.connect()

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        if self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except Exception:
            return False

    @property
    def reconnect_count(self) -> int:
        """重连次数"""
        return self._reconnect_count

    @property
    def listen_key(self) -> Optional[str]:
        """当前 listenKey"""
        return self._listen_key
