# Input: src.models enums and dataclasses
# Output: package re-exports
# Pos: src package initializer
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
binance-exit-executor: Binance U 本位永续 Hedge 模式 Reduce-Only 小单平仓执行器
"""

from src.models import (
    # 枚举
    PositionSide,
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    ExecutionMode,
    ExecutionState,
    SignalReason,
    # 数据结构
    MarketEvent,
    MarketState,
    Position,
    PositionUpdate,
    LeverageUpdate,
    SymbolRules,
    ExitSignal,
    OrderIntent,
    OrderResult,
    OrderUpdate,
    SideExecutionState,
    RiskFlag,
)

__all__ = [
    # 枚举
    "PositionSide",
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderStatus",
    "ExecutionMode",
    "ExecutionState",
    "SignalReason",
    # 数据结构
    "MarketEvent",
    "MarketState",
    "Position",
    "PositionUpdate",
    "LeverageUpdate",
    "SymbolRules",
    "ExitSignal",
    "OrderIntent",
    "OrderResult",
    "OrderUpdate",
    "SideExecutionState",
    "RiskFlag",
]
