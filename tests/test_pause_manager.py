"""PauseManager 单元测试"""

import asyncio

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


# ================================================================
# 定时暂停测试
# ================================================================


@pytest.mark.asyncio
async def test_timed_global_pause_auto_resumes() -> None:
    """定时全局暂停到期后自动恢复"""
    pm = PauseManager()
    result = await pm.pause(duration_s=0.05)
    assert "已暂停全局" in result
    assert "自动恢复" in result
    assert pm.is_paused() is True

    # 等待定时器触发
    await asyncio.sleep(0.15)
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_timed_symbol_pause_auto_resumes() -> None:
    """定时 symbol 暂停到期后自动恢复"""
    pm = PauseManager()
    result = await pm.pause("BTC/USDT:USDT", duration_s=0.05)
    assert "已暂停" in result
    assert "自动恢复" in result
    assert pm.is_paused("BTC/USDT:USDT") is True

    await asyncio.sleep(0.15)
    assert pm.is_paused("BTC/USDT:USDT") is False


@pytest.mark.asyncio
async def test_manual_resume_cancels_timer() -> None:
    """手动 resume 取消定时器（不会再自动恢复）"""
    pm = PauseManager()
    await pm.pause(duration_s=1.0)
    assert pm.is_paused() is True

    # 手动 resume
    result = await pm.resume()
    assert "已恢复" in result
    assert pm.is_paused() is False

    # 定时器已被取消，不应有残留任务
    assert len(pm._resume_tasks) == 0
    assert len(pm._auto_resume_at) == 0


@pytest.mark.asyncio
async def test_global_resume_cancels_all_timers() -> None:
    """全局 resume 取消所有定时器（含 per-symbol）"""
    pm = PauseManager()
    await pm.pause("BTC/USDT:USDT", duration_s=1.0)
    await pm.pause("ETH/USDT:USDT", duration_s=1.0)
    assert pm.is_paused("BTC/USDT:USDT") is True
    assert pm.is_paused("ETH/USDT:USDT") is True

    result = await pm.resume()
    assert "已恢复" in result
    assert len(pm._resume_tasks) == 0
    assert len(pm._auto_resume_at) == 0


@pytest.mark.asyncio
async def test_get_status_contains_resume_at() -> None:
    """get_status 含 resume_at 字段"""
    pm = PauseManager()

    # 无定时暂停
    status = pm.get_status()
    assert status["global_resume_at"] is None
    assert status["symbol_resume_at"] == {}

    # 全局定时暂停
    await pm.pause(duration_s=60.0)
    status = pm.get_status()
    assert status["global_resume_at"] is not None

    await pm.resume()

    # symbol 定时暂停
    await pm.pause("BTC/USDT:USDT", duration_s=60.0)
    status = pm.get_status()
    assert "BTC/USDT:USDT" in status["symbol_resume_at"]

    # 清理
    await pm.resume()


@pytest.mark.asyncio
async def test_timed_pause_update_timer() -> None:
    """已暂停状态下再次传入 duration 更新定时器"""
    pm = PauseManager()
    await pm.pause(duration_s=10.0)
    result = await pm.pause(duration_s=0.05)
    assert "已更新定时恢复" in result

    await asyncio.sleep(0.15)
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_cancel_all_timers() -> None:
    """cancel_all_timers 清理定时任务"""
    pm = PauseManager()
    await pm.pause(duration_s=1.0)
    await pm.pause("BTC/USDT:USDT", duration_s=1.0)

    # cancel_all_timers 不改变暂停状态，只取消定时任务
    # 但实际上全局暂停时不允许 per-symbol 暂停，所以先 resume 全局再 pause symbol
    await pm.resume()
    await pm.pause(duration_s=1.0)
    pm.cancel_all_timers()
    assert len(pm._resume_tasks) == 0
    assert len(pm._auto_resume_at) == 0
    # 暂停状态不变
    assert pm.is_paused() is True
    await pm.resume()


@pytest.mark.asyncio
async def test_format_duration() -> None:
    """_format_duration 正确格式化"""
    assert PauseManager._format_duration(10) == "10s"
    assert PauseManager._format_duration(60) == "1m"
    assert PauseManager._format_duration(1800) == "30m"
    assert PauseManager._format_duration(3600) == "1h"
    assert PauseManager._format_duration(7200) == "2h"


@pytest.mark.asyncio
async def test_schedule_resume_failure_degrades_gracefully() -> None:
    """_try_schedule_resume 失败时降级：暂停生效但无定时恢复"""
    pm = PauseManager()
    # 直接调 pause() 传 inf → 入口通常由 _parse_duration 拦截，
    # 但作为独立 API，PauseManager 内部应自行降级
    result = await pm.pause(duration_s=float("inf"))
    assert pm.is_paused() is True
    assert "已暂停全局" in result
    assert "定时恢复设置失败" in result
    assert len(pm._resume_tasks) == 0
    await pm.resume()


@pytest.mark.asyncio
async def test_schedule_resume_failure_symbol_degrades() -> None:
    """symbol 级 _try_schedule_resume 失败降级"""
    pm = PauseManager()
    result = await pm.pause("BTC/USDT:USDT", duration_s=float("inf"))
    assert pm.is_paused("BTC/USDT:USDT") is True
    assert "定时恢复设置失败" in result
    await pm.resume("BTC/USDT:USDT")


@pytest.mark.asyncio
async def test_update_timer_failure_degrades() -> None:
    """已暂停时更新定时器失败降级"""
    pm = PauseManager()
    await pm.pause()
    result = await pm.pause(duration_s=float("inf"))
    assert pm.is_paused() is True
    assert "定时恢复设置失败" in result
    await pm.resume()


@pytest.mark.asyncio
async def test_auto_resume_callback_called() -> None:
    """定时恢复到期后触发 on_auto_resume_callback"""
    called_with: list[str] = []

    async def on_resume(msg: str) -> None:
        called_with.append(msg)

    pm = PauseManager(on_auto_resume_callback=on_resume)
    await pm.pause(duration_s=0.05)
    assert pm.is_paused() is True

    await asyncio.sleep(0.15)
    assert pm.is_paused() is False
    assert len(called_with) == 1
    assert "已恢复" in called_with[0]


@pytest.mark.asyncio
async def test_auto_resume_callback_called_symbol() -> None:
    """symbol 定时恢复到期后触发 on_auto_resume_callback"""
    called_with: list[str] = []

    async def on_resume(msg: str) -> None:
        called_with.append(msg)

    pm = PauseManager(on_auto_resume_callback=on_resume)
    await pm.pause("BTC/USDT:USDT", duration_s=0.05)

    await asyncio.sleep(0.15)
    assert pm.is_paused("BTC/USDT:USDT") is False
    assert len(called_with) == 1
    assert "已恢复" in called_with[0]
