"""TelegramBot 单元测试"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple

import pytest

from src.notify.bot import TelegramBot
from src.utils.logger import setup_logger


@pytest.fixture(autouse=True)
def setup_logger_for_tests() -> Any:
    """每个测试前设置 logger"""
    with TemporaryDirectory() as tmpdir:
        setup_logger(Path(tmpdir), console=False)
        yield


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status = status
        self._payload = payload

    async def json(self, content_type: Any = None) -> Any:
        return self._payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class FakeSession:
    """模拟 aiohttp.ClientSession，支持 get/post 调用记录。"""

    def __init__(self, responses: List[Tuple[int, Any]]):
        self._responses = list(responses)
        self.get_calls: List[Tuple[str, Dict[str, Any], Any]] = []
        self.post_calls: List[Tuple[str, Dict[str, Any], Any]] = []
        self.closed = False

    def get(self, url: str, params: Any = None, proxy: Any = None) -> "FakeResponse":
        self.get_calls.append((url, params or {}, proxy))
        status, payload = self._responses.pop(0)
        return FakeResponse(status=status, payload=payload)

    def post(self, url: str, json: Any = None, proxy: Any = None) -> "FakeResponse":
        self.post_calls.append((url, json or {}, proxy))
        status, payload = self._responses.pop(0)
        return FakeResponse(status=status, payload=payload)

    async def close(self) -> None:
        self.closed = True


def _make_update(update_id: int, chat_id: str, text: str) -> Dict[str, Any]:
    """构造一个 Telegram update dict。"""
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


def _make_bot(
    allowed_chat_ids: set[str] | None = None,
) -> TelegramBot:
    bot = TelegramBot(
        token="test-token",
        allowed_chat_ids=allowed_chat_ids or {"12345"},
    )
    return bot


@pytest.mark.asyncio
async def test_process_update_routes_command() -> None:
    """命令正确路由到 handler"""
    bot = _make_bot()
    received_args: list[str] = []

    async def handler(args: str) -> str:
        received_args.append(args)
        return "ok"

    bot.register_handler("test", handler)

    # 模拟 _send_reply 的 session
    fake_session = FakeSession([(200, {"ok": True})])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "/test")
    await bot._process_update(update)

    assert received_args == [""]
    assert len(fake_session.post_calls) == 1
    assert fake_session.post_calls[0][1]["text"] == "ok"


@pytest.mark.asyncio
async def test_process_update_with_args() -> None:
    """带参数的命令正确解析"""
    bot = _make_bot()
    received_args: list[str] = []

    async def handler(args: str) -> str:
        received_args.append(args)
        return "done"

    bot.register_handler("pause", handler)

    fake_session = FakeSession([(200, {"ok": True})])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "/pause BTCUSDT")
    await bot._process_update(update)

    assert received_args == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_process_update_ignores_unauthorized_chat() -> None:
    """忽略未授权 chat_id"""
    bot = _make_bot(allowed_chat_ids={"12345"})
    handler_called = False

    async def handler(args: str) -> str:
        nonlocal handler_called
        handler_called = True
        return "ok"

    bot.register_handler("test", handler)

    fake_session = FakeSession([])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "99999", "/test")
    await bot._process_update(update)

    assert handler_called is False
    assert len(fake_session.post_calls) == 0


@pytest.mark.asyncio
async def test_process_update_unknown_command() -> None:
    """未知命令返回帮助提示"""
    bot = _make_bot()

    fake_session = FakeSession([(200, {"ok": True})])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "/unknown")
    await bot._process_update(update)

    assert len(fake_session.post_calls) == 1
    reply_text = fake_session.post_calls[0][1]["text"]
    assert "未知命令" in reply_text
    assert "/help" in reply_text


@pytest.mark.asyncio
async def test_process_update_ignores_non_command() -> None:
    """忽略非 / 开头消息"""
    bot = _make_bot()
    handler_called = False

    async def handler(args: str) -> str:
        nonlocal handler_called
        handler_called = True
        return "ok"

    bot.register_handler("test", handler)

    fake_session = FakeSession([])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "hello world")
    await bot._process_update(update)

    assert handler_called is False


@pytest.mark.asyncio
async def test_parse_bot_mention() -> None:
    """解析 /pause@mybot 格式"""
    bot = _make_bot()
    received_args: list[str] = []

    async def handler(args: str) -> str:
        received_args.append(args)
        return "ok"

    bot.register_handler("pause", handler)

    fake_session = FakeSession([(200, {"ok": True})])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "/pause@mybot BTCUSDT")
    await bot._process_update(update)

    assert received_args == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_get_updates_updates_offset() -> None:
    """offset 正确递增"""
    bot = _make_bot()

    fake_session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 100, "message": {"chat": {"id": 12345}, "text": "hi"}},
            {"update_id": 101, "message": {"chat": {"id": 12345}, "text": "hi"}},
        ]}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    updates = await bot._get_updates()
    assert len(updates) == 2
    assert bot._offset == 102  # 101 + 1


@pytest.mark.asyncio
async def test_get_updates_empty_result() -> None:
    """空结果不更新 offset"""
    bot = _make_bot()

    fake_session = FakeSession([
        (200, {"ok": True, "result": []}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    updates = await bot._get_updates()
    assert len(updates) == 0
    assert bot._offset == 0


@pytest.mark.asyncio
async def test_get_updates_api_error() -> None:
    """API 返回错误时返回空列表"""
    bot = _make_bot()

    fake_session = FakeSession([
        (200, {"ok": False, "description": "Unauthorized"}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    updates = await bot._get_updates()
    assert len(updates) == 0


@pytest.mark.asyncio
async def test_handler_exception_returns_error_message() -> None:
    """handler 抛异常时返回错误消息"""
    bot = _make_bot()

    async def bad_handler(args: str) -> str:
        raise RuntimeError("test error")

    bot.register_handler("crash", bad_handler)

    fake_session = FakeSession([(200, {"ok": True})])
    bot._session = fake_session  # type: ignore[assignment]

    update = _make_update(1, "12345", "/crash")
    await bot._process_update(update)

    assert len(fake_session.post_calls) == 1
    reply_text = fake_session.post_calls[0][1]["text"]
    assert "异常" in reply_text


@pytest.mark.asyncio
async def test_process_update_no_message() -> None:
    """update 无 message 字段时静默跳过"""
    bot = _make_bot()
    fake_session = FakeSession([])
    bot._session = fake_session  # type: ignore[assignment]

    await bot._process_update({"update_id": 1})
    assert len(fake_session.post_calls) == 0


@pytest.mark.asyncio
async def test_flush_pending_updates_advances_offset() -> None:
    """flush 启动时将 offset 推进到最新"""
    bot = _make_bot()
    assert bot._offset == 0

    fake_session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 500, "message": {"chat": {"id": 12345}, "text": "/old"}},
        ]}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    await bot._flush_pending_updates()
    assert bot._offset == 501  # 500 + 1


@pytest.mark.asyncio
async def test_flush_pending_updates_empty() -> None:
    """flush 无积压消息时 offset 不变"""
    bot = _make_bot()
    assert bot._offset == 0

    fake_session = FakeSession([
        (200, {"ok": True, "result": []}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    await bot._flush_pending_updates()
    assert bot._offset == 0


@pytest.mark.asyncio
async def test_flush_pending_updates_api_error() -> None:
    """flush API 失败时不影响运行"""
    bot = _make_bot()

    fake_session = FakeSession([
        (200, {"ok": False, "description": "error"}),
    ])
    bot._session = fake_session  # type: ignore[assignment]

    await bot._flush_pending_updates()
    assert bot._offset == 0
