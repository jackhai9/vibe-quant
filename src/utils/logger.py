# Input: log dir and normalized event fields
# Output: configured logger and structured logging helpers (including event type CN map)
# Pos: logging setup and event normalization (including fill roles/pnl/console color for fill)
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

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
    "market_update": "行情",
    "signal": "信号",
    "place": "下单",
    "cancel": "撤单",
    "fill": "成交",
    "timeout": "超时",
    "position": "仓位",
    "leverage": "杠杆",
    "leverage_snapshot": "杠杆快照",
    "mode": "模式",
    "calibration": "校准",
    "risk": "风控",
    "rate_limit": "限速",
    "reject": "拒单",
    "order_retry": "重试",
    "fill_rate": "成交率",
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
        "<level>{level: <7}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # 文件日志格式（不带颜色）
    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <7} | "
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
    """构建额外字段字符串，cn 字段始终在最前面且不带 key"""
    fields = []
    # cn 字段优先，且直接显示值（不带 cn=）
    if "cn" in kwargs and kwargs["cn"] is not None:
        fields.append(str(kwargs["cn"]))
    # 其他字段
    for key, value in kwargs.items():
        if key != "cn" and value is not None:
            fields.append(f"{key}={_format_value(value)}")
    return " | ".join(fields) if fields else ""


def log_event(event_type: str, *, level: str | None = None, **fields) -> None:
    """
    记录结构化事件日志

    Args:
        event_type: 事件类型（startup/shutdown/ws_connect/signal/place/fill/...）
        level: 日志级别覆盖（debug/info/warning/error），不传则根据 event_type 自动选择
        **fields: 事件字段，常用字段会自动缩短名称：
            - best_bid → bid
            - best_ask → ask
            - last_trade → last
            - filled_qty → filled
            - position_amt → pos
    """
    normalized_event_type = event_type.lower()
    event_cn = EVENT_TYPE_CN.get(normalized_event_type)

    # event_cn 统一转为 cn
    if "event_cn" in fields:
        fields["cn"] = fields.pop("event_cn")

    # 自动添加中文名（若未手动传入）
    if event_cn and "cn" not in fields:
        fields["cn"] = event_cn

    # 字段名缩短（保持日志简洁）
    field_renames = {
        "best_bid": "bid",
        "best_ask": "ask",
        "last_trade": "last",
        "filled_qty": "filled",
        "position_amt": "pos",
    }
    for old_name, new_name in field_renames.items():
        if old_name in fields:
            fields[new_name] = fields.pop(old_name)

    # symbol 简写：ZEN/USDT:USDT → ZEN
    if "symbol" in fields and fields["symbol"]:
        symbol = str(fields["symbol"])
        if "/" in symbol:
            fields["symbol"] = symbol.split("/")[0]

    # 提取 error 字段用于日志级别判断
    error = fields.get("error")

    # 构建日志消息
    fields_str = _build_extra_fields(**fields)
    message = f"[{event_type.upper()}]"
    if fields_str:
        message = f"{message} {fields_str}"

    # 根据事件类型选择日志级别（level 参数可覆盖）
    event_logger = _logger
    if event_type == "fill":
        event_logger = _logger.opt(colors=True)
        message = f"<green>{message}</green>"

    if level == "debug":
        event_logger.debug(message)
    elif level == "info":
        event_logger.info(message)
    elif level == "warning":
        event_logger.warning(message)
    elif level == "error":
        event_logger.error(message)
    elif event_type == "error" or error:
        event_logger.error(message)
    elif event_type in (
        "ws_disconnect",
        "ws_reconnect",
        "timeout",
        "risk",
        "rate_limit",
        "reject",
    ):
        event_logger.warning(message)
    elif event_type in ("startup", "shutdown", "signal", "fill"):
        event_logger.info(message)
    elif event_type in ("market_update",):
        event_logger.debug(message)
    else:
        event_logger.info(message)


# 便捷函数
def log_startup(symbols: list[str]) -> None:
    """记录启动事件"""
    log_event("startup", symbols=",".join(symbols))


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
        "place",
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
        "cancel",
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
    role: Optional[str] = None,
    pnl: Optional[Decimal] = None,
    fee: Optional[Decimal] = None,
    # fee_asset: Optional[str] = None,
) -> None:
    """记录成交事件"""
    log_event(
        "fill",
        symbol=symbol,
        side=side,
        order_id=order_id,
        filled_qty=filled_qty,
        avg_price=avg_price,
        role=role,
        pnl=pnl,
        fee=fee,
        # fee_asset=fee_asset,
    )


def log_order_timeout(
    symbol: str,
    side: str,
    order_id: str,
    timeout_count: int,
) -> None:
    """记录超时事件"""
    log_event(
        "timeout",
        symbol=symbol,
        side=side,
        timeout_count=timeout_count,
        order_id=order_id,
    )


def log_position_update(
    symbol: str,
    side: str,
    position_amt: Decimal,
) -> None:
    """记录仓位更新事件"""
    log_event(
        "position",
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


def log_order_reject(
    symbol: str,
    side: str,
    reason: str,
    code: Optional[str] = None,
    order_type: Optional[str] = None,
    time_in_force: Optional[str] = None,
    reduce_only: Optional[bool] = None,
    close_position: Optional[bool] = None,
    price: Optional[Decimal] = None,
    qty: Optional[Decimal] = None,
) -> None:
    """记录下单被拒（可预期错误）事件。"""
    log_event(
        "reject",
        symbol=symbol,
        side=side,
        reason=reason,
        code=code,
        order_type=order_type,
        time_in_force=time_in_force,
        reduce_only=reduce_only,
        close_position=close_position,
        price=price,
        qty=qty,
    )
