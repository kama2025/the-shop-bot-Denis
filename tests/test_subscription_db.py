"""Проверка подписки на каналы."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.repo import content as content_repo
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.db


@dataclass
class FakeMember:
    status: str


class FakeBot:
    """Бот, которым управляет тест.

    `answers` — что вернуть по каждому каналу: строка-статус или исключение.
    """

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def get_chat_member(self, chat_id: str, user_id: int):
        self.calls.append(chat_id)
        answer = self.answers.get(chat_id, "left")
        if isinstance(answer, Exception):
            raise answer
        return FakeMember(status=str(answer))


def _broken() -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message="member list is inaccessible")


async def test_no_channels_means_open_shop(session) -> None:
    service = SubscriptionService(redis=None)
    result = await service.check(session, FakeBot({}), user_id=1)
    assert result.subscribed is True
    assert result.missing == []


@pytest.mark.parametrize("status", ["creator", "administrator", "member", "restricted"])
async def test_subscribed_statuses(session, status: str) -> None:
    await content_repo.add_channel(session, "@main", "Основной", "https://t.me/main")
    await session.commit()

    service = SubscriptionService(redis=None)
    result = await service.check(session, FakeBot({"@main": status}), user_id=1)
    assert result.subscribed is True


@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_unsubscribed_statuses(session, status: str) -> None:
    await content_repo.add_channel(session, "@main", "Основной", "https://t.me/main")
    await session.commit()

    service = SubscriptionService(redis=None)
    result = await service.check(session, FakeBot({"@main": status}), user_id=1)
    assert result.subscribed is False
    assert [channel.chat_ref for channel in result.missing] == ["@main"]


async def test_all_channels_are_required(session) -> None:
    await content_repo.add_channel(session, "@one", "Первый", "https://t.me/one")
    await content_repo.add_channel(session, "@two", "Второй", "https://t.me/two")
    await session.commit()

    service = SubscriptionService(redis=None)
    result = await service.check(
        session, FakeBot({"@one": "member", "@two": "left"}), user_id=1
    )
    assert result.subscribed is False
    assert [channel.chat_ref for channel in result.missing] == ["@two"]


async def test_broken_channel_does_not_close_the_shop(session) -> None:
    """Бота выкинули из администраторов канала.

    Считать это «не подписан» — значит закрыть магазин для всех сразу. Ложный
    отказ ломает работу так же, как дыра, поэтому такой канал пропускается,
    а ошибка сохраняется для админов.
    """
    await content_repo.add_channel(session, "@broken", "Сломанный", "https://t.me/broken")
    await session.commit()

    service = SubscriptionService(redis=None)
    result = await service.check(session, FakeBot({"@broken": _broken()}), user_id=1)

    assert result.subscribed is True
    assert result.has_broken is True

    channels = await content_repo.list_channels(session, only_active=False)
    assert channels[0].last_error is not None


async def test_broken_channel_does_not_hide_a_real_miss(session) -> None:
    """Сломанный канал не должен маскировать честное «не подписан» на другом."""
    await content_repo.add_channel(session, "@broken", "Сломанный", "https://t.me/broken")
    await content_repo.add_channel(session, "@real", "Рабочий", "https://t.me/real")
    await session.commit()

    service = SubscriptionService(redis=None)
    result = await service.check(
        session, FakeBot({"@broken": _broken(), "@real": "left"}), user_id=1
    )
    assert result.subscribed is False
    assert [channel.chat_ref for channel in result.missing] == ["@real"]


async def test_disabled_channel_is_skipped(session) -> None:
    channel = await content_repo.add_channel(session, "@off", "Выключен", "https://t.me/off")
    channel.is_active = False
    await session.commit()

    service = SubscriptionService(redis=None)
    bot = FakeBot({"@off": "left"})
    result = await service.check(session, bot, user_id=1)

    assert result.subscribed is True
    assert bot.calls == [], "выключенный канал не должен даже проверяться"


async def test_error_clears_after_successful_check(session) -> None:
    await content_repo.add_channel(session, "@flaky", "Мигающий", "https://t.me/flaky")
    await session.commit()
    service = SubscriptionService(redis=None)

    await service.check(session, FakeBot({"@flaky": _broken()}), user_id=1)
    await session.commit()
    channels = await content_repo.list_channels(session, only_active=False)
    assert channels[0].last_error is not None

    await service.check(session, FakeBot({"@flaky": "member"}), user_id=1)
    await session.commit()
    channels = await content_repo.list_channels(session, only_active=False)
    assert channels[0].last_error is None
