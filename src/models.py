# Input: none
# Output: shared enums and dataclasses for module contracts
# Pos: core data contracts, events, and execution state
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
核心数据结构定义

模块间通过这些数据结构传递信息，不直接访问内部状态。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional


# ============================================================
# 枚举类型
# ============================================================

class PositionSide(str, Enum):
    """仓位方向（Hedge 模式）"""
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """订单类型"""
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class TimeInForce(str, Enum):
    """订单有效期"""
    GTC = "GTC"  # Good Till Cancel
    GTX = "GTX"  # Post-Only (maker only)
    IOC = "IOC"  # Immediate Or Cancel
    FOK = "FOK"  # Fill Or Kill


class OrderStatus(str, Enum):
    """订单状态"""
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ExecutionMode(str, Enum):
    """执行模式"""
    MAKER_ONLY = "MAKER_ONLY"
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT"


class ExecutionState(str, Enum):
    """执行状态机状态"""
    IDLE = "IDLE"
    PLACING = "PLACING"
    WAITING = "WAITING"
    CANCELING = "CANCELING"
    COOLDOWN = "COOLDOWN"


class SignalReason(str, Enum):
    """平仓信号触发原因"""
    LONG_PRIMARY = "long_primary"
    LONG_BID_IMPROVE = "long_bid_improve"
    SHORT_PRIMARY = "short_primary"
    SHORT_ASK_IMPROVE = "short_ask_improve"


# ============================================================
# 市场数据
# ============================================================

@dataclass
class MarketEvent:
    """
    市场数据事件（从 WS 接收）

    来源：
    - bookTicker: best_bid, best_ask
    - aggTrade: last_trade_price
    - markPriceUpdate: mark_price
    """
    symbol: str
    timestamp_ms: int
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    last_trade_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    event_type: Literal["book_ticker", "agg_trade", "mark_price"] = "book_ticker"


@dataclass
class MarketState:
    """
    某个 symbol 的市场状态（聚合后）

    由 SignalEngine 维护，用于信号判断。
    """
    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    last_trade_price: Decimal
    previous_trade_price: Optional[Decimal] = None
    last_update_ms: int = 0
    is_ready: bool = False  # 是否有足够数据进行信号判断


# ============================================================
# 仓位数据
# ============================================================

@dataclass
class Position:
    """
    仓位信息（Hedge 模式）
    """
    symbol: str
    position_side: PositionSide
    position_amt: Decimal  # 仓位数量（正数）
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    mark_price: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None


# ============================================================
# 仓位更新（User Data Stream）
# ============================================================

@dataclass
class PositionUpdate:
    """
    仓位更新事件（从 User Data Stream 的 ACCOUNT_UPDATE 接收）

    注：User Data Stream 的仓位字段不一定包含 mark/liquidation/leverage 等信息；
    上层可将其与已有 Position 合并，避免丢失关键信息。
    """
    symbol: str
    position_side: PositionSide
    position_amt: Decimal
    entry_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    timestamp_ms: int = 0


@dataclass
class LeverageUpdate:
    """
    杠杆更新事件（从 User Data Stream 的 ACCOUNT_CONFIG_UPDATE 接收）
    """
    symbol: str
    leverage: int
    timestamp_ms: int = 0


# ============================================================
# 交易规则
# ============================================================

@dataclass
class SymbolRules:
    """
    交易对规则（从 exchange.markets 提取）
    """
    symbol: str
    tick_size: Decimal      # 价格最小变动
    step_size: Decimal      # 数量步进
    min_qty: Decimal        # 最小下单量
    min_notional: Decimal   # 最小名义价值


# ============================================================
# 信号
# ============================================================

@dataclass
class ExitSignal:
    """
    平仓信号（由 SignalEngine 产生）
    """
    symbol: str
    position_side: PositionSide
    reason: SignalReason
    timestamp_ms: int
    best_bid: Decimal
    best_ask: Decimal
    last_trade_price: Decimal
    roi_mult: int = 1
    accel_mult: int = 1
    roi: Optional[Decimal] = None
    ret_window: Optional[Decimal] = None


# ============================================================
# 订单
# ============================================================

@dataclass
class OrderIntent:
    """
    下单意图（由 ExecutionEngine 产生，传给 ExchangeAdapter）
    """
    symbol: str
    side: OrderSide
    position_side: PositionSide
    qty: Decimal
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTX
    reduce_only: bool = True
    close_position: bool = False
    client_order_id: Optional[str] = None
    is_risk: bool = False


@dataclass
class OrderResult:
    """
    下单结果（由 ExchangeAdapter 返回）
    """
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: Optional[OrderStatus] = None
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class OrderUpdate:
    """
    订单更新事件（从 User Data Stream 接收）
    """
    symbol: str
    order_id: str
    client_order_id: str
    side: OrderSide
    position_side: PositionSide
    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal
    timestamp_ms: int
    order_type: Optional[str] = None  # WS 字段 o.o，例如 LIMIT / STOP_MARKET / TAKE_PROFIT_MARKET
    close_position: Optional[bool] = None  # WS 字段 o.cp，closePosition/Close-All
    reduce_only: Optional[bool] = None  # WS 字段 o.R
    is_maker: Optional[bool] = None  # WS 字段 o.m，成交方向（maker/taker）
    realized_pnl: Optional[Decimal] = None  # WS 字段 o.rp，已实现盈亏


# ============================================================
# Algo 条件单（User Data Stream: ALGO_UPDATE）
# ============================================================

@dataclass
class AlgoOrderUpdate:
    """
    Algo 条件单更新事件（从 User Data Stream 接收）

    参考 Binance 文档：User Data Streams - Event Algo Order Update (ALGO_UPDATE)
    """
    symbol: str
    algo_id: str
    client_algo_id: str
    side: OrderSide
    status: str  # NEW / CANCELED / TRIGGERING / TRIGGERED / FINISHED / REJECTED / EXPIRED ...
    timestamp_ms: int
    order_type: Optional[str] = None  # WS 字段 o.o，例如 STOP / TAKE_PROFIT
    position_side: Optional[PositionSide] = None  # WS 字段 o.ps，可能为 BOTH
    close_position: Optional[bool] = None  # WS 字段 o.cp，If Close-All
    reduce_only: Optional[bool] = None  # WS 字段 o.R

# ============================================================
# 执行状态
# ============================================================

@dataclass
class SideExecutionState:
    """
    单侧（LONG 或 SHORT）的执行状态

    每个 symbol + position_side 维护一个独立状态。
    """
    symbol: str
    position_side: PositionSide
    state: ExecutionState = ExecutionState.IDLE
    mode: ExecutionMode = ExecutionMode.MAKER_ONLY

    # 当前订单
    current_order_id: Optional[str] = None
    current_order_placed_ms: int = 0
    current_order_mode: Optional[ExecutionMode] = None
    current_order_reason: Optional[str] = None
    current_order_is_risk: bool = False
    current_order_filled_qty: Decimal = Decimal("0")

    # 已完成订单缓存（用于接收迟到的 WS 成交回执）
    last_completed_order_id: Optional[str] = None
    last_completed_ms: int = 0
    pending_fill_log: bool = False
    last_completed_filled_qty: Decimal = Decimal("0")
    last_completed_avg_price: Decimal = Decimal("0")
    last_completed_mode: Optional["ExecutionMode"] = None
    last_completed_reason: Optional[str] = None
    last_completed_realized_pnl: Optional[Decimal] = None

    # 风控兜底（panic close）覆盖项：仅在 risk_active 时生效
    risk_active: bool = False
    ttl_ms_override: Optional[int] = None
    maker_timeouts_to_escalate_override: Optional[int] = None

    # 计数器（用于模式轮转）
    maker_timeout_count: int = 0
    aggr_timeout_count: int = 0
    aggr_fill_count: int = 0

    # 上次信号时间（用于节流）
    last_signal_ms: int = 0

    # 冷却结束时间
    cooldown_until_ms: int = 0


# ============================================================
# 风控（RiskManager 输出）
# ============================================================

@dataclass
class RiskFlag:
    """
    风险标记（由 RiskManager 产生）
    """
    symbol: str
    position_side: PositionSide
    is_triggered: bool = False
    dist_to_liq: Optional[Decimal] = None
    reason: Optional[str] = None
