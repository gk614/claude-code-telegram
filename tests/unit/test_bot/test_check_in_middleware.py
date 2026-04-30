"""Unit tests for `bot.middleware.check_in.check_in_middleware`.

The middleware is a coroutine of shape `(handler, event, data) -> Any`.
We test five branches:

1. Reply to an AM ping → captured + confirmation + handler NOT called.
2. Reply to a PM ping → captured + confirmation + handler NOT called.
3. Message with no `reply_to_message` → handler called (passthrough).
4. Reply to a non-check-in message → handler called (passthrough).
5. Capture failure → handler called (fail-open passthrough).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.middleware.check_in import check_in_middleware


def _make_event(reply_text=None, msg_text="my answer"):
    """Build a minimal mock event with optional `reply_to_message.text`."""
    event = MagicMock()
    event.effective_user = MagicMock()
    event.effective_user.id = 12345
    event.effective_user.is_bot = False

    msg = MagicMock()
    msg.text = msg_text
    msg.reply_text = AsyncMock()

    if reply_text is None:
        msg.reply_to_message = None
    else:
        reply = MagicMock()
        reply.text = reply_text
        reply.caption = None
        msg.reply_to_message = reply

    event.effective_message = msg
    return event


@pytest.fixture
def settings_stub(tmp_path):
    """Settings stub with `genaos_repo_path` pointing into tmp_path."""
    settings = MagicMock()
    settings.genaos_repo_path = tmp_path
    return settings


@pytest.fixture
def downstream_handler():
    """AsyncMock standing in for the next middleware/handler in the chain."""
    return AsyncMock(return_value="downstream-result")


@pytest.mark.asyncio
async def test_am_reply_is_captured_and_chain_stops(
    settings_stub, downstream_handler, tmp_path
):
    event = _make_event(
        reply_text="🌅 *AM check-in* — ответь реплаем", msg_text="8, 7, бот, панды"
    )
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    # Chain stopped: downstream not called, return is None (handler-stop signal).
    assert result is None
    downstream_handler.assert_not_awaited()

    # Confirmation reply sent.
    event.effective_message.reply_text.assert_awaited_once()
    assert "AM" in event.effective_message.reply_text.await_args.args[0]

    # Episodic file written under the right section.
    episodic_dir = tmp_path / "tracks" / "state" / "episodic"
    files = list(episodic_dir.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "## AM check-in" in content
    assert "8, 7, бот, панды" in content


@pytest.mark.asyncio
async def test_pm_reply_is_captured_and_chain_stops(
    settings_stub, downstream_handler, tmp_path
):
    event = _make_event(
        reply_text="🌙 *PM check-in* — ответь", msg_text="устал, но день ок"
    )
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    assert result is None
    downstream_handler.assert_not_awaited()

    episodic_dir = tmp_path / "tracks" / "state" / "episodic"
    content = next(episodic_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "## PM рефлексия" in content
    assert "устал, но день ок" in content


@pytest.mark.asyncio
async def test_no_reply_passes_through(settings_stub, downstream_handler):
    event = _make_event(reply_text=None, msg_text="hello")
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    assert result == "downstream-result"
    downstream_handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_reply_to_non_checkin_passes_through(settings_stub, downstream_handler):
    event = _make_event(
        reply_text="случайное сообщение от бота, не check-in",
        msg_text="ответ на него",
    )
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    assert result == "downstream-result"
    downstream_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_text_passes_through(settings_stub, downstream_handler):
    event = _make_event(reply_text=None, msg_text=None)
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    assert result == "downstream-result"
    downstream_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_repo_path_falls_back_to_router(downstream_handler):
    """When genaos_repo_path is unset, we cannot write episodic — fall through to router."""
    settings = MagicMock()
    settings.genaos_repo_path = None
    event = _make_event(reply_text="🌅 *AM check-in*", msg_text="8")
    data = {"settings": settings}

    result = await check_in_middleware(downstream_handler, event, data)

    assert result == "downstream-result"
    downstream_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_failure_falls_back_to_router(
    monkeypatch, settings_stub, downstream_handler
):
    """If capture_check_in_reply raises, the message is passed to router instead of being lost."""
    from src.bot.middleware import check_in as middleware_module

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(middleware_module, "capture_check_in_reply", boom)

    event = _make_event(reply_text="🌅 *AM check-in*", msg_text="8")
    data = {"settings": settings_stub}

    result = await check_in_middleware(downstream_handler, event, data)

    # Fail-open: downstream still ran.
    assert result == "downstream-result"
    downstream_handler.assert_awaited_once()
