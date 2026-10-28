"""Доступ: middleware подкладывает текущего пользователя, фильтр проверяет роль."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        db = data["db"]
        user = data.get("event_from_user")
        me = None
        if user:
            me = await db.user_by_tg(user.id)
            if me and not me["is_active"]:
                me = None
            if me and user.username and me.get("username") != user.username:
                await db.update_user(me["id"], username=user.username)
        data["me"] = me
        return await handler(event, data)


class Role(Filter):
    """Role() — любой подключённый пользователь; Role('admin','hr') — только эти роли."""

    def __init__(self, *roles: str):
        self.roles = roles

    async def __call__(self, event: Message | CallbackQuery, me: dict | None = None) -> bool:
        if not me:
            return False
        return not self.roles or me["role"] in self.roles


class Private(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        return bool(chat) and chat.type == "private"
