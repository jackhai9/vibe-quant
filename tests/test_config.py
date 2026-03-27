# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
配置模块单元测试
"""

import os
import pytest
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.config import ConfigLoader, AppConfig, MergedSymbolConfig
from src.config.models import SymbolConfig, StrategyConfig, PressureExitConfig


class TestConfigLoader:
    """ConfigLoader 测试"""

    @pytest.fixture
    def sample_config_yaml(self):
        """创建临时配置文件"""
        content = """
global:
  ws:
    stale_data_ms: 2000
    reconnect:
      initial_delay_ms: 500
      max_delay_ms: 60000
      multiplier: 3
  execution:
    order_ttl_ms: 1000
    repost_cooldown_ms: 150
    min_signal_interval_ms: 250
    default_base_mult: 2
    use_roi_mult: true
    use_accel_mult: true
    maker_price_mode: "at_touch"
    maker_n_ticks: 2
    maker_safety_ticks: 2
    max_mult: 100
    max_order_notional: 500
    maker_timeouts_to_escalate: 3
    aggr_fills_to_deescalate: 2
    aggr_timeouts_to_deescalate: 3
  accel:
    window_ms: 3000
    tiers: []
  roi:
    tiers: []
  risk:
    liq_distance_threshold: 0.02
    protective_stop:
      enabled: true
      dist_to_liq: 0.01
  rate_limit:
    max_orders_per_sec: 10
    max_cancels_per_sec: 15

symbols:
  BTC/USDT:USDT:
    execution:
      order_ttl_ms: 1500
      use_roi_mult: false
      maker_safety_ticks: 3
      max_mult: 150
      max_order_notional: 1000
    risk:
      protective_stop:
        dist_to_liq: 0.02
  ETH/USDT:USDT:
    execution:
      maker_price_mode: "custom_ticks"
      maker_n_ticks: 3
  DASH/USDT:USDT:
    strategy:
      mode: orderbook_pressure
    pressure_exit:
      threshold_qty: 100
      sustain_ms: 2000
      passive_level: 3
      base_mult: 5
      use_roi_mult: true
      use_accel_mult: true
      qty_jitter_pct: 0.15
      qty_anti_repeat_lookback: 4
      active_recheck_cooldown_ms: 1000
      active_recheck_cooldown_jitter_pct: 0.2
      active_burst_window_ms: 12000
      active_burst_max_attempts: 9
      active_burst_max_fills: 6
      active_burst_pause_min_ms: 3000
      active_burst_pause_max_ms: 7000
      passive_ttl_ms: 10000
      passive_ttl_jitter_pct: 0.25
"""
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            temp_path = f.name
        yield Path(temp_path)
        # 清理
        os.unlink(temp_path)

    @pytest.fixture
    def env_vars(self, monkeypatch):
        """设置环境变量"""
        monkeypatch.setenv("BINANCE_API_KEY", "test_api_key")
        monkeypatch.setenv("BINANCE_API_SECRET", "test_api_secret")

    def test_load_config(self, sample_config_yaml, env_vars):
        """测试加载配置文件"""
        loader = ConfigLoader(sample_config_yaml)
        config = loader.load()

        assert isinstance(config, AppConfig)
        assert config.global_.ws.stale_data_ms == 2000
        assert config.global_.execution.order_ttl_ms == 1000
        assert config.global_.execution.maker_price_mode == "at_touch"
        assert config.global_.execution.maker_safety_ticks == 2

    def test_api_keys(self, sample_config_yaml, env_vars):
        """测试 API 密钥加载"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        assert loader.api_key == "test_api_key"
        assert loader.api_secret == "test_api_secret"

    def test_missing_api_key(self, sample_config_yaml, monkeypatch):
        """测试缺少 API Key"""
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

        loader = ConfigLoader(sample_config_yaml)
        with pytest.raises(ValueError, match="BINANCE_API_KEY"):
            loader.load()

    def test_get_symbols(self, sample_config_yaml, env_vars):
        """测试获取 symbol 列表"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        symbols = loader.get_symbols()
        assert "BTC/USDT:USDT" in symbols
        assert "ETH/USDT:USDT" in symbols
        assert "DASH/USDT:USDT" in symbols
        assert len(symbols) == 3

    def test_get_symbol_config_with_override(self, sample_config_yaml, env_vars):
        """测试获取带覆盖的 symbol 配置"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        # BTC 有自定义配置
        btc_config = loader.get_symbol_config("BTC/USDT:USDT")
        assert isinstance(btc_config, MergedSymbolConfig)
        assert btc_config.symbol == "BTC/USDT:USDT"
        # 覆盖值
        assert btc_config.order_ttl_ms == 1500
        assert btc_config.execution_use_roi_mult is False
        assert btc_config.execution_use_accel_mult is True
        assert btc_config.maker_safety_ticks == 3
        assert btc_config.max_mult == 150
        assert btc_config.max_order_notional == Decimal("1000")
        # 继承 global 值
        assert btc_config.maker_price_mode == "at_touch"  # 继承 global
        assert btc_config.repost_cooldown_ms == 150  # 继承 global

    def test_get_symbol_config_partial_override(self, sample_config_yaml, env_vars):
        """测试获取部分覆盖的 symbol 配置"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        # ETH 只覆盖 maker_price_mode 和 maker_n_ticks
        eth_config = loader.get_symbol_config("ETH/USDT:USDT")
        assert eth_config.maker_price_mode == "custom_ticks"
        assert eth_config.maker_n_ticks == 3
        assert eth_config.maker_safety_ticks == 2
        assert eth_config.execution_use_roi_mult is True
        assert eth_config.execution_use_accel_mult is True
        # 其他继承 global
        assert eth_config.order_ttl_ms == 1000
        assert eth_config.max_mult == 100
        assert eth_config.strategy_mode == "orderbook_price"

    def test_get_symbol_config_no_override(self, sample_config_yaml, env_vars):
        """测试获取没有覆盖的 symbol 配置"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        # 不存在的 symbol 使用完全 global 默认值
        unknown_config = loader.get_symbol_config("DOGE/USDT:USDT")
        assert unknown_config.symbol == "DOGE/USDT:USDT"
        assert unknown_config.order_ttl_ms == 1000  # global 默认
        assert unknown_config.max_mult == 100  # global 默认
        assert unknown_config.maker_price_mode == "at_touch"  # global 默认
        assert unknown_config.maker_safety_ticks == 2  # global 默认
        assert unknown_config.execution_use_roi_mult is True
        assert unknown_config.execution_use_accel_mult is True
        assert unknown_config.strategy_mode == "orderbook_price"
        assert unknown_config.pressure_exit_enabled is False

    def test_get_symbol_config_pressure_exit_mode(self, sample_config_yaml, env_vars):
        """测试盘口量模式按 symbol 启用且 orderbook_price 仍为默认。"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        dash_config = loader.get_symbol_config("DASH/USDT:USDT")
        assert dash_config.strategy_mode == "orderbook_pressure"
        assert dash_config.pressure_exit_enabled is True
        assert dash_config.pressure_exit_threshold_qty == Decimal("100")
        assert dash_config.pressure_exit_sustain_ms == 2000
        assert dash_config.pressure_exit_passive_level == 3
        assert dash_config.pressure_exit_base_mult == 5
        assert dash_config.pressure_exit_use_roi_mult is True
        assert dash_config.pressure_exit_use_accel_mult is True
        assert dash_config.pressure_exit_active_recheck_cooldown_ms == 1000
        assert dash_config.pressure_exit_active_recheck_cooldown_jitter_pct == Decimal("0.2")
        assert dash_config.pressure_exit_active_burst_window_ms == 12000
        assert dash_config.pressure_exit_active_burst_max_attempts == 9
        assert dash_config.pressure_exit_active_burst_max_fills == 6
        assert dash_config.pressure_exit_active_burst_pause_min_ms == 3000
        assert dash_config.pressure_exit_active_burst_pause_max_ms == 7000
        assert dash_config.pressure_exit_passive_ttl_ms == 10000
        assert dash_config.pressure_exit_passive_ttl_jitter_pct == Decimal("0.25")
        assert dash_config.pressure_exit_qty_jitter_pct == Decimal("0.15")
        assert dash_config.pressure_exit_qty_anti_repeat_lookback == 4

        btc_config = loader.get_symbol_config("BTC/USDT:USDT")
        assert btc_config.strategy_mode == "orderbook_price"
        assert btc_config.pressure_exit_enabled is False

    def test_pressure_exit_rejects_invalid_active_burst_pause_bounds(self):
        with pytest.raises(ValueError, match="active_burst_pause_max_ms must be >="):
            PressureExitConfig(
                threshold_qty=Decimal("100"),
                active_burst_pause_min_ms=6000,
                active_burst_pause_max_ms=3000,
            )

    def test_pressure_exit_rejects_mode_orderbook_pressure_with_enabled_false(self):
        """strategy.mode=orderbook_pressure + pressure_exit.enabled=false 应被拒绝。"""
        with pytest.raises(ValueError, match="enabled 不能为 false"):
            SymbolConfig(
                strategy=StrategyConfig(mode="orderbook_pressure"),
                pressure_exit=PressureExitConfig(
                    enabled=False,
                    threshold_qty=Decimal("100"),
                ),
            )

    def test_ws_config(self, sample_config_yaml, env_vars):
        """测试 WS 配置合并"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        config = loader.get_symbol_config("BTC/USDT:USDT")
        assert config.stale_data_ms == 2000
        assert config.reconnect_initial_delay_ms == 500
        assert config.reconnect_max_delay_ms == 60000
        assert config.reconnect_multiplier == 3

    def test_risk_config(self, sample_config_yaml, env_vars):
        """测试风控配置合并"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        config = loader.get_symbol_config("BTC/USDT:USDT")
        assert config.liq_distance_threshold == Decimal("0.02")
        assert config.protective_stop_enabled is True
        assert config.protective_stop_dist_to_liq == Decimal("0.02")

    def test_rate_limit_config(self, sample_config_yaml, env_vars):
        """测试限速配置合并"""
        loader = ConfigLoader(sample_config_yaml)
        loader.load()

        config = loader.get_symbol_config("BTC/USDT:USDT")
        assert config.max_orders_per_sec == 10
        assert config.max_cancels_per_sec == 15

    def test_symbol_accel_mult_percent_scales_global_tiers(self, env_vars):
        """测试 symbol.accel.mult_percent 缩放 global tiers（向上取整 + 最小 1）"""
        content = """
global:
  accel:
    window_ms: 2000
    tiers:
      - { ret: 0.001, mult: 1 }
      - { ret: 0.003, mult: 3 }
symbols:
  BTC/USDT:USDT:
    accel:
      mult_percent: 0.5
  ETH/USDT:USDT:
    accel:
      tiers:
        - { ret: 0.002, mult: 9 }
      mult_percent: 0.5
"""
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            loader = ConfigLoader(Path(temp_path))
            loader.load()

            btc_config = loader.get_symbol_config("BTC/USDT:USDT")
            assert [(t.ret, t.mult) for t in btc_config.accel_tiers] == [
                (Decimal("0.001"), 1),
                (Decimal("0.003"), 2),
            ]

            eth_config = loader.get_symbol_config("ETH/USDT:USDT")
            assert [(t.ret, t.mult) for t in eth_config.accel_tiers] == [
                (Decimal("0.002"), 9),
            ]
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self, env_vars):
        """测试配置文件不存在"""
        loader = ConfigLoader(Path("/nonexistent/config.yaml"))
        with pytest.raises(FileNotFoundError):
            loader.load()


class TestDefaultConfig:
    """测试默认配置值"""

    @pytest.fixture
    def minimal_config_yaml(self):
        """最小配置文件（仅空 global）"""
        content = """
global: {}
symbols: {}
"""
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            temp_path = f.name
        yield Path(temp_path)
        os.unlink(temp_path)

    @pytest.fixture
    def env_vars(self, monkeypatch):
        """设置环境变量"""
        monkeypatch.setenv("BINANCE_API_KEY", "test_api_key")
        monkeypatch.setenv("BINANCE_API_SECRET", "test_api_secret")

    def test_default_values(self, minimal_config_yaml, env_vars):
        """测试所有默认值"""
        loader = ConfigLoader(minimal_config_yaml)
        loader.load()

        config = loader.get_symbol_config("ANY/USDT:USDT")

        # WS 默认值
        assert config.stale_data_ms == 1500
        assert config.reconnect_initial_delay_ms == 1000
        assert config.reconnect_max_delay_ms == 30000
        assert config.reconnect_multiplier == 2

        # 执行默认值
        assert config.order_ttl_ms == 800
        assert config.repost_cooldown_ms == 100
        assert config.min_signal_interval_ms == 200
        assert config.default_base_mult == 1
        assert config.execution_use_roi_mult is True
        assert config.execution_use_accel_mult is True
        assert config.maker_price_mode == "inside_spread_1tick"
        assert config.maker_n_ticks == 1
        assert config.maker_safety_ticks == 1
        assert config.max_mult == 50
        assert config.max_order_notional == Decimal("200")
        assert config.maker_timeouts_to_escalate == 2
        assert config.aggr_fills_to_deescalate == 1
        assert config.aggr_timeouts_to_deescalate == 2

        # 加速默认值
        assert config.accel_window_ms == 2000
        assert config.accel_tiers == []

        # ROI 默认值
        assert config.roi_tiers == []

        # 风控默认值
        assert config.liq_distance_threshold == Decimal("0.015")
        assert config.protective_stop_enabled is True
        assert config.protective_stop_dist_to_liq == Decimal("0.01")

        # 限速默认值
        assert config.max_orders_per_sec == 5
        assert config.max_cancels_per_sec == 8
