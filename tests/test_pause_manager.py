"""PauseManager 单元测试"""

import pytest
from src.notify.pause_manager import PauseManager


@pytest.fixture
def pm() -> PauseManager:
    return PauseManager()


@pytest.mark.asyncio
async def test_initial_state_not_paused(pm: PauseManager) -> None:
    """初始状态：全局和任意 symbol 都未暂停"""
    assert pm.is_paused() is False
    assert pm.is_paused("BTC/USDT:USDT") is False


@pytest.mark.asyncio
async def test_global_pause_and_resume(pm: PauseManager) -> None:
    """全局暂停 -> 恢复"""
    result = await pm.pause()
    assert "已暂停全局" in result
    assert pm.is_paused() is True
    assert pm.is_paused("BTC/USDT:USDT") is True

    result = await pm.resume()
    assert "已恢复" in result
    assert pm.is_paused() is False
    assert pm.is_paused("BTC/USDT:USDT") is False


@pytest.mark.asyncio
async def test_symbol_pause_and_resume(pm: PauseManager) -> None:
    """per-symbol 暂停 -> 恢复"""
    result = await pm.pause("BTC/USDT:USDT")
    assert "已暂停" in result
    assert pm.is_paused("BTC/USDT:USDT") is True
    assert pm.is_paused("ETH/USDT:USDT") is False
    assert pm.is_paused() is False  # 全局未暂停

    result = await pm.resume("BTC/USDT:USDT")
    assert "已恢复" in result
    assert pm.is_paused("BTC/USDT:USDT") is False


@pytest.mark.asyncio
async def test_global_pause_overrides_symbol(pm: PauseManager) -> None:
    """全局暂停时，所有 symbol 都视为暂停"""
    await pm.pause()
    assert pm.is_paused("ANY/USDT:USDT") is True


@pytest.mark.asyncio
async def test_global_resume_clears_symbol_pauses(pm: PauseManager) -> None:
    """全局恢复同时清除所有 per-symbol 暂停"""
    await pm.pause("BTC/USDT:USDT")
    await pm.pause("ETH/USDT:USDT")
    assert pm.is_paused("BTC/USDT:USDT") is True
    assert pm.is_paused("ETH/USDT:USDT") is True

    result = await pm.resume()
    assert "已恢复" in result
    assert pm.is_paused("BTC/USDT:USDT") is False
    assert pm.is_paused("ETH/USDT:USDT") is False


@pytest.mark.asyncio
async def test_duplicate_pause_idempotent(pm: PauseManager) -> None:
    """重复暂停返回已暂停提示"""
    await pm.pause()
    result = await pm.pause()
    assert "已处于暂停" in result

    pm2 = PauseManager()
    await pm2.pause("BTC/USDT:USDT")
    result = await pm2.pause("BTC/USDT:USDT")
    assert "已处于暂停" in result


@pytest.mark.asyncio
async def test_resume_not_paused(pm: PauseManager) -> None:
    """恢复未暂停的 symbol"""
    result = await pm.resume("BTC/USDT:USDT")
    assert "未处于暂停" in result


@pytest.mark.asyncio
async def test_resume_symbol_during_global_pause(pm: PauseManager) -> None:
    """全局暂停时，尝试恢复单个 symbol 应提示使用全局恢复"""
    await pm.pause()
    result = await pm.resume("BTC/USDT:USDT")
    assert "全局暂停" in result


@pytest.mark.asyncio
async def test_pause_symbol_during_global_pause(pm: PauseManager) -> None:
    """全局暂停时，尝试单独暂停 symbol 应提示无需操作"""
    await pm.pause()
    result = await pm.pause("BTC/USDT:USDT")
    assert "全局已暂停" in result


@pytest.mark.asyncio
async def test_pause_callback_called() -> None:
    """暂停时触发回调"""
    called_with: list[object] = []

    async def callback(sym: object) -> None:
        called_with.append(sym)

    pm = PauseManager(on_pause_callback=callback)
    await pm.pause()
    assert called_with == [None]


@pytest.mark.asyncio
async def test_pause_callback_receives_symbol() -> None:
    """回调接收正确的 symbol 参数"""
    called_with: list[object] = []

    async def callback(sym: object) -> None:
        called_with.append(sym)

    pm = PauseManager(on_pause_callback=callback)
    await pm.pause("BTC/USDT:USDT")
    assert called_with == ["BTC/USDT:USDT"]


@pytest.mark.asyncio
async def test_pause_callback_not_called_on_duplicate() -> None:
    """重复暂停不触发回调"""
    call_count = 0

    async def callback(_sym: object) -> None:
        nonlocal call_count
        call_count += 1

    pm = PauseManager(on_pause_callback=callback)
    await pm.pause()
    assert call_count == 1
    await pm.pause()
    assert call_count == 1  # 不应再次调用


@pytest.mark.asyncio
async def test_pause_callback_error_does_not_break() -> None:
    """回调异常不影响暂停状态设置"""
    async def bad_callback(_symbol: object) -> None:
        raise RuntimeError("callback error")

    pm_with_bad_cb = PauseManager(on_pause_callback=bad_callback)
    result = await pm_with_bad_cb.pause()
    assert "已暂停" in result
    assert "失败" in result
    assert pm_with_bad_cb.is_paused() is True


@pytest.mark.asyncio
async def test_get_status(pm: PauseManager) -> None:
    """status 返回正确结构"""
    status = pm.get_status()
    assert status["global_paused"] is False
    assert status["global_paused_at"] is None
    assert status["paused_symbols"] == {}

    await pm.pause("BTC/USDT:USDT")
    status = pm.get_status()
    assert "BTC/USDT:USDT" in status["paused_symbols"]

    await pm.resume("BTC/USDT:USDT")
    await pm.pause()
    status = pm.get_status()
    assert status["global_paused"] is True
    assert status["global_paused_at"] is not None


@pytest.mark.asyncio
async def test_pause_callback_error_reports_failure_global() -> None:
    """全局暂停回调失败时，返回消息应指明失败"""
    async def bad_callback(_symbol: object) -> None:
        raise RuntimeError("cancel failed")

    pm = PauseManager(on_pause_callback=bad_callback)
    result = await pm.pause()
    assert "失败" in result
    assert pm.is_paused() is True  # 状态仍然设置


@pytest.mark.asyncio
async def test_pause_callback_error_reports_failure_symbol() -> None:
    """per-symbol 暂停回调失败时，返回消息应指明失败"""
    async def bad_callback(_symbol: object) -> None:
        raise RuntimeError("cancel failed")

    pm = PauseManager(on_pause_callback=bad_callback)
    result = await pm.pause("BTC/USDT:USDT")
    assert "失败" in result
    assert pm.is_paused("BTC/USDT:USDT") is True
