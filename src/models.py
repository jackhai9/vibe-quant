# Input: none
# Output: shared enums and dataclasses for module contracts, account events, execution feedback, reduce-only block state, liq-distance risk latch state, and pressure jitter/burst pacing metadata
# Pos: core data contracts, events, per-side execution state, same-side open-order block metadata, liq-distance risk latch metadata, and pressure anti-repeat/burst pacing state
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
核心数据结构定义

模块间通过这些数据结构传递信息，不直接访问内部状态。
"""

from dataclasses import dataclass, field
from collections import deque
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional, Deque, Dict


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


class StrategyMode(str, Enum):
    """运行时策略模式"""
    ORDERBOOK_PRICE = "orderbook_price"
    ORDERBOOK_PRESSURE = "orderbook_pressure"


class SignalExecutionPreference(str, Enum):
    """信号期望的执行偏好"""
    PASSIVE = "passive"
    AGGRESSIVE = "aggressive"


class QtyPolicy(str, Enum):
    """信号数量策略"""
    DYNAMIC = "dynamic"
    FIXED_MIN_QTY_MULT = "fixed_min_qty_mult"


class SignalReason(str, Enum):
    """平仓信号触发原因"""
    LONG_PRIMARY = "long_primary"
    LONG_BID_IMPROVE = "long_bid_improve"
    SHORT_PRIMARY = "short_primary"
    SHORT_ASK_IMPROVE = "short_ask_improve"
    LONG_BID_PRESSURE_ACTIVE = "long_bid_pressure_active"
    SHORT_ASK_PRESSURE_ACTIVE = "short_ask_pressure_active"
    LONG_ASK_PRESSURE_PASSIVE = "long_ask_pressure_passive"
    SHORT_BID_PRESSURE_PASSIVE = "short_bid_pressure_passive"


# ============================================================
# 市场数据
# ============================================================

@dataclass
class MarketEvent:
    """
    市场数据事件（从 WS 接收）

    来源：
    - bookTicker: best_bid, best_ask, best_bid_qty, best_ask_qty
    - depth: bid_levels, ask_levels
    - aggTrade: last_trade_price
    - markPriceUpdate: mark_price
    """
    symbol: str
    timestamp_ms: int
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    best_bid_qty: Optional[Decimal] = None
    best_ask_qty: Optional[Decimal] = None
    bid_levels: Optional[list[tuple[Decimal, Decimal]]] = None
    ask_levels: Optional[list[tuple[Decimal, Decimal]]] = None
    last_trade_price: Optional[Decimal] = None
    trade_qty: Optional[Decimal] = None
    is_buyer_maker: Optional[bool] = None
    mark_price: Optional[Decimal] = None
    event_type: Literal["book_ticker", "depth", "agg_trade", "mark_price"] = "book_ticker"


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
    best_bid_qty: Decimal = Decimal("0")
    best_ask_qty: Decimal = Decimal("0")
    bid_levels: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    ask_levels: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    previous_trade_price: Optional[Decimal] = None
    last_update_ms: int = 0
    last_book_ticker_ms: int = 0
    last_depth_update_ms: int = 0
    last_trade_update_ms: int = 0
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
class AccountUpdateEvent:
    """
    账户更新事件（从 User Data Stream 的 ACCOUNT_UPDATE 接收）

    用途：
    - 识别账户资产侧事件（如划转保证金），触发上层 REST 刷新仓位
    """
    reason: Optional[str] = None
    timestamp_ms: int = 0
    has_balance_delta: bool = False
    balance_delta_assets: tuple[str, ...] = ()
    has_position_delta: bool = False


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
    strategy_mode: StrategyMode = StrategyMode.ORDERBOOK_PRICE
    execution_preference: SignalExecutionPreference = SignalExecutionPreference.PASSIVE
    qty_policy: QtyPolicy = QtyPolicy.DYNAMIC
    price_override: Optional[Decimal] = None
    ttl_override_ms: Optional[int] = None
    cooldown_override_ms: Optional[int] = None
    base_mult_override: Optional[int] = None
    fixed_qty_jitter_pct: Optional[Decimal] = None
    fixed_qty_anti_repeat_lookback: Optional[int] = None
    active_burst_window_ms: Optional[int] = None
    active_burst_max_attempts: Optional[int] = None
    active_burst_max_fills: Optional[int] = None
    active_burst_pause_min_ms: Optional[int] = None
    active_burst_pause_max_ms: Optional[int] = None
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
    fee: Optional[Decimal] = None  # WS 字段 o.n，手续费
    fee_asset: Optional[str] = None  # WS 字段 o.N，手续费资产


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


@dataclass
class ReduceOnlyBlockInfo:
    """
    `-4118 ReduceOnly Order Failed` 的同侧挂单占仓确认结果。

    语义：
    - `position_amt` 保留当前仓位原始符号（SHORT 为负）
    - `tradable_position_amt` 为按 step/minQty 规整后的本轮可平数量
    - `blocking_*` 只统计同 symbol + 同 positionSide + 同平仓方向的普通挂单剩余量
    """
    symbol: str
    position_side: PositionSide
    position_amt: Decimal
    tradable_position_amt: Decimal
    blocking_qty: Decimal
    blocking_order_count: int
    own_blocking_qty: Decimal = Decimal("0")
    own_blocking_order_count: int = 0
    external_blocking_qty: Decimal = Decimal("0")
    external_blocking_order_count: int = 0


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
    current_order_execution_preference: Optional[SignalExecutionPreference] = None
    current_order_strategy_mode: Optional[StrategyMode] = None
    current_order_ttl_ms_override: Optional[int] = None
    current_order_cooldown_ms_override: Optional[int] = None
    current_order_terminal_grace_until_ms: int = 0
    current_order_cancel_retry_after_ms: int = 0

    # 已完成订单缓存（用于接收迟到的 WS 成交回执）
    last_completed_order_id: Optional[str] = None
    last_completed_ms: int = 0
    pending_fill_log: bool = False
    last_completed_filled_qty: Decimal = Decimal("0")
    last_completed_avg_price: Decimal = Decimal("0")
    last_completed_mode: Optional["ExecutionMode"] = None
    last_completed_reason: Optional[str] = None
    last_completed_realized_pnl: Optional[Decimal] = None
    last_completed_fee: Optional[Decimal] = None
    last_completed_fee_asset: Optional[str] = None

    # 风控兜底（panic close）覆盖项：仅在 risk_active 时生效
    risk_active: bool = False
    ttl_ms_override: Optional[int] = None
    maker_timeouts_to_escalate_override: Optional[int] = None

    # 一级风控（liq_distance）锁存：用于风险区内维持 AGGRESSIVE_LIMIT 并控制日志/通知频率
    liq_distance_active: bool = False
    liq_distance_reason: Optional[str] = None

    # 计数器（用于模式轮转）
    maker_timeout_count: int = 0
    aggr_timeout_count: int = 0
    aggr_fill_count: int = 0

    # 上次信号时间（用于节流）
    last_signal_ms: int = 0

    # 冷却结束时间
    cooldown_until_ms: int = 0

    # 成交率反馈（maker 提交/成交）
    recent_maker_submits: Deque[int] = field(default_factory=deque)
    recent_maker_fills: Deque[int] = field(default_factory=deque)
    maker_submit_ts_by_order_id: Dict[str, int] = field(default_factory=dict)
    fill_rate: Optional[Decimal] = None
    fill_rate_bucket: Optional[str] = None
    fill_rate_ttl_override: Optional[int] = None

    # 固定片大小 anti-repeat：仅用于 orderbook_pressure 的固定数量路径
    recent_fixed_order_qtys: Deque[Decimal] = field(default_factory=lambda: deque(maxlen=16))

    # `-4118` 后的“同侧平仓挂单已占满可交易仓位”锁存
    reduce_only_block: Optional[ReduceOnlyBlockInfo] = None
    reduce_only_block_recheck_after_ms: int = 0


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
