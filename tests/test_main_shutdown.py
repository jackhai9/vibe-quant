# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
main.py 应用关闭行为测试
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.main import Application
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
