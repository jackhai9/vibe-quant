# Input: numeric values, time, symbols
# Output: rounded values and formatting helpers
# Pos: utility functions and formatters
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
工具函数模块

职责：
- 价格/数量规整函数
- 时间戳工具
- 其他通用工具
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Optional
import time


def round_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """
    按 tick_size 规整价格（向下取整）

    Args:
        value: 原始值
        tick_size: 最小变动单位

    Returns:
        规整后的值
    """
    if tick_size <= 0:
        return value
    return (value // tick_size) * tick_size


def round_up_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """
    按 tick_size 规整价格（向上取整）

    Args:
        value: 原始值
        tick_size: 最小变动单位

    Returns:
        规整后的值
    """
    if tick_size <= 0:
        return value
    remainder = value % tick_size
    if remainder > 0:
        return value + (tick_size - remainder)
    return value


def round_to_step(value: Decimal, step_size: Decimal) -> Decimal:
    """
    按 step_size 规整数量（向下取整）

    Args:
        value: 原始值
        step_size: 步进单位

    Returns:
        规整后的值
    """
    if step_size <= 0:
        return value
    return (value // step_size) * step_size


def round_up_to_step(value: Decimal, step_size: Decimal) -> Decimal:
    """
    按 step_size 规整数量（向上取整）

    用于满足 minNotional 时增大数量。

    Args:
        value: 原始值
        step_size: 步进单位

    Returns:
        规整后的值
    """
    if step_size <= 0:
        return value
    remainder = value % step_size
    if remainder > 0:
        return value + (step_size - remainder)
    return value


def current_time_ms() -> int:
    """
    获取当前时间戳（毫秒）

    Returns:
        当前时间戳
    """
    return int(time.time() * 1000)


def symbol_to_ws_stream(symbol: str) -> str:
    """
    将 ccxt symbol 格式转换为 Binance WS stream 格式

    例如：BTC/USDT:USDT -> btcusdt

    Args:
        symbol: ccxt 格式的 symbol

    Returns:
        WS stream 格式
    """
    # 移除 :USDT 后缀，移除 /，转小写
    base = symbol.split(":")[0]  # BTC/USDT
    return base.replace("/", "").lower()  # btcusdt


def ws_stream_to_symbol(stream_symbol: str) -> str:
    """
    将 Binance WS stream symbol 转换为 ccxt 格式

    例如：BTCUSDT -> BTC/USDT:USDT

    Args:
        stream_symbol: WS stream 格式

    Returns:
        ccxt 格式的 symbol
    """
    # 假设都是 USDT 永续
    # 需要根据实际情况调整
    stream_symbol = stream_symbol.upper()
    if stream_symbol.endswith("USDT"):
        base = stream_symbol[:-4]
        return f"{base}/USDT:USDT"
    return stream_symbol


def format_decimal(value: Optional[Decimal], precision: int = 4) -> Optional[str]:
    """
    格式化 Decimal（用于日志/通知），最多保留指定小数位并去除尾部 0。

    Args:
        value: 原始 Decimal
        precision: 小数位数
    """
    if value is None:
        return None

    if precision < 0:
        raise ValueError("precision must be non-negative")

    quantizer = Decimal("1").scaleb(-precision)
    quantized = value.quantize(quantizer, rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_decimal_fixed(value: Optional[Decimal], precision: int = 4) -> Optional[str]:
    """
    固定小数位格式化 Decimal（用于通知），保留指定小数位。

    Args:
        value: 原始 Decimal
        precision: 小数位数
    """
    if value is None:
        return None
    if precision < 0:
        raise ValueError("precision must be non-negative")
    quantizer = Decimal("1").scaleb(-precision)
    quantized = value.quantize(quantizer, rounding=ROUND_HALF_UP)
    return format(quantized, "f")
