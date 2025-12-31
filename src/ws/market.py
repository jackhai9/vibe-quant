# Input: WS URLs, symbols, callbacks, reconnect state
# Output: MarketEvent stream + reconnect callbacks (aiohttp ws + proxy)
# Pos: market WS client (market data)
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
市场数据 WebSocket 模块

职责：
- 连接 Binance Futures WS
- 订阅 bookTicker（best bid/ask）
- 订阅 aggTrade（last trade）
- 断线自动重连（指数退避）
- 将 WS 消息转换为 MarketEvent
- 数据陈旧检测

输入：
- 配置（symbol 列表、重连参数）

输出：
- MarketEvent（通过回调）
"""

import asyncio
import json
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Any

import aiohttp

from src.models import MarketEvent
from src.utils.logger import (
    get_logger,
    log_ws_connect,
    log_ws_disconnect,
    log_ws_reconnect,
    log_error,
)
from src.utils.helpers import current_time_ms, ws_stream_to_symbol


# Binance Futures WebSocket 基础 URL
WS_BASE_URL = "wss://fstream.binance.com"

# WS 默认超时与心跳
HTTP_TIMEOUT_S = 10.0
WS_CLOSE_TIMEOUT_S = 1.0
SESSION_CLOSE_TIMEOUT_S = 1.0
WS_HEARTBEAT_S = 20.0


class MarketWSClient:
    """市场数据 WebSocket 客户端"""

    def __init__(
        self,
        symbols: List[str],
        on_event: Callable[[MarketEvent], None],
        on_reconnect: Optional[Callable[[str], None]] = None,
        initial_delay_ms: int = 1000,
        max_delay_ms: int = 30000,
        multiplier: int = 2,
        stale_data_ms: int = 1500,
        proxy: Optional[str] = None,
    ):
        """
        初始化市场数据 WS 客户端

        Args:
            symbols: 订阅的 symbol 列表（ccxt 格式，如 "BTC/USDT:USDT"）
            on_event: 收到 MarketEvent 时的回调
            on_reconnect: WS 重连成功回调（用于触发上层 REST 校准）
            initial_delay_ms: 重连初始延迟
            max_delay_ms: 重连最大延迟
            multiplier: 重连延迟倍数
            stale_data_ms: 数据陈旧阈值（ms）
            proxy: HTTP 代理地址，如 "http://127.0.0.1:7890"
        """
        self.symbols = symbols
        self.on_event = on_event
        self.on_reconnect = on_reconnect
        self.initial_delay_ms = initial_delay_ms
        self.max_delay_ms = max_delay_ms
        self.multiplier = multiplier
        self.stale_data_ms = stale_data_ms
        self.proxy = proxy

        # WebSocket 连接
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._reconnect_task: Optional[asyncio.Task[None]] = None

        # 当前重连延迟
        self._current_delay_ms = initial_delay_ms

        # 每个 symbol 的最后更新时间
        self._last_update_ms: Dict[str, int] = {}
        # 每个 symbol 的最后 markPrice 更新时间（不参与 stale 判定）
        self._last_mark_price_ms: Dict[str, int] = {}

        # 重连次数
        self._reconnect_count = 0

    def _build_stream_url(self) -> str:
        """
        构建 combined streams URL

        格式: wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade/...
        """
        streams = []
        for symbol in self.symbols:
            # 转换 ccxt 格式到 WS 格式
            ws_symbol = self._symbol_to_ws(symbol)
            streams.append(f"{ws_symbol}@bookTicker")
            streams.append(f"{ws_symbol}@aggTrade")
            streams.append(f"{ws_symbol}@markPrice@1s")

        stream_str = "/".join(streams)
        return f"{WS_BASE_URL}/stream?streams={stream_str}"

    def _symbol_to_ws(self, symbol: str) -> str:
        """
        将 ccxt 格式 symbol 转换为 WS 格式

        "BTC/USDT:USDT" -> "btcusdt"
        """
        # 移除 :USDT 后缀和 /
        base = symbol.replace(":USDT", "").replace("/", "").lower()
        return base

    def _ws_to_symbol(self, ws_symbol: str) -> str:
        """
        将 WS 格式 symbol 转换为 ccxt 格式

        "BTCUSDT" -> "BTC/USDT:USDT"
        """
        return ws_stream_to_symbol(ws_symbol)

    async def connect(self) -> None:
        """
        建立 WS 连接并开始接收数据
        """
        # 修复：只有已连接时才跳过，而非 _running=True 时跳过
        # 否则 _reconnect() 调用 connect() 时会直接返回，导致重连失败
        if self.is_connected:
            return

        self._running = True
        logger = get_logger()

        url = self._build_stream_url()
        logger.debug(f"WS 连接 URL: {url}")

        try:
            was_reconnect = self._reconnect_count > 0
            if not self._session or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
                self._session = aiohttp.ClientSession(timeout=timeout)

            self._ws = await self._session.ws_connect(
                url,
                heartbeat=WS_HEARTBEAT_S,
                proxy=self.proxy,
                timeout=aiohttp.ClientWSTimeout(ws_close=WS_CLOSE_TIMEOUT_S),
            )

            log_ws_connect("market_data")
            self._current_delay_ms = self.initial_delay_ms

            if was_reconnect and self.on_reconnect:
                try:
                    self.on_reconnect("market_data")
                except Exception as e:
                    log_error(f"on_reconnect 回调异常: {e}")

            self._reconnect_count = 0

            # 初始化所有 symbol 的更新时间
            now = current_time_ms()
            for symbol in self.symbols:
                self._last_update_ms[symbol] = now

            # 开始接收消息
            await self._receive_loop()

        except Exception as e:
            log_error(f"WS 连接失败: {type(e).__name__} {e}")
            if self._running:
                await self._reconnect()

    async def disconnect(self) -> None:
        """
        断开 WS 连接
        """
        self._running = False

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await asyncio.wait_for(self._reconnect_task, timeout=1.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                pass

        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=WS_CLOSE_TIMEOUT_S)
            except Exception:
                pass
            self._ws = None

        if self._session:
            try:
                await asyncio.wait_for(self._session.close(), timeout=SESSION_CLOSE_TIMEOUT_S)
            except Exception:
                pass
            self._session = None

        log_ws_disconnect("market_data")

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

                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError as e:
                        log_error(f"JSON 解析错误: {e}")
                    except Exception as e:
                        log_error(f"消息处理错误: {e}")
                elif message.type == aiohttp.WSMsgType.ERROR:
                    err = self._ws.exception() if self._ws else None
                    log_error(f"WS 接收错误: {err}")
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break

        except asyncio.CancelledError:
            self._running = False
            raise
        except Exception as e:
            log_error(f"WS 接收错误: {type(e).__name__} {e}")

        if self._running:
            close_code = self._ws.close_code if self._ws else None
            if close_code is not None:
                log_ws_disconnect("market_data", f"code={close_code}")
            else:
                log_ws_disconnect("market_data")
            await self._reconnect()

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """
        处理 WS 消息

        Combined streams 格式:
        {
            "stream": "btcusdt@bookTicker",
            "data": {...}
        }
        """
        stream = data.get("stream", "")
        payload = data.get("data", {})

        if not stream or not payload:
            return

        # 解析事件
        if "@bookTicker" in stream:
            event = self._parse_book_ticker(payload)
        elif "@aggTrade" in stream:
            event = self._parse_agg_trade(payload)
        elif "@markPrice" in stream:
            event = self._parse_mark_price(payload)
        else:
            return

        if event:
            # 更新最后更新时间（stale 仅由 bookTicker/aggTrade 刷新；markPrice 不参与）
            if event.event_type in ("book_ticker", "agg_trade"):
                self._last_update_ms[event.symbol] = event.timestamp_ms
            elif event.event_type == "mark_price":
                self._last_mark_price_ms[event.symbol] = event.timestamp_ms
            # 调用回调
            self.on_event(event)

    def _parse_book_ticker(self, data: Dict[str, Any]) -> Optional[MarketEvent]:
        """
        解析 bookTicker 消息

        格式:
        {
            "e": "bookTicker",
            "u": 400900217,
            "s": "BTCUSDT",
            "b": "25.35190000",  // best bid price
            "B": "31.21000000",  // best bid qty
            "a": "25.36520000",  // best ask price
            "A": "40.66000000",  // best ask qty
            "T": 1591097736594,  // transaction time
            "E": 1591097736593   // event time
        }
        """
        try:
            ws_symbol = data.get("s", "")
            symbol = self._ws_to_symbol(ws_symbol)

            # 验证是我们订阅的 symbol
            if symbol not in self.symbols:
                return None

            best_bid = Decimal(str(data.get("b", "0")))
            best_ask = Decimal(str(data.get("a", "0")))
            timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()

            # 验证 bid <= ask（bid > ask 为异常数据，bid == ask 在低流动性市场可能出现）
            if best_bid > best_ask:
                get_logger().warning(f"异常报价: {symbol} bid={best_bid} > ask={best_ask}")
                return None

            return MarketEvent(
                symbol=symbol,
                timestamp_ms=timestamp_ms,
                best_bid=best_bid,
                best_ask=best_ask,
                last_trade_price=None,
                event_type="book_ticker",
            )

        except Exception as e:
            log_error(f"解析 bookTicker 失败: {e}")
            return None

    def _parse_agg_trade(self, data: Dict[str, Any]) -> Optional[MarketEvent]:
        """
        解析 aggTrade 消息

        格式:
        {
            "e": "aggTrade",
            "E": 1591097736593,  // event time
            "s": "BTCUSDT",
            "a": 8589026,        // aggregate trade ID
            "p": "9500.00",      // price
            "q": "0.001",        // quantity
            "f": 1,              // first trade ID
            "l": 1,              // last trade ID
            "T": 1591097736594,  // trade time
            "m": true            // is buyer maker
        }
        """
        try:
            ws_symbol = data.get("s", "")
            symbol = self._ws_to_symbol(ws_symbol)

            # 验证是我们订阅的 symbol
            if symbol not in self.symbols:
                return None

            last_trade_price = Decimal(str(data.get("p", "0")))
            timestamp_ms = int(data.get("T", 0)) or int(data.get("E", 0)) or current_time_ms()

            return MarketEvent(
                symbol=symbol,
                timestamp_ms=timestamp_ms,
                best_bid=None,
                best_ask=None,
                last_trade_price=last_trade_price,
                event_type="agg_trade",
            )

        except Exception as e:
            log_error(f"解析 aggTrade 失败: {e}")
            return None

    def _parse_mark_price(self, data: Dict[str, Any]) -> Optional[MarketEvent]:
        """
        解析 markPriceUpdate 消息

        格式（部分字段）：
        {
            "e": "markPriceUpdate",
            "E": 1562305380000,     // event time
            "s": "BTCUSDT",         // symbol
            "p": "11185.87786614",  // mark price
            "i": "11154.40853556",  // index price
            "r": "0.00010000",      // funding rate
            "T": 1562306400000      // next funding time
        }
        """
        try:
            ws_symbol = data.get("s", "")
            symbol = self._ws_to_symbol(ws_symbol)

            if symbol not in self.symbols:
                return None

            mark_price = Decimal(str(data.get("p", "0")))
            timestamp_ms = int(data.get("E", 0)) or int(data.get("T", 0)) or current_time_ms()

            if mark_price <= Decimal("0"):
                return None

            return MarketEvent(
                symbol=symbol,
                timestamp_ms=timestamp_ms,
                best_bid=None,
                best_ask=None,
                last_trade_price=None,
                mark_price=mark_price,
                event_type="mark_price",
            )
        except Exception as e:
            log_error(f"解析 markPriceUpdate 失败: {e}")
            return None

    async def _reconnect(self) -> None:
        """
        断线重连（指数退避）

        策略：
        - 初始延迟: 1s
        - 倍数: 2x
        - 最大延迟: 30s
        - 无限重试
        """
        if not self._running:
            return

        self._reconnect_count += 1
        delay_s = self._current_delay_ms / 1000.0

        log_ws_reconnect("market_data", self._reconnect_count)
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

        # 重新连接
        if self._running:
            await self.connect()

    def is_stale(self, symbol: str) -> bool:
        """
        检查指定 symbol 的数据是否陈旧

        Args:
            symbol: 交易对符号

        Returns:
            True 表示数据陈旧
        """
        last_update = self._last_update_ms.get(symbol, 0)
        if last_update == 0:
            return True

        elapsed = current_time_ms() - last_update
        return elapsed > self.stale_data_ms

    def get_last_update_ms(self, symbol: str) -> int:
        """
        获取指定 symbol 的最后更新时间

        Args:
            symbol: 交易对符号

        Returns:
            最后更新时间戳（ms）
        """
        return self._last_update_ms.get(symbol, 0)

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        if self._ws is None:
            return False
        try:
            return not self._ws.closed
        except Exception:
            return False

    @property
    def reconnect_count(self) -> int:
        """重连次数"""
        return self._reconnect_count
