"""
配置加载模块

职责：
- 加载 YAML 配置文件
- 支持 global 默认值 + symbol 覆盖
- 从环境变量读取 API 密钥

输入：
- config.yaml 文件路径
- 环境变量 BINANCE_API_KEY, BINANCE_API_SECRET

输出：
- AppConfig 配置对象
- MergedSymbolConfig 合并后的 symbol 配置
"""

import os
from pathlib import Path
from typing import Optional

import yaml

from .models import (
    AppConfig,
    MergedSymbolConfig,
    GlobalConfig,
    SymbolConfig,
)


class ConfigLoader:
    """配置加载器"""

    def __init__(self, config_path: Path):
        """
        初始化配置加载器

        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self._config: Optional[AppConfig] = None
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None

    def load(self) -> AppConfig:
        """
        加载配置文件

        Returns:
            AppConfig 对象

        Raises:
            FileNotFoundError: 配置文件不存在
            yaml.YAMLError: YAML 解析错误
            pydantic.ValidationError: 配置验证错误
        """
        # 读取 YAML 文件
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f) or {}

        # 解析为 pydantic 模型
        self._config = AppConfig(**raw_config)

        # 加载 API 密钥
        self._load_api_keys()

        return self._config

    def _load_api_keys(self) -> None:
        """从环境变量加载 API 密钥"""
        self._api_key = os.environ.get("BINANCE_API_KEY")
        self._api_secret = os.environ.get("BINANCE_API_SECRET")

        if not self._api_key:
            raise ValueError("环境变量 BINANCE_API_KEY 未设置")
        if not self._api_secret:
            raise ValueError("环境变量 BINANCE_API_SECRET 未设置")

    @property
    def api_key(self) -> str:
        """获取 API Key"""
        if self._api_key is None:
            raise ValueError("配置未加载，请先调用 load()")
        return self._api_key

    @property
    def api_secret(self) -> str:
        """获取 API Secret"""
        if self._api_secret is None:
            raise ValueError("配置未加载，请先调用 load()")
        return self._api_secret

    @property
    def config(self) -> AppConfig:
        """获取配置对象"""
        if self._config is None:
            raise ValueError("配置未加载，请先调用 load()")
        return self._config

    def get_symbol_config(self, symbol: str) -> MergedSymbolConfig:
        """
        获取特定 symbol 的配置（合并 global 和 symbol 覆盖）

        Args:
            symbol: 交易对符号（如 "BTC/USDT:USDT"）

        Returns:
            MergedSymbolConfig 合并后的配置对象
        """
        if self._config is None:
            raise ValueError("配置未加载，请先调用 load()")

        global_cfg = self._config.global_
        symbol_cfg = self._config.symbols.get(symbol)

        return self._merge_config(symbol, global_cfg, symbol_cfg)

    def _merge_config(
        self,
        symbol: str,
        global_cfg: GlobalConfig,
        symbol_cfg: Optional[SymbolConfig],
    ) -> MergedSymbolConfig:
        """
        合并 global 配置和 symbol 覆盖

        合并规则：symbol 覆盖优先，如果 symbol 未指定则使用 global 默认值

        Args:
            symbol: 交易对符号
            global_cfg: 全局配置
            symbol_cfg: symbol 级别覆盖配置（可选）

        Returns:
            MergedSymbolConfig 合并后的配置
        """
        # 获取 global 默认值
        g_exec = global_cfg.execution
        g_accel = global_cfg.accel
        g_roi = global_cfg.roi
        g_ws = global_cfg.ws
        g_risk = global_cfg.risk
        g_rate = global_cfg.rate_limit

        # 获取 symbol 覆盖（如果存在）
        s_exec = symbol_cfg.execution if symbol_cfg and symbol_cfg.execution else None
        s_accel = symbol_cfg.accel if symbol_cfg and symbol_cfg.accel else None
        s_roi = symbol_cfg.roi if symbol_cfg and symbol_cfg.roi else None
        s_risk = symbol_cfg.risk if symbol_cfg and symbol_cfg.risk else None

        # 合并执行配置
        order_ttl_ms = _get_override(s_exec, "order_ttl_ms", g_exec.order_ttl_ms)
        repost_cooldown_ms = _get_override(s_exec, "repost_cooldown_ms", g_exec.repost_cooldown_ms)
        min_signal_interval_ms = _get_override(s_exec, "min_signal_interval_ms", g_exec.min_signal_interval_ms)
        base_lot_mult = _get_override(s_exec, "base_lot_mult", g_exec.base_lot_mult)
        maker_price_mode = _get_override(s_exec, "maker_price_mode", g_exec.maker_price_mode)
        maker_n_ticks = _get_override(s_exec, "maker_n_ticks", g_exec.maker_n_ticks)
        maker_safety_ticks = _get_override(s_exec, "maker_safety_ticks", g_exec.maker_safety_ticks)
        max_mult = _get_override(s_exec, "max_mult", g_exec.max_mult)
        max_order_notional = _get_override(s_exec, "max_order_notional", g_exec.max_order_notional)
        maker_timeouts_to_escalate = _get_override(s_exec, "maker_timeouts_to_escalate", g_exec.maker_timeouts_to_escalate)
        aggr_fills_to_deescalate = _get_override(s_exec, "aggr_fills_to_deescalate", g_exec.aggr_fills_to_deescalate)
        aggr_timeouts_to_deescalate = _get_override(s_exec, "aggr_timeouts_to_deescalate", g_exec.aggr_timeouts_to_deescalate)

        # 合并加速配置
        accel_window_ms = _get_override(s_accel, "window_ms", g_accel.window_ms)
        accel_tiers = _get_override(s_accel, "tiers", g_accel.tiers)

        # 合并 ROI 配置
        roi_tiers = _get_override(s_roi, "tiers", g_roi.tiers)

        # 合并风控配置
        liq_distance_threshold = _get_override(s_risk, "liq_distance_threshold", g_risk.liq_distance_threshold)

        # panic_close 嵌套结构
        s_panic = s_risk.panic_close if s_risk and s_risk.panic_close else None
        panic_close_enabled = _get_override(s_panic, "enabled", g_risk.panic_close.enabled)
        panic_close_ttl_percent = _get_override(s_panic, "ttl_percent", g_risk.panic_close.ttl_percent)
        panic_close_tiers = _get_override(s_panic, "tiers", g_risk.panic_close.tiers)

        # protective_stop 嵌套结构
        s_pstop = s_risk.protective_stop if s_risk and s_risk.protective_stop else None
        protective_stop_enabled = _get_override(s_pstop, "enabled", g_risk.protective_stop.enabled)
        protective_stop_dist_to_liq = _get_override(s_pstop, "dist_to_liq", g_risk.protective_stop.dist_to_liq)
        s_et = s_pstop.external_takeover if s_pstop and getattr(s_pstop, "external_takeover", None) else None
        g_et = g_risk.protective_stop.external_takeover
        protective_stop_external_takeover_enabled = _get_override(s_et, "enabled", g_et.enabled)
        protective_stop_external_takeover_rest_verify_interval_s = _get_override(
            s_et, "rest_verify_interval_s", g_et.rest_verify_interval_s
        )
        protective_stop_external_takeover_max_hold_s = _get_override(s_et, "max_hold_s", g_et.max_hold_s)

        return MergedSymbolConfig(
            symbol=symbol,
            # WS
            stale_data_ms=g_ws.stale_data_ms,
            reconnect_initial_delay_ms=g_ws.reconnect.initial_delay_ms,
            reconnect_max_delay_ms=g_ws.reconnect.max_delay_ms,
            reconnect_multiplier=g_ws.reconnect.multiplier,
            # 执行
            order_ttl_ms=order_ttl_ms,
            repost_cooldown_ms=repost_cooldown_ms,
            min_signal_interval_ms=min_signal_interval_ms,
            base_lot_mult=base_lot_mult,
            maker_price_mode=maker_price_mode,
            maker_n_ticks=maker_n_ticks,
            maker_safety_ticks=maker_safety_ticks,
            max_mult=max_mult,
            max_order_notional=max_order_notional,
            maker_timeouts_to_escalate=maker_timeouts_to_escalate,
            aggr_fills_to_deescalate=aggr_fills_to_deescalate,
            aggr_timeouts_to_deescalate=aggr_timeouts_to_deescalate,
            # 加速
            accel_window_ms=accel_window_ms,
            accel_tiers=accel_tiers,
            # ROI
            roi_tiers=roi_tiers,
            # 风控
            liq_distance_threshold=liq_distance_threshold,
            panic_close_enabled=panic_close_enabled,
            panic_close_ttl_percent=panic_close_ttl_percent,
            panic_close_tiers=panic_close_tiers,
            protective_stop_enabled=protective_stop_enabled,
            protective_stop_dist_to_liq=protective_stop_dist_to_liq,
            protective_stop_external_takeover_enabled=protective_stop_external_takeover_enabled,
            protective_stop_external_takeover_rest_verify_interval_s=protective_stop_external_takeover_rest_verify_interval_s,
            protective_stop_external_takeover_max_hold_s=protective_stop_external_takeover_max_hold_s,
            # 限速
            max_orders_per_sec=g_rate.max_orders_per_sec,
            max_cancels_per_sec=g_rate.max_cancels_per_sec,
        )

    def get_symbols(self) -> list[str]:
        """
        获取配置中的所有 symbol 列表

        Returns:
            symbol 列表
        """
        if self._config is None:
            raise ValueError("配置未加载，请先调用 load()")
        return list(self._config.symbols.keys())


def _get_override(symbol_cfg, field: str, default):
    """
    获取覆盖值

    Args:
        symbol_cfg: symbol 级别配置对象（可能为 None）
        field: 字段名
        default: 默认值

    Returns:
        覆盖值或默认值
    """
    if symbol_cfg is None:
        return default
    value = getattr(symbol_cfg, field, None)
    return value if value is not None else default
