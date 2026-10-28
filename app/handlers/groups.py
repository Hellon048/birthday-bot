"""Прикрепление групп для поздравлений сотрудников."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, ChatMemberUpdated, KeyboardButton,
                           KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import Private, Role
from ..db import DB
from ..notifier import Notifier
from ..ui import BTN_CANCEL, BTN_GROUPS, h, main_menu

router = Router(name="groups")
REQUEST_ID = 37


# ---------- события в самих группах (без проверки роли через фильтр) ----------
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, db: DB, notifier: Notifier):
    if event.chat.type not in ("group", "supergroup"):
        return
    adder = await db.user_by_tg(event.from_user.id)
    if adder and adder["is_active"] and adder["role"] in ("admin", "hr"):
        await db.upsert_chat(event.chat.id, event.chat.title or str(event.chat.id), adder["id"])
        await notifier.send(event.from_user.id,
                            f"✅ Группа «{h(event.chat.title)}» прикреплена. Сюда будут приходить "
                            "поздравления сотрудников. Фильтр по отделам — в «💬 Группы».")
    else:
        # бота добавил посторонний — уходим, чтобы не светить базу
        try:
            await event.bot.leave_chat(event.chat.id)
        except TelegramAPIError:
            pass


@router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, db: DB, notifier: Notifier):
    chat = await db.chat(event.chat.id)
    if chat and chat["is_active"]:
        await db.execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (event.chat.id,))
        for u in await notifier.hr_targets():
            await notifier.send(u["tg_id"], f"⚠️ Бота удалили из группы «{h(chat['title'])}» — "
                                            "поздравления туда больше не пойдут.")


@router.message(F.migrate_to_chat_id)
async def migrated(message: Message, db: DB):
    await db.execute("UPDATE chats SET chat_id=? WHERE chat_id=?",
                     (message.migrate_to_chat_id, message.chat.id))


# ---------- управление из лички ----------
staff = Router(name="groups_staff")
staff.message.filter(Private(), Role("admin", "hr"))
staff.callback_query.filter(Role("admin", "hr"))


async def groups_view(db: DB):
    chats = await db.chats()
    kb = InlineKeyboardBuilder()
    for c in chats:
        deps = [d for d in c["departments"].split("|") if d]
        label = "все отделы" if not deps else f"{len(deps)} отд."
        kb.button(text=f"💬 {c['title']} · {label}", callback_data=f"g:view:{c['chat_id']}")
    kb.button(text="➕ Прикрепить группу", callback_data="g:add")
    kb.adjust(1)
    text = "<b>Группы для поздравлений сотрудников</b>\n"
    text += ("Пока ни одной. " if not chats else "") + \
            "Чтобы прикрепить: добавьте бота в группу (сами, с этого аккаунта) — он прикрепится " \
            "автоматически. Или нажмите «Прикрепить», если бот там уже есть."
    return text, kb.as_markup()


@staff.message(F.text == BTN_GROUPS)
async def groups(message: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await groups_view(db)
    await message.answer(text, reply_markup=kb)


@staff.callback_query(F.data == "g:list")
async def groups_cb(cb: CallbackQuery, db: DB):
    text, kb = await groups_view(db)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@staff.callback_query(F.data == "g:add")
async def group_add(cb: CallbackQuery, bot: Bot):
    me = await bot.me()
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[
        [KeyboardButton(text="📎 Выбрать группу", request_chat=KeyboardButtonRequestChat(
            request_id=REQUEST_ID, chat_is_channel=False, bot_is_member=True, request_title=True))],
        [KeyboardButton(text=BTN_CANCEL)],
    ])
    invite = f"https://t.me/{me.username}?startgroup=bday"
    ikb = InlineKeyboardBuilder()
    ikb.button(text="➕ Добавить бота в новую группу", url=invite)
    await cb.message.answer(
        "Нажмите «📎 Выбрать группу» внизу — покажу группы, где бот уже состоит.\n"
        "Если бота там ещё нет — добавьте его кнопкой ниже, он прикрепится сам.",
        reply_markup=kb)
    await cb.message.answer("Добавить бота в группу:", reply_markup=ikb.as_markup())
    await cb.answer()


@staff.message(F.chat_shared)
async def group_shared(message: Message, bot: Bot, db: DB, me: dict):
    shared = message.chat_shared
    if shared.request_id != REQUEST_ID:
        return
    try:
        chat = await bot.get_chat(shared.chat_id)
        title = chat.title or shared.title or str(shared.chat_id)
    except TelegramAPIError:
        return await message.answer("Бот не видит эту группу — сначала добавьте его туда.",
                                    reply_markup=main_menu(me["role"]))
    await db.upsert_chat(shared.chat_id, title, me["id"])
    await message.answer(f"✅ Группа «{h(title)}» прикреплена.", reply_markup=main_menu(me["role"]))
    text, kb = await groups_view(db)
    await message.answer(text, reply_markup=kb)


async def group_card(db: DB, chat_id: int):
    c = await db.chat(chat_id)
    chosen = [d for d in c["departments"].split("|") if d]
    kb = InlineKeyboardBuilder()
    for i, d in enumerate((await db.departments())[:30]):
        kb.button(text=("✅ " if d in chosen else "▫️ ") + d, callback_data=f"g:dep:{chat_id}:{i}")
    kb.button(text="Все отделы", callback_data=f"g:all:{chat_id}")
    kb.button(text="📨 Тестовое сообщение", callback_data=f"g:test:{chat_id}")
    kb.button(text="🔌 Открепить", callback_data=f"g:off:{chat_id}")
    kb.button(text="⬅️ Назад", callback_data="g:list")
    kb.adjust(2)
    deps = ", ".join(chosen) if chosen else "все"
    return (f"<b>{h(c['title'])}</b>\nОтделы, которых поздравляем здесь: {h(deps)}\n\n"
            "Отметьте отделы, если у вас несколько групп (например, отдельный чат производства)."), \
        kb.as_markup()


@staff.callback_query(F.data.startswith("g:view:"))
async def group_view(cb: CallbackQuery, db: DB):
    text, kb = await group_card(db, int(cb.data.split(":")[2]))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@staff.callback_query(F.data.startswith("g:dep:"))
async def group_dep(cb: CallbackQuery, db: DB):
    _, _, chat_id, i = cb.data.split(":")
    chat_id = int(chat_id)
    c = await db.chat(chat_id)
    dep = (await db.departments())[int(i)]
    chosen = [d for d in c["departments"].split("|") if d]
    chosen = [d for d in chosen if d != dep] if dep in chosen else chosen + [dep]
    await db.execute("UPDATE chats SET departments=? WHERE chat_id=?", ("|".join(chosen), chat_id))
    text, kb = await group_card(db, chat_id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@staff.callback_query(F.data.startswith("g:all:"))
async def group_all(cb: CallbackQuery, db: DB):
    chat_id = int(cb.data.split(":")[2])
    await db.execute("UPDATE chats SET departments='' WHERE chat_id=?", (chat_id,))
    text, kb = await group_card(db, chat_id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@staff.callback_query(F.data.startswith("g:test:"))
async def group_test(cb: CallbackQuery, bot: Bot):
    chat_id = int(cb.data.split(":")[2])
    try:
        await bot.send_message(chat_id, "🎂 Бот дней рождения подключён. Здесь будут поздравления коллег.")
        await cb.answer("Отправлено")
    except TelegramAPIError as e:
        await cb.answer(f"Ошибка: {e.message}", show_alert=True)


@staff.callback_query(F.data.startswith("g:off:"))
async def group_off(cb: CallbackQuery, db: DB):
    chat_id = int(cb.data.split(":")[2])
    await db.execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (chat_id,))
    text, kb = await groups_view(db)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Откреплено")
