"""
配置模型定义（pydantic）

职责：
- 定义配置结构的类型验证
- 提供默认值
- 支持 global + symbol 覆盖
"""

from decimal import Decimal
from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# 子配置模型
# ============================================================

class ReconnectConfig(BaseModel):
    """WS 重连配置"""
    initial_delay_ms: int = Field(default=1000, description="初始重连延迟(ms)")
    max_delay_ms: int = Field(default=30000, description="最大重连延迟(ms)")
    multiplier: int = Field(default=2, description="延迟倍数")


class WSConfig(BaseModel):
    """WebSocket 配置"""
    stale_data_ms: int = Field(default=1500, description="数据陈旧阈值(ms)")
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)


class AccelTier(BaseModel):
    """加速档位"""
    ret: Decimal = Field(description="回报率阈值")
    mult: int = Field(description="倍数")


class AccelConfig(BaseModel):
    """加速配置"""
    window_ms: int = Field(default=2000, description="滑动窗口(ms)")
    tiers: List[AccelTier] = Field(default_factory=list, description="加速档位（LONG/SHORT 共用，方向自动处理）")


class RoiTier(BaseModel):
    """ROI 档位"""
    roi: Decimal = Field(description="ROI 阈值")
    mult: int = Field(description="倍数")


class RoiConfig(BaseModel):
    """ROI 配置"""
    tiers: List[RoiTier] = Field(default_factory=list, description="ROI 档位")


class PanicCloseTier(BaseModel):
    """强平兜底：分级强制平仓档位"""
    dist_to_liq: Decimal = Field(gt=Decimal("0"), description="强平距离阈值（dist_to_liq <= dist_to_liq 触发）")
    slice_ratio: Decimal = Field(gt=Decimal("0"), le=Decimal("1"), description="每次强制平仓的仓位比例（0~1）")
    maker_timeouts_to_escalate: int = Field(default=2, ge=1, description="maker 连续超时后升级阈值")

class PanicCloseConfig(BaseModel):
    """强平兜底：分级强制平仓配置"""
    enabled: bool = Field(default=False, description="是否启用强制平仓兜底（独立于信号）")
    ttl_percent: Decimal = Field(
        default=Decimal("0.5"),
        gt=Decimal("0"),
        le=Decimal("1"),
        description="强制平仓 TTL = execution.order_ttl_ms × ttl_percent（固定比例）",
    )
    tiers: List[PanicCloseTier] = Field(default_factory=list, description="按 dist_to_liq 分级的强制平仓档位")

class ProtectiveStopConfig(BaseModel):
    """仓位保护性止损：交易所端条件单兜底（防程序崩溃/休眠/断网）"""
    enabled: bool = Field(default=True, description="是否启用保护性止损（STOP_MARKET close）")
    dist_to_liq: Decimal = Field(
        default=Decimal("0.01"),
        gt=Decimal("0"),
        le=Decimal("1"),
        description="止损触发距离：使触发时 dist_to_liq≈dist_to_liq（按 liquidation_price 反推 stopPrice）",
    )
    class ExternalTakeoverConfig(BaseModel):
        """外部止损接管（手动/其他端）"""
        enabled: bool = Field(default=True, description="是否启用外部止损接管锁存")
        rest_verify_interval_s: int = Field(default=30, ge=1, description="锁存期间的 REST 校验间隔(s)")
        max_hold_s: int = Field(default=300, ge=1, description="锁存最长持续时间(s)，超时后触发 REST 校验兜底")

    external_takeover: ExternalTakeoverConfig = Field(default_factory=ExternalTakeoverConfig)

class RiskConfig(BaseModel):
    """风控配置"""
    liq_distance_threshold: Decimal = Field(
        default=Decimal("0.015"),
        description="强平距离阈值"
    )
    panic_close: PanicCloseConfig = Field(default_factory=PanicCloseConfig)
    protective_stop: ProtectiveStopConfig = Field(default_factory=ProtectiveStopConfig)


class RateLimitConfig(BaseModel):
    """限速配置"""
    max_orders_per_sec: int = Field(default=5, description="每秒最大下单数")
    max_cancels_per_sec: int = Field(default=8, description="每秒最大撤单数")


class TelegramEventsConfig(BaseModel):
    """Telegram 事件配置"""
    on_fill: bool = Field(default=True, description="成交通知")
    on_reconnect: bool = Field(default=True, description="重连通知")
    on_risk_trigger: bool = Field(default=True, description="风险触发通知")
    on_open_alert: bool = Field(default=True, description="开仓/加仓告警")


class TelegramConfig(BaseModel):
    """Telegram 配置（token/chat_id 从环境变量读取）"""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, description="是否启用")
    events: TelegramEventsConfig = Field(default_factory=TelegramEventsConfig)


class ExecutionConfig(BaseModel):
    """执行配置"""
    # 时序
    order_ttl_ms: int = Field(default=800, description="订单 TTL(ms)")
    repost_cooldown_ms: int = Field(default=100, description="撤单后冷却(ms)")
    min_signal_interval_ms: int = Field(default=200, description="最小信号间隔(ms)")
    base_lot_mult: int = Field(default=1, description="基础片大小倍数")

    # maker 定价策略
    maker_price_mode: Literal["at_touch", "inside_spread_1tick", "custom_ticks"] = Field(
        default="inside_spread_1tick",
        description="maker 定价模式"
    )
    maker_n_ticks: int = Field(default=1, description="custom_ticks 模式的 tick 数")
    maker_safety_ticks: int = Field(
        default=1,
        ge=1,
        description="post-only maker 安全距离（ticks，默认 1）",
    )

    # 双保险
    max_mult: int = Field(default=50, description="最大倍数")
    max_order_notional: Decimal = Field(
        default=Decimal("200"),
        description="最大订单名义价值(USDT)"
    )

    # 模式轮转
    maker_timeouts_to_escalate: int = Field(default=2, description="升级到激进限价的超时次数")
    aggr_fills_to_deescalate: int = Field(default=1, description="降级到 maker 的成交次数")
    aggr_timeouts_to_deescalate: int = Field(default=2, description="降级到 maker 的超时次数")


# ============================================================
# Symbol 级别覆盖配置
# ============================================================

class SymbolExecutionConfig(BaseModel):
    """Symbol 级别执行配置覆盖（所有字段可选）"""
    order_ttl_ms: Optional[int] = None
    repost_cooldown_ms: Optional[int] = None
    min_signal_interval_ms: Optional[int] = None
    base_lot_mult: Optional[int] = None
    maker_price_mode: Optional[Literal["at_touch", "inside_spread_1tick", "custom_ticks"]] = None
    maker_n_ticks: Optional[int] = None
    maker_safety_ticks: Optional[int] = Field(default=None, ge=1)
    max_mult: Optional[int] = None
    max_order_notional: Optional[Decimal] = None
    maker_timeouts_to_escalate: Optional[int] = None
    aggr_fills_to_deescalate: Optional[int] = None
    aggr_timeouts_to_deescalate: Optional[int] = None


class SymbolAccelConfig(BaseModel):
    """Symbol 级别加速配置覆盖"""
    window_ms: Optional[int] = None
    tiers: Optional[List[AccelTier]] = None


class SymbolRoiConfig(BaseModel):
    """Symbol 级别 ROI 配置覆盖"""
    tiers: Optional[List[RoiTier]] = None


class SymbolPanicCloseConfig(BaseModel):
    """Symbol 级别强制平仓覆盖（所有字段可选）"""
    enabled: Optional[bool] = None
    ttl_percent: Optional[Decimal] = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    tiers: Optional[List[PanicCloseTier]] = None


class SymbolProtectiveStopConfig(BaseModel):
    """Symbol 级别保护性止损覆盖（所有字段可选）"""
    enabled: Optional[bool] = None
    dist_to_liq: Optional[Decimal] = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    class SymbolExternalTakeoverConfig(BaseModel):
        enabled: Optional[bool] = None
        rest_verify_interval_s: Optional[int] = Field(default=None, ge=1)
        max_hold_s: Optional[int] = Field(default=None, ge=1)

    external_takeover: Optional[SymbolExternalTakeoverConfig] = None


class SymbolRiskConfig(BaseModel):
    """Symbol 级别风控配置覆盖（结构与 global.risk 一致）"""
    liq_distance_threshold: Optional[Decimal] = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    panic_close: Optional[SymbolPanicCloseConfig] = None
    protective_stop: Optional[SymbolProtectiveStopConfig] = None


class SymbolConfig(BaseModel):
    """单个 symbol 的覆盖配置"""
    execution: Optional[SymbolExecutionConfig] = None
    accel: Optional[SymbolAccelConfig] = None
    roi: Optional[SymbolRoiConfig] = None
    risk: Optional[SymbolRiskConfig] = None


# ============================================================
# 全局配置
# ============================================================

class GlobalConfig(BaseModel):
    """全局配置（global 部分）"""
    testnet: bool = False  # 是否使用测试网
    proxy: Optional[str] = None  # HTTP 代理地址，如 "http://127.0.0.1:7890"
    ws: WSConfig = Field(default_factory=WSConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    accel: AccelConfig = Field(default_factory=AccelConfig)
    roi: RoiConfig = Field(default_factory=RoiConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class AppConfig(BaseModel):
    """应用配置（完整配置文件）"""
    model_config = ConfigDict(populate_by_name=True)

    global_: GlobalConfig = Field(default_factory=GlobalConfig, alias="global")
    symbols: Dict[str, SymbolConfig] = Field(default_factory=dict)


# ============================================================
# 合并后的 Symbol 配置（用于运行时）
# ============================================================

class MergedSymbolConfig(BaseModel):
    """
    合并后的 symbol 配置

    global 默认值 + symbol 覆盖 = 最终配置
    """
    symbol: str

    # WS
    stale_data_ms: int
    reconnect_initial_delay_ms: int
    reconnect_max_delay_ms: int
    reconnect_multiplier: int

    # 执行
    order_ttl_ms: int
    repost_cooldown_ms: int
    min_signal_interval_ms: int
    base_lot_mult: int
    maker_price_mode: str
    maker_n_ticks: int
    maker_safety_ticks: int
    max_mult: int
    max_order_notional: Decimal
    maker_timeouts_to_escalate: int
    aggr_fills_to_deescalate: int
    aggr_timeouts_to_deescalate: int

    # 加速
    accel_window_ms: int
    accel_tiers: List[AccelTier]

    # ROI
    roi_tiers: List[RoiTier]

    # 风控
    liq_distance_threshold: Decimal
    panic_close_enabled: bool
    panic_close_ttl_percent: Decimal
    panic_close_tiers: List[PanicCloseTier]
    protective_stop_enabled: bool
    protective_stop_dist_to_liq: Decimal
    protective_stop_external_takeover_enabled: bool
    protective_stop_external_takeover_rest_verify_interval_s: int
    protective_stop_external_takeover_max_hold_s: int

    # 限速
    max_orders_per_sec: int
    max_cancels_per_sec: int
