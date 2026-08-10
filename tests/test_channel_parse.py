"""Разбор ссылки на канал, которую присылает администратор.

Вариантов много, и каждый неверно разобранный превращается в неработающую
проверку подписки — то есть либо в открытый для всех магазин, либо в закрытый
для всех.
"""

from __future__ import annotations

import pytest

from bot.handlers.admin.content import _parse_channel_ref


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str | None = None, forward_from_chat=None) -> None:
        self.text = text
        self.caption = None
        self.forward_from_chat = forward_from_chat


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@shopnews", "@shopnews"),
        ("shopnews", "@shopnews"),
        ("https://t.me/shopnews", "@shopnews"),
        ("http://t.me/shopnews", "@shopnews"),
        ("t.me/shopnews", "@shopnews"),
        ("https://telegram.me/shopnews", "@shopnews"),
        ("https://t.me/shopnews/123", "@shopnews"),
        ("https://t.me/shopnews?start=x", "@shopnews"),
        ("  @shopnews  ", "@shopnews"),
        ("-1001234567890", "-1001234567890"),
        ("my_channel_2026", "@my_channel_2026"),
    ],
)
def test_recognises_every_common_form(raw: str, expected: str) -> None:
    assert _parse_channel_ref(FakeMessage(text=raw)) == expected


def test_forwarded_post_wins_over_text() -> None:
    """Пересылка — самый надёжный способ, особенно для приватного канала."""
    message = FakeMessage(text="какой-то текст", forward_from_chat=FakeChat(-1001112223334))
    assert _parse_channel_ref(message) == "-1001112223334"


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/+AbCdEfGhIj",
        "t.me/+AbCdEfGhIj",
        "https://t.me/joinchat/AAAAAE",
    ],
)
def test_private_invite_links_are_rejected(raw: str) -> None:
    """По пригласительной ссылке проверять подписку нельзя.

    В ней нет имени канала, а `getChatMember` требует именно его или числовой
    ID. Принять такую ссылку значит завести канал, который никогда не
    проверится, и узнать об этом от покупателей.
    """
    assert _parse_channel_ref(FakeMessage(text=raw)) is None


@pytest.mark.parametrize("raw", ["", "   ", "не ссылка вовсе", "https://example.com/channel"])
def test_garbage_is_rejected(raw: str) -> None:
    assert _parse_channel_ref(FakeMessage(text=raw)) is None


def test_empty_message_is_rejected() -> None:
    assert _parse_channel_ref(FakeMessage()) is None
