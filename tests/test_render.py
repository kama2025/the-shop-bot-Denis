"""Показ экрана: порядок «отправить, потом удалить».

Это защита от случая, который уже случался в проде: при правке экрана старое
сообщение удалялось первым, «в расчёте» на то, что новое отправится. Когда
отправка падала — например, из-за стиля кнопки, который Telegram не принимает, —
у покупателя исчезал и старый экран, и новый. Со стороны это выглядело так,
будто бот стирает свои сообщения.

Разрушающий шаг не должен выполняться на предположении, что следующий удастся.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.utils.render import _edit_or_resend


class FakePhotoSize:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


class FakeMessage:
    """Сообщение Telegram, которым управляет тест.

    Записывает порядок вызовов — именно он здесь и проверяется.
    """

    def __init__(
        self,
        has_photo: bool = False,
        edit_fails: bool = True,
        send_fails: bool = False,
        delete_fails: bool = False,
        photo_id: str = "file-id-old",
    ) -> None:
        self.photo = [FakePhotoSize(photo_id)] if has_photo else None
        self.edit_fails = edit_fails
        self.send_fails = send_fails
        self.delete_fails = delete_fails
        self.calls: list[str] = []

    def _boom(self, what: str) -> TelegramBadRequest:
        return TelegramBadRequest(method=None, message=f"Bad Request: {what}")

    async def edit_text(self, *args, **kwargs):
        self.calls.append("edit_text")
        if self.edit_fails:
            raise self._boom("can't parse InlineKeyboardButton: invalid button style specified")
        return self

    async def edit_caption(self, *args, **kwargs):
        self.calls.append("edit_caption")
        if self.edit_fails:
            raise self._boom("message can't be edited")
        return self

    async def edit_media(self, *args, **kwargs):
        self.calls.append("edit_media")
        if self.edit_fails:
            raise self._boom("message can't be edited")
        return self

    async def answer(self, *args, **kwargs):
        self.calls.append("answer")
        if self.send_fails:
            raise self._boom("can't parse InlineKeyboardButton: invalid button style specified")
        return FakeMessage()

    async def answer_photo(self, *args, **kwargs):
        self.calls.append("answer_photo")
        if self.send_fails:
            raise self._boom("wrong file identifier")
        return FakeMessage()

    async def delete(self):
        self.calls.append("delete")
        if self.delete_fails:
            raise self._boom("message can't be deleted")


async def test_sends_before_deleting() -> None:
    """Новое сообщение уходит раньше, чем исчезает старое."""
    message = FakeMessage(has_photo=False, edit_fails=True)

    await _edit_or_resend(message, "новый экран", None, photo=None)

    assert message.calls == ["edit_text", "answer", "delete"]
    assert message.calls.index("answer") < message.calls.index("delete")


async def test_old_message_survives_when_send_fails() -> None:
    """Главная проверка.

    Отправка нового экрана падает. Старый обязан остаться на месте: у человека
    должно быть хоть что-то, на что можно нажать.
    """
    message = FakeMessage(has_photo=False, edit_fails=True, send_fails=True)

    with pytest.raises(TelegramBadRequest):
        await _edit_or_resend(message, "новый экран", None, photo=None)

    assert "delete" not in message.calls, "старое сообщение удалили, а нового нет"


async def test_undeletable_old_message_is_not_fatal() -> None:
    """Сообщение старше 48 часов удалить нельзя — новое уже отправлено, не беда."""
    message = FakeMessage(has_photo=False, edit_fails=True, delete_fails=True)

    result = await _edit_or_resend(message, "новый экран", None, photo=None)

    assert result is not None
    assert message.calls == ["edit_text", "answer", "delete"]


async def test_unchanged_screen_is_not_an_error() -> None:
    """Повторное нажатие той же кнопки не должно ничего пересоздавать."""

    class NotModified(FakeMessage):
        async def edit_text(self, *args, **kwargs):
            self.calls.append("edit_text")
            raise TelegramBadRequest(
                method=None, message="Bad Request: message is not modified"
            )

    message = NotModified(has_photo=False)
    result = await _edit_or_resend(message, "тот же экран", None, photo=None)

    assert result is None
    assert message.calls == ["edit_text"]
    assert "delete" not in message.calls


async def test_text_to_photo_switch_resends() -> None:
    """Текстовое сообщение нельзя превратить в сообщение с картинкой правкой."""
    message = FakeMessage(has_photo=False, edit_fails=False)

    await _edit_or_resend(message, "экран с картинкой", None, photo="file-id-1")

    assert message.calls == ["answer_photo", "delete"]


async def test_photo_to_text_switch_resends() -> None:
    message = FakeMessage(has_photo=True, edit_fails=False)

    await _edit_or_resend(message, "экран без картинки", None, photo=None)

    assert message.calls == ["answer", "delete"]


async def test_same_photo_edits_only_the_caption() -> None:
    """Картинка та же — меняем подпись, а не гоняем медиа заново."""
    message = FakeMessage(has_photo=True, edit_fails=False, photo_id="file-id-1")

    result = await _edit_or_resend(message, "новая подпись", None, photo="file-id-1")

    assert result is None
    assert message.calls == ["edit_caption"]
    assert "delete" not in message.calls


async def test_other_photo_replaces_the_media() -> None:
    message = FakeMessage(has_photo=True, edit_fails=False, photo_id="file-id-old")

    result = await _edit_or_resend(message, "другая картинка", None, photo="file-id-new")

    assert result is None
    assert message.calls == ["edit_media"]
    assert "delete" not in message.calls
