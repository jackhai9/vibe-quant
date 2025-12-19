"""
日志配置模块

职责：
- 配置 loguru 按天滚动日志
- 定义结构化日志格式
- 提供事件日志记录函数

输出：
- 配置好的 logger
- log_event() 结构化日志函数
"""

import sys
from pathlib import Path
from typing import Optional, Any
from decimal import Decimal

from src.utils.helpers import format_decimal

from loguru import logger


# 移除默认的 stderr handler
logger.remove()

# 全局 logger 实例
_logger = logger


EVENT_TYPE_CN = {
    "startup": "启动",
    "shutdown": "关闭",
    "ws_connect": "WS连接",
    "ws_disconnect": "WS断开",
    "ws_reconnect": "WS重连",
    "market_update": "行情更新",
    "signal": "信号",
    "order_place": "下单提交",
    "order_cancel": "撤单",
    "order_fill": "已成交",
    "order_timeout": "超时未成交",
    "position_update": "仓位更新",
    "mode_change": "模式切换",
    "calibration": "校准",
    "risk_trigger": "风险触发",
    "rate_limit": "限速",
    "error": "错误",
}


def setup_logger(
    log_dir: Path,
    level: str = "INFO",
    *,
    file_level: Optional[str] = None,
    rotation: str = "00:00",  # 每天零点滚动
    retention: str = "30 days",
    console: bool = True,
) -> None:
    """
    配置日志系统

    Args:
        log_dir: 日志目录
        level: 日志级别（DEBUG, INFO, WARNING, ERROR）
        rotation: 滚动策略（默认每天零点）
        retention: 保留时间（默认 30 天）
        console: 是否输出到控制台
    """
    global _logger

    effective_file_level = file_level or level

    # 清理旧的 sink，避免多次调用导致文件句柄泄漏（尤其在测试中）
    _logger.remove()

    # 确保日志目录存在
    log_dir.mkdir(parents=True, exist_ok=True)

    # 日志格式
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # 文件日志格式（不带颜色）
    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} | "
        "{message}"
    )

    # 控制台输出
    if console:
        _logger.add(
            sys.stderr,
            format=log_format,
            level=level,
            colorize=True,
        )

    # 文件输出 - 按天滚动
    log_file = log_dir / "vibe-quant_{time:YYYY-MM-DD}.log"
    _logger.add(
        str(log_file),
        format=file_format,
        level=effective_file_level,
        rotation=rotation,
        retention=retention,
        compression="gz",  # 压缩旧日志
        encoding="utf-8",
    )

    # 错误日志单独文件
    error_file = log_dir / "error_{time:YYYY-MM-DD}.log"
    _logger.add(
        str(error_file),
        format=file_format,
        level="ERROR",
        rotation=rotation,
        retention=retention,
        compression="gz",
        encoding="utf-8",
    )

    if console:
        _logger.info(
            f"日志系统初始化完成，目录: {log_dir}, 控制台级别: {level}, 文件级别: {effective_file_level}"
        )
    else:
        _logger.info(
            f"日志系统初始化完成，目录: {log_dir}, 文件级别: {effective_file_level}"
        )


def get_logger():
    """获取 logger 实例"""
    return _logger


def _format_value(value: Any) -> str:
    """格式化值为字符串"""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format_decimal(value) or ""
    return str(value)


def _build_extra_fields(**kwargs) -> str:
    """构建额外字段字符串"""
    fields = []
    for key, value in kwargs.items():
        if value is not None:
            fields.append(f"{key}={_format_value(value)}")
    return " | ".join(fields) if fields else ""


def log_event(
    event_type: str,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    mode: Optional[str] = None,
    state: Optional[str] = None,
    reason: Optional[str] = None,
    best_bid: Optional[Decimal] = None,
    best_ask: Optional[Decimal] = None,
    last_trade: Optional[Decimal] = None,
    order_id: Optional[str] = None,
    qty: Optional[Decimal] = None,
    price: Optional[Decimal] = None,
    filled_qty: Optional[Decimal] = None,
    avg_price: Optional[Decimal] = None,
    position_amt: Optional[Decimal] = None,
    error: Optional[str] = None,
    **kwargs,
) -> None:
    """
    记录结构化事件日志

    Args:
        event_type: 事件类型
            - startup: 启动
            - shutdown: 关闭
            - ws_connect: WS 连接
            - ws_disconnect: WS 断开
            - ws_reconnect: WS 重连
            - market_update: 行情更新
            - signal: 信号触发
            - order_place: 下单
            - order_cancel: 撤单
            - order_fill: 成交
            - order_timeout: 超时
            - position_update: 仓位更新
            - error: 错误
        symbol: 交易对
        side: 方向（LONG/SHORT）
        mode: 执行模式（MAKER_ONLY/AGGRESSIVE_LIMIT）
        state: 状态（IDLE/PLACING/WAITING/CANCELING/COOLDOWN）
        reason: 原因
        best_bid: 买一价
        best_ask: 卖一价
        last_trade: 最近成交价
        order_id: 订单 ID
        qty: 数量
        price: 价格
        filled_qty: 成交数量
        avg_price: 平均成交价
        position_amt: 仓位数量
        error: 错误信息
        **kwargs: 其他字段
    """
    normalized_event_type = event_type.lower()
    event_cn = EVENT_TYPE_CN.get(normalized_event_type)

    # 构建基础字段
    base_fields = {
        "cn": event_cn if event_cn and "cn" not in kwargs else None,
        "symbol": symbol,
        "side": side,
        "mode": mode,
        "state": state,
        "reason": reason,
        "order_id": order_id,
    }

    # 构建价格字段
    price_fields = {
        "bid": best_bid,
        "ask": best_ask,
        "last": last_trade,
        "price": price,
        "avg_price": avg_price,
    }

    # 构建数量字段
    qty_fields = {
        "qty": qty,
        "filled": filled_qty,
        "pos": position_amt,
    }

    # 合并所有字段
    all_fields = {**base_fields, **price_fields, **qty_fields, **kwargs}

    # 过滤掉 None 值
    fields_str = _build_extra_fields(**all_fields)

    # 构建日志消息
    message = f"[{event_type.upper()}]"
    if fields_str:
        message = f"{message} {fields_str}"

    # 根据事件类型选择日志级别
    if event_type == "error" or error:
        if error:
            message = f"{message} | error={error}"
        _logger.error(message)
    elif event_type in ("ws_disconnect", "ws_reconnect", "order_timeout", "risk_trigger", "rate_limit"):
        _logger.warning(message)
    elif event_type in ("startup", "shutdown", "signal", "order_fill"):
        _logger.info(message)
    elif event_type in ("market_update",):
        _logger.debug(message)
    else:
        _logger.info(message)


# 便捷函数
def log_startup(symbols: list[str]) -> None:
    """记录启动事件"""
    log_event("startup", reason=f"symbols={','.join(symbols)}")


def log_shutdown(reason: str = "normal") -> None:
    """记录关闭事件"""
    log_event("shutdown", reason=reason)


def log_ws_connect(stream_type: str) -> None:
    """记录 WS 连接事件"""
    log_event("ws_connect", reason=stream_type)


def log_ws_disconnect(stream_type: str, reason: Optional[str] = None) -> None:
    """记录 WS 断开事件"""
    log_event("ws_disconnect", reason=f"{stream_type}: {reason}" if reason else stream_type)


def log_ws_reconnect(stream_type: str, attempt: int) -> None:
    """记录 WS 重连事件"""
    log_event("ws_reconnect", reason=f"{stream_type} attempt={attempt}")


def log_market_update(
    symbol: str,
    best_bid: Decimal,
    best_ask: Decimal,
    last_trade: Optional[Decimal] = None,
) -> None:
    """记录行情更新事件"""
    log_event(
        "market_update",
        symbol=symbol,
        best_bid=best_bid,
        best_ask=best_ask,
        last_trade=last_trade,
    )


def log_signal(
    symbol: str,
    side: str,
    reason: str,
    best_bid: Optional[Decimal] = None,
    best_ask: Optional[Decimal] = None,
    last_trade: Optional[Decimal] = None,
    roi_mult: Optional[int] = None,
    accel_mult: Optional[int] = None,
    roi: Optional[Decimal] = None,
    ret_window: Optional[Decimal] = None,
) -> None:
    """记录信号触发事件"""
    roi_display = format_decimal(roi, precision=4)
    ret_display = format_decimal(ret_window, precision=4)
    log_event(
        "signal",
        symbol=symbol,
        side=side,
        reason=reason,
        best_bid=best_bid,
        best_ask=best_ask,
        last_trade=last_trade,
        roi_mult=roi_mult,
        accel_mult=accel_mult,
        roi=roi_display,
        ret_window=ret_display,
    )


def log_order_place(
    symbol: str,
    side: str,
    mode: str,
    qty: Decimal,
    price: Optional[Decimal] = None,
    order_id: Optional[str] = None,
) -> None:
    """记录下单事件"""
    log_event(
        "order_place",
        symbol=symbol,
        side=side,
        mode=mode,
        qty=qty,
        price=price,
        order_id=order_id,
    )


def log_order_cancel(
    symbol: str,
    order_id: str,
    reason: str = "timeout",
) -> None:
    """记录撤单事件"""
    log_event(
        "order_cancel",
        symbol=symbol,
        order_id=order_id,
        reason=reason,
    )


def log_order_fill(
    symbol: str,
    side: str,
    order_id: str,
    filled_qty: Decimal,
    avg_price: Decimal,
) -> None:
    """记录成交事件"""
    log_event(
        "order_fill",
        symbol=symbol,
        side=side,
        order_id=order_id,
        filled_qty=filled_qty,
        avg_price=avg_price,
    )


def log_order_timeout(
    symbol: str,
    side: str,
    order_id: str,
    timeout_count: int,
) -> None:
    """记录超时事件"""
    log_event(
        "order_timeout",
        symbol=symbol,
        side=side,
        order_id=order_id,
        reason=f"timeout_count={timeout_count}",
    )


def log_position_update(
    symbol: str,
    side: str,
    position_amt: Decimal,
) -> None:
    """记录仓位更新事件"""
    log_event(
        "position_update",
        symbol=symbol,
        side=side,
        position_amt=position_amt,
    )


def log_error(
    error: str,
    symbol: Optional[str] = None,
    **kwargs,
) -> None:
    """记录错误事件"""
    log_event(
        "error",
        symbol=symbol,
        error=error,
        **kwargs,
    )
