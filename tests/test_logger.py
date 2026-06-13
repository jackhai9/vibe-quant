# Input: 日志模块与 pytest 夹具
# Output: 日志行为的 pytest 断言
# Pos: 日志模块测试
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
日志模块单元测试
"""

import os
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal

from src.utils.logger import (
    setup_logger,
    get_logger,
    log_event,
    log_startup,
    log_shutdown,
    log_ws_connect,
    log_ws_disconnect,
    log_ws_reconnect,
    log_market_update,
    log_signal,
    log_order_place,
    log_order_cancel,
    log_order_fill,
    log_order_timeout,
    log_position_update,
    log_error,
    _build_extra_fields,
    _format_value,
)


class TestLoggerSetup:
    """日志设置测试"""

    def test_setup_logger_creates_directory(self):
        """测试日志目录创建"""
        with TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            setup_logger(log_dir, console=False)
            assert log_dir.exists()

    def test_setup_logger_creates_log_files(self):
        """测试日志文件创建"""
        with TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            setup_logger(log_dir, console=False)

            # 写入一条日志
            logger = get_logger()
            logger.info("test message")

            # 检查日志文件是否存在
            log_files = list(log_dir.glob("binance-exit-executor_*.log"))
            assert len(log_files) >= 1

    def test_get_logger_returns_logger(self):
        """测试获取 logger"""
        logger = get_logger()
        assert logger is not None


class TestHelperFunctions:
    """辅助函数测试"""

    def test_format_value_none(self):
        """测试格式化 None"""
        assert _format_value(None) == ""

    def test_format_value_decimal(self):
        """测试格式化 Decimal"""
        assert _format_value(Decimal("123.456")) == "123.456"

    def test_format_value_string(self):
        """测试格式化字符串"""
        assert _format_value("test") == "test"

    def test_format_value_int(self):
        """测试格式化整数"""
        assert _format_value(123) == "123"

    def test_build_extra_fields_empty(self):
        """测试构建空字段"""
        assert _build_extra_fields() == ""

    def test_build_extra_fields_with_none(self):
        """测试构建带 None 的字段"""
        result = _build_extra_fields(a=1, b=None, c="test")
        assert "a=1" in result
        assert "b=" not in result
        assert "c=test" in result

    def test_build_extra_fields_multiple(self):
        """测试构建多个字段"""
        result = _build_extra_fields(symbol="BTC", side="LONG", qty=Decimal("0.1"))
        assert "symbol=BTC" in result
        assert "side=LONG" in result
        assert "qty=0.1" in result


class TestLogEvent:
    """log_event 测试"""

    @pytest.fixture(autouse=True)
    def setup_logger_for_tests(self):
        """每个测试前设置 logger"""
        with TemporaryDirectory() as tmpdir:
            setup_logger(Path(tmpdir), console=False)
            yield

    def test_log_event_basic(self):
        """测试基本事件日志"""
        # 不应抛出异常
        log_event("test_event")

    def test_log_event_with_all_fields(self):
        """测试带所有字段的事件日志"""
        log_event(
            "place",
            symbol="BTC/USDT:USDT",
            side="LONG",
            mode="MAKER_ONLY",
            state="PLACING",
            reason="signal",
            best_bid=Decimal("50000.00"),
            best_ask=Decimal("50000.10"),
            last_trade=Decimal("50000.05"),
            order_id="12345",
            qty=Decimal("0.001"),
            price=Decimal("50000.00"),
            filled_qty=Decimal("0"),
            avg_price=None,
            position_amt=Decimal("0.01"),
        )

    def test_log_event_error(self):
        """测试错误事件日志"""
        log_event("error", error="Connection failed", symbol="BTC/USDT:USDT")


class TestConvenienceFunctions:
    """便捷函数测试"""

    @pytest.fixture(autouse=True)
    def setup_logger_for_tests(self):
        """每个测试前设置 logger"""
        with TemporaryDirectory() as tmpdir:
            setup_logger(Path(tmpdir), console=False)
            yield

    def test_log_startup(self):
        """测试启动日志"""
        log_startup(["BTC/USDT:USDT", "ETH/USDT:USDT"])

    def test_log_shutdown(self):
        """测试关闭日志"""
        log_shutdown("signal received")

    def test_log_ws_connect(self):
        """测试 WS 连接日志"""
        log_ws_connect("bookTicker")

    def test_log_ws_disconnect(self):
        """测试 WS 断开日志"""
        log_ws_disconnect("bookTicker", "connection lost")

    def test_log_ws_reconnect(self):
        """测试 WS 重连日志"""
        log_ws_reconnect("bookTicker", 3)

    def test_log_market_update(self):
        """测试行情更新日志"""
        log_market_update(
            symbol="BTC/USDT:USDT",
            best_bid=Decimal("50000.00"),
            best_ask=Decimal("50000.10"),
            last_trade=Decimal("50000.05"),
        )

    def test_log_signal(self):
        """测试信号日志"""
        log_signal(
            symbol="BTC/USDT:USDT",
            side="LONG",
            reason="long_primary",
            best_bid=Decimal("50000.00"),
            best_ask=Decimal("50000.10"),
            last_trade=Decimal("50000.05"),
        )

    def test_log_order_place(self):
        """测试下单日志"""
        log_order_place(
            symbol="BTC/USDT:USDT",
            side="LONG",
            mode="MAKER_ONLY",
            qty=Decimal("0.001"),
            price=Decimal("50000.00"),
            order_id="12345",
        )

    def test_log_order_cancel(self):
        """测试撤单日志"""
        log_order_cancel(
            symbol="BTC/USDT:USDT",
            order_id="12345",
            reason="timeout",
        )

    def test_log_order_fill(self):
        """测试成交日志"""
        log_order_fill(
            symbol="BTC/USDT:USDT",
            side="LONG",
            order_id="12345",
            filled_qty=Decimal("0.001"),
            avg_price=Decimal("50000.00"),
        )

    def test_log_order_timeout(self):
        """测试超时日志"""
        log_order_timeout(
            symbol="BTC/USDT:USDT",
            side="LONG",
            order_id="12345",
            timeout_count=2,
        )

    def test_log_position_update(self):
        """测试仓位更新日志"""
        log_position_update(
            symbol="BTC/USDT:USDT",
            side="LONG",
            position_amt=Decimal("0.01"),
        )

    def test_log_error(self):
        """测试错误日志"""
        log_error(
            error="Connection timeout",
            symbol="BTC/USDT:USDT",
            extra_field="extra_value",
        )
