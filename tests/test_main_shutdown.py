# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例（main.py 关闭行为 + 命令解析 + 保护止损调度竞态）
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
main.py 应用关闭行为 & 命令解析 & 保护止损调度竞态测试
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import Application
from src.notify.pause_manager import PauseManager
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class DummyWS:
    def __init__(self):
        self.connect_started = False
        self.disconnect_called = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self.connect_started = True
        await self._block.wait()

    async def disconnect(self) -> None:
        self.disconnect_called = True
        self._block.set()


@pytest.mark.asyncio
async def test_run_should_exit_on_shutdown_without_blocking_ws_connect(monkeypatch):
    app = Application(Path("config/config.yaml"))
    app.market_ws = DummyWS()  # type: ignore[assignment]
    app.user_data_ws = DummyWS()  # type: ignore[assignment]
    # run() 只有在存在 active_symbols 时才会触发 market ws rebuild/连接
    app._active_symbols = {"BTC/USDT:USDT"}

    async def noop() -> None:
        return

    async def wait_shutdown() -> None:
        await app._shutdown_event.wait()

    app._fetch_positions = noop  # type: ignore[method-assign]

    async def fake_rebuild_market_ws(*args, **kwargs) -> None:
        # 模拟真实 _rebuild_market_ws：启动 connect 任务但不阻塞 run()
        app._market_ws_task = asyncio.create_task(app.market_ws.connect())  # type: ignore[union-attr]

    app._rebuild_market_ws = fake_rebuild_market_ws  # type: ignore[method-assign]

    async def noop_cancel(reason: str) -> None:
        return

    app._cancel_own_orders = noop_cancel  # type: ignore[method-assign]
    app._main_loop = wait_shutdown  # type: ignore[method-assign]
    app._timeout_check_loop = wait_shutdown  # type: ignore[method-assign]

    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float, result=None):
        await original_sleep(0)
        return result

    import src.main as main_module
    monkeypatch.setattr(main_module.asyncio, "sleep", fast_sleep)

    async def trigger_shutdown() -> None:
        await original_sleep(0)
        app.request_shutdown()

    asyncio.create_task(trigger_shutdown())

    await asyncio.wait_for(app.run(), timeout=1.0)

    assert app.market_ws.connect_started is True  # type: ignore[union-attr]
    assert app.market_ws.disconnect_called is True  # type: ignore[union-attr]
    assert app.user_data_ws.connect_started is True  # type: ignore[union-attr]
    assert app.user_data_ws.disconnect_called is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_main_loop_spawns_side_tasks_and_shutdown_cancels_them():
    app = Application(Path("config/config.yaml"))
    app._running = True
    app._active_symbols = {"BTC/USDT:USDT", "ETH/USDT:USDT"}

    class DummyConfigLoader:
        def get_symbols(self):
            return ["BTC/USDT:USDT", "ETH/USDT:USDT"]

    app.config_loader = DummyConfigLoader()  # type: ignore[assignment]

    main_loop_task = asyncio.create_task(app._main_loop())
    app._main_loop_task = main_loop_task

    await asyncio.sleep(0)
    assert len(app._side_tasks) == 4  # 2 symbols × (LONG+SHORT)

    await asyncio.wait_for(app.shutdown(), timeout=2.0)
    assert len(app._side_tasks) == 0


def test_protective_stop_debounce_classification():
    assert Application._protective_stop_debounce_s("position_update:LONG") == 1.0
    assert Application._protective_stop_debounce_s("startup") == 0.0
    assert Application._protective_stop_debounce_s("calibration:user_data") == 0.0
    assert Application._protective_stop_debounce_s("order_update:FILLED") == 0.2
    assert Application._protective_stop_debounce_s("our_algo:CANCELED") == 0.2


# ================================================================
# _resolve_symbol 测试
# ================================================================

def _make_app_with_symbols(symbols: set[str]) -> Application:
    """构造一个带 _active_symbols 的 Application（不做真实初始化）。"""
    app = Application.__new__(Application)
    app._active_symbols = symbols
    return app


def test_resolve_symbol_exact_ccxt_format():
    """精确匹配 ccxt 格式"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_resolve_symbol_compact_format():
    """简写匹配 BTCUSDT"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTCUSDT") == "BTC/USDT:USDT"


def test_resolve_symbol_base_only():
    """base 币种匹配 BTC（唯一命中）"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    assert app._resolve_symbol("BTC") == "BTC/USDT:USDT"


def test_resolve_symbol_base_case_insensitive():
    """base 币种匹配大小写不敏感"""
    app = _make_app_with_symbols({"DASH/USDT:USDT"})
    assert app._resolve_symbol("dash") == "DASH/USDT:USDT"


def test_resolve_symbol_base_ambiguous():
    """base 币种歧义时返回 None（假设存在 BTC/USDT:USDT 和 BTC/BUSD:BUSD）"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "BTC/BUSD:BUSD"})
    assert app._resolve_symbol("BTC") is None


def test_resolve_symbol_unknown():
    """未知 symbol 返回 None"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    assert app._resolve_symbol("XYZ") is None


def test_resolve_symbol_empty():
    """空字符串返回 None"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    assert app._resolve_symbol("") is None
    assert app._resolve_symbol("  ") is None


# ================================================================
# _parse_duration 测试
# ================================================================


def test_parse_duration_seconds():
    """秒解析"""
    assert Application._parse_duration("10s") == 10.0
    assert Application._parse_duration("1s") == 1.0
    assert Application._parse_duration("0.5s") == 0.5


def test_parse_duration_minutes():
    """分解析"""
    assert Application._parse_duration("30m") == 1800.0
    assert Application._parse_duration("1m") == 60.0


def test_parse_duration_hours():
    """时解析"""
    assert Application._parse_duration("2h") == 7200.0
    assert Application._parse_duration("1h") == 3600.0


def test_parse_duration_case_insensitive():
    """大小写不敏感"""
    assert Application._parse_duration("10S") == 10.0
    assert Application._parse_duration("30M") == 1800.0
    assert Application._parse_duration("2H") == 7200.0


def test_parse_duration_invalid():
    """无效输入返回 None"""
    assert Application._parse_duration("") is None
    assert Application._parse_duration("s") is None
    assert Application._parse_duration("abc") is None
    assert Application._parse_duration("10") is None
    assert Application._parse_duration("10x") is None
    assert Application._parse_duration("-5s") is None
    assert Application._parse_duration("0s") is None
    # NaN / Inf / 超上限
    assert Application._parse_duration("nans") is None
    assert Application._parse_duration("infs") is None
    assert Application._parse_duration("infh") is None
    assert Application._parse_duration("1e309s") is None
    assert Application._parse_duration("25h") is None  # 超过 24h 上限


# ================================================================
# _handle_cmd_pause args 拆分测试
# ================================================================


@pytest.mark.asyncio
async def test_handle_cmd_pause_global_no_args():
    """无参数 -> 全局暂停"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("")
    assert "已暂停全局" in result
    assert app.pause_manager.is_paused() is True


@pytest.mark.asyncio
async def test_handle_cmd_pause_global_with_duration():
    """全局定时暂停: /pause 10s"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("10s")
    assert "已暂停全局" in result
    assert "自动恢复" in result
    assert app.pause_manager.is_paused() is True
    # 清理
    await app.pause_manager.resume()


@pytest.mark.asyncio
async def test_handle_cmd_pause_symbol_with_duration():
    """symbol 定时暂停: /pause BTC 30m"""
    app = _make_app_with_symbols({"BTC/USDT:USDT", "ETH/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("BTC 30m")
    assert "已暂停" in result
    assert "自动恢复" in result
    assert app.pause_manager.is_paused("BTC/USDT:USDT") is True
    assert app.pause_manager.is_paused("ETH/USDT:USDT") is False
    # 清理
    await app.pause_manager.resume("BTC/USDT:USDT")


@pytest.mark.asyncio
async def test_handle_cmd_pause_symbol_no_duration():
    """symbol 无限期暂停: /pause BTC"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("BTC")
    assert "已暂停" in result
    assert app.pause_manager.is_paused("BTC/USDT:USDT") is True
    await app.pause_manager.resume("BTC/USDT:USDT")


@pytest.mark.asyncio
async def test_handle_cmd_pause_unknown_symbol():
    """未知 symbol"""
    app = _make_app_with_symbols({"BTC/USDT:USDT"})
    app.pause_manager = PauseManager()
    result = await app._handle_cmd_pause("XYZ 10s")
    assert "未知交易对" in result


# ================================================================
# _schedule_protective_stop_sync 两阶段取消测试
# ================================================================


def _make_app_for_schedule_test() -> Application:
    """构造一个能测试 _schedule_protective_stop_sync 的 Application。"""
    app = Application.__new__(Application)
    app._running = True
    app._active_symbols = {"BTC/USDT:USDT"}
    app.exchange = MagicMock()
    app.protective_stop_manager = MagicMock()
    app._protective_stop_tasks = {}  # type: ignore[assignment]
    app._protective_stop_task_reasons = {}  # type: ignore[assignment]
    app._protective_stop_task_executing = {}  # type: ignore[assignment]
    app._protective_stop_pending_reason = {}  # type: ignore[assignment]
    app._positions = {}  # type: ignore[assignment]
    app._symbol_configs = {}  # type: ignore[assignment]
    app._rules = {}  # type: ignore[assignment]
    return app


@pytest.mark.asyncio
async def test_schedule_debounce_task_can_be_cancelled():
    """debounce 阶段的任务应该被新调度取消"""
    app = _make_app_for_schedule_test()
    sync_called = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        sync_called.set()

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度 (debounce=0.2s)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    first_task = app._protective_stop_tasks["BTC/USDT:USDT"]

    # 立即第二次调度 -> 应取消第一个(还在 debounce sleep)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    second_task = app._protective_stop_tasks["BTC/USDT:USDT"]

    assert first_task is not second_task
    # cancel 已发出, yield 让事件循环处理取消
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()

    # 等第二个完成
    await second_task
    assert sync_called.is_set()


@pytest.mark.asyncio
async def test_schedule_executing_task_not_cancelled():
    """已进入执行阶段的任务不应被取消, 新请求走脏标记, 任务完成后自行 re-run"""
    app = _make_app_for_schedule_test()

    call_order: list[str] = []
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        call_order.append(reason)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度 (startup -> debounce=0s, 立即进入执行)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    # 等第一个进入执行阶段
    await first_entered.wait()

    # 此时 past_debounce 已 set, 新调度应走脏标记路径(不创建新任务)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    # 同一个 task 对象(脏标记模式不创建新任务)
    assert app._protective_stop_tasks["BTC/USDT:USDT"] is task
    assert not task.cancelled()

    # 放行第一个
    first_can_finish.set()
    await task

    # 两次 sync 都应该被调用, 且顺序正确
    assert call_order == ["startup", "our_algo_canceled"]


@pytest.mark.asyncio
async def test_schedule_no_concurrent_sync():
    """同一 symbol 不应并发执行 _sync_protective_stop"""
    app = _make_app_for_schedule_test()

    concurrent_count = 0
    max_concurrent = 0
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()
        concurrent_count -= 1

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # 第一次调度(startup, debounce=0)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    await first_entered.wait()

    # 第二次调度, 走脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")

    first_can_finish.set()
    await task

    assert max_concurrent == 1  # 始终串行


@pytest.mark.asyncio
async def test_schedule_triple_trigger_no_concurrent():
    """三次触发: T1 执行中 + T2 脏标记 + T3 覆盖脏标记, 始终串行且不丢失最终请求"""
    app = _make_app_for_schedule_test()

    concurrent_count = 0
    max_concurrent = 0
    call_order: list[str] = []
    first_entered = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def mock_sync(*, symbol, reason):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        call_order.append(reason)
        if reason == "startup":
            first_entered.set()
            await first_can_finish.wait()
        concurrent_count -= 1

    app._sync_protective_stop = mock_sync  # type: ignore[method-assign]

    # T1: startup (debounce=0, 立即进入执行)
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "startup")
    task = app._protective_stop_tasks["BTC/USDT:USDT"]

    await first_entered.wait()

    # T2: 走脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "our_algo_canceled")
    assert app._protective_stop_pending_reason.get("BTC/USDT:USDT") == "our_algo_canceled"

    # T3: 覆盖脏标记
    app._schedule_protective_stop_sync("BTC/USDT:USDT", "position_update")
    assert app._protective_stop_pending_reason.get("BTC/USDT:USDT") == "position_update"

    # 同一个 task 对象(T2/T3 都不创建新任务)
    assert app._protective_stop_tasks["BTC/USDT:USDT"] is task

    # 放行 T1
    first_can_finish.set()
    await task

    # T1 + T3 的 re-run(T2 被 T3 覆盖)
    assert call_order == ["startup", "position_update"]
    assert max_concurrent == 1  # 始终串行, 不会出现并发
