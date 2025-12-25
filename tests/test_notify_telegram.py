# Input: 被测模块与 pytest 夹具
# Output: pytest 断言结果（含限流重试）
# Pos: 测试用例
# 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。

"""
Telegram 通知模块测试
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple

import pytest

import src.notify.telegram as telegram_module
from src.notify.telegram import TelegramNotifier
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests():
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):  # noqa: ANN001
        return self._payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False


class FakeSession:
    def __init__(self, responses: List[Tuple[int, Any]]):
        self._responses = list(responses)
        self.post_calls: List[Tuple[str, Dict[str, Any], Any]] = []
        self.closed = False

    def post(self, url: str, json: Dict[str, Any], proxy=None):  # noqa: ANN001
        self.post_calls.append((url, json, proxy))
        status, payload = self._responses.pop(0)
        return FakeResponse(status=status, payload=payload)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_send_message_retries_until_success(monkeypatch):
    notifier = TelegramNotifier(token="token", chat_id="chat", enabled=True, max_retries=3)
    fake_session = FakeSession(responses=[(500, {"ok": False}), (200, {"ok": True})])
    notifier._session = fake_session  # type: ignore[assignment]

    async def noop_ensure_session() -> None:
        return

    notifier._ensure_session = noop_ensure_session  # type: ignore[method-assign]

    async def fast_sleep(delay: float, result=None):  # noqa: ANN001
        return result

    monkeypatch.setattr(telegram_module.asyncio, "sleep", fast_sleep)

    ok = await notifier._send_message("hello")  # noqa: SLF001
    assert ok is True
    assert len(fake_session.post_calls) == 2
    assert fake_session.post_calls[0][0].endswith("/sendMessage")
    assert fake_session.post_calls[0][1]["chat_id"] == "chat"


@pytest.mark.asyncio
async def test_send_message_noop_when_disabled(monkeypatch):
    notifier = TelegramNotifier(token="", chat_id="", enabled=False)

    async def should_not_run() -> None:
        raise AssertionError("_ensure_session should not be called when disabled")

    notifier._ensure_session = should_not_run  # type: ignore[method-assign]

    ok = await notifier._send_message("hello")  # noqa: SLF001
    assert ok is True


@pytest.mark.asyncio
async def test_close_closes_session():
    notifier = TelegramNotifier(token="token", chat_id="chat", enabled=True)
    fake_session = FakeSession(responses=[])
    notifier._session = fake_session  # type: ignore[assignment]

    await notifier.close()
    assert fake_session.closed is True
    assert notifier._session is None


@pytest.mark.asyncio
async def test_notify_fill_formats_chinese_multiline(monkeypatch):
    notifier = TelegramNotifier(token="token", chat_id="chat", enabled=True)
    sent: List[str] = []

    async def capture(text: str) -> bool:  # noqa: ANN001
        sent.append(text)
        return True

    notifier._send_message = capture  # type: ignore[method-assign]  # noqa: SLF001

    await notifier.notify_fill(
        symbol="BTC/USDT:USDT",
        side="LONG",
        mode="MAKER_ONLY",
        qty="0.1",
        avg_price="50000",
        reason="long_primary",
        position_before="1.0",
        position_after="0.9",
    )

    assert sent == [
        "【已成交】平多\n"
        "  交易对: BTC/USDT\n"
        "  成交: 0.1 @ 50000\n"
        "  执行: 挂单模式\n"
        "  原因: long_primary\n"
        "  仓位: 1.0 -> 0.9"
    ]


@pytest.mark.asyncio
async def test_notify_open_alert_formats_chinese_multiline(monkeypatch):
    notifier = TelegramNotifier(token="token", chat_id="chat", enabled=True)
    sent: List[str] = []

    async def capture(text: str) -> bool:  # noqa: ANN001
        sent.append(text)
        return True

    notifier._send_message = capture  # type: ignore[method-assign]  # noqa: SLF001

    await notifier.notify_open_alert(
        symbol="ETH/USDT:USDT",
        side="SHORT",
        position_before="0",
        position_after="1.2",
    )

    assert sent == [
        "【告警】开空\n"
        "  交易对: ETH/USDT\n"
        "  仓位: 0 -> 1.2"
    ]


@pytest.mark.asyncio
async def test_send_message_respects_retry_after(monkeypatch):
    notifier = TelegramNotifier(token="token", chat_id="chat", enabled=True, max_retries=1)
    fake_session = FakeSession(
        responses=[
            (429, {"ok": False, "parameters": {"retry_after": 2}}),
            (200, {"ok": True}),
        ]
    )
    notifier._session = fake_session  # type: ignore[assignment]

    async def noop_ensure_session() -> None:
        return

    notifier._ensure_session = noop_ensure_session  # type: ignore[method-assign]

    sleeps: List[float] = []

    async def capture_sleep(delay: float, result=None):  # noqa: ANN001
        sleeps.append(delay)
        return result

    monkeypatch.setattr(telegram_module.asyncio, "sleep", capture_sleep)

    ok = await notifier._send_message("hello")  # noqa: SLF001
    assert ok is True
    assert len(fake_session.post_calls) == 2
    assert notifier._cooldown_until > 0
