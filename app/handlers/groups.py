"""Прикрепление групп для поздравлений сотрудников."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
import logging

from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, ChatMemberUpdated, KeyboardButton,
                           KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import Private, Role
from ..db import DB
from ..notifier import Notifier
from ..ui import BTN_CANCEL, BTN_GROUPS, h, main_menu

log = logging.getLogger(__name__)
router = Router(name="groups")
REQUEST_ID = 37


GROUP_TYPES = ("group", "supergroup")


def _staff(u: dict | None) -> bool:
    return bool(u) and u["is_active"] and u["role"] in ("admin", "hr")


async def attach(db: DB, notifier: Notifier, chat_id: int, title: str, user: dict) -> None:
    await db.upsert_chat(chat_id, title, user["id"])
    log.info("Группа %s «%s» прикреплена пользователем %s", chat_id, title, user["name"])
    if user["tg_id"]:
        await notifier.send(user["tg_id"],
                            f"✅ Группа «{h(title)}» прикреплена. Сюда будут приходить поздравления "
                            "сотрудников. Фильтр по отделам — в «💬 Группы».")


async def ask_admins(db: DB, notifier: Notifier, chat_id: int, title: str, who: str) -> None:
    """Бота добавил кто-то, кого бот не знает (или анонимный админ) — спрашиваем админов."""
    await db.execute(
        "INSERT INTO chats(chat_id, title, is_active, created_at) VALUES (?, ?, 0, datetime('now')) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title", (chat_id, title))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Прикрепить", callback_data=f"g:accept:{chat_id}")
    kb.button(text="🚪 Выйти из группы", callback_data=f"g:leave:{chat_id}")
    for u in await db.users(roles=["admin", "hr"], only_linked=True):
        await notifier.send(u["tg_id"],
                            f"Меня добавили в группу «{h(title)}» ({h(who)}). Прикрепить её для "
                            "поздравлений сотрудников?", kb.as_markup())


# ---------- события в самих группах ----------
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, db: DB, notifier: Notifier):
    fu = event.from_user
    log.info("Бота добавили в %s %s «%s», добавил %s (@%s)",
             event.chat.type, event.chat.id, event.chat.title, fu.id, fu.username)
    if event.chat.type not in GROUP_TYPES:
        return
    title = event.chat.title or str(event.chat.id)
    adder = await db.user_by_tg(fu.id)
    if _staff(adder):
        await attach(db, notifier, event.chat.id, title, adder)
    else:
        who = "анонимный администратор" if fu.username == "GroupAnonymousBot" else \
            (f"@{fu.username}" if fu.username else fu.full_name)
        await ask_admins(db, notifier, event.chat.id, title, who)


@router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, db: DB, notifier: Notifier):
    log.info("Бота удалили из %s «%s»", event.chat.id, event.chat.title)
    chat = await db.chat(event.chat.id)
    if chat and chat["is_active"]:
        await db.execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (event.chat.id,))
        for u in await notifier.hr_targets():
            await notifier.send(u["tg_id"], f"⚠️ Бота удалили из группы «{h(chat['title'])}» — "
                                            "поздравления туда больше не пойдут.")


@router.message(F.migrate_to_chat_id)
async def migrated(message: Message, db: DB):
    # группа стала супергруппой (например, бота сделали админом) — id меняется
    log.info("Группа %s мигрировала в %s", message.chat.id, message.migrate_to_chat_id)
    await db.execute("UPDATE chats SET chat_id=? WHERE chat_id=?",
                     (message.migrate_to_chat_id, message.chat.id))


@router.message(F.chat.type.in_(GROUP_TYPES), Command("bind", "start"))
async def bind_in_group(message: Message, db: DB, notifier: Notifier, me: dict | None):
    """/bind в группе (или /start после ссылки «добавить в группу») — прикрепить группу."""
    title = message.chat.title or str(message.chat.id)
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return await message.reply("Вы пишете как анонимный администратор — я не вижу, кто это. "
                                   "Отключите анонимность или прикрепите группу из лички бота.")
    if not _staff(me):
        if message.text.startswith("/bind"):
            await message.reply("Прикрепить группу может только администратор или HR бота.")
        return
    already = await db.chat(message.chat.id)
    if already and already["is_active"]:
        return await message.reply("Группа уже прикреплена ✅")
    await attach(db, notifier, message.chat.id, title, me)
    await message.reply("✅ Готово, буду поздравлять здесь коллег с днём рождения.")


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
            "Как прикрепить:\n" \
            "• «➕ Прикрепить группу» → добавить бота в группу — прикрепится сам;\n" \
            "• или напишите в группе команду <code>/bind</code>."
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
            request_id=REQUEST_ID, chat_is_channel=False, request_title=True))],
        [KeyboardButton(text=BTN_CANCEL)],
    ])
    invite = f"https://t.me/{me.username}?startgroup=bday"
    ikb = InlineKeyboardBuilder()
    ikb.button(text="➕ Добавить бота в группу", url=invite)
    await cb.message.answer(
        "Два способа:\n"
        "1. «➕ Добавить бота в группу» (кнопка ниже) — выберите группу, бот прикрепится сам.\n"
        "2. Если бот уже в группе — «📎 Выбрать группу» внизу экрана или команда "
        "<code>/bind</code> в самой группе.",
        reply_markup=kb)
    await cb.message.answer("Добавить бота в группу:", reply_markup=ikb.as_markup())
    await cb.answer()


@staff.message(F.chat_shared)
async def group_shared(message: Message, bot: Bot, db: DB, me: dict):
    shared = message.chat_shared
    if shared.request_id != REQUEST_ID:
        return
    log.info("Выбрана группа %s «%s»", shared.chat_id, shared.title)
    try:
        chat = await bot.get_chat(shared.chat_id)
        title = chat.title or shared.title or str(shared.chat_id)
    except TelegramAPIError as e:
        log.info("Бот не состоит в группе %s: %s", shared.chat_id, e)
        bot_me = await bot.me()
        ikb = InlineKeyboardBuilder()
        ikb.button(text="➕ Добавить бота в группу", url=f"https://t.me/{bot_me.username}?startgroup=bday")
        await message.answer("Меня ещё нет в этой группе.", reply_markup=main_menu(me["role"]))
        return await message.answer("Добавьте бота — после этого группа прикрепится сама:",
                                    reply_markup=ikb.as_markup())
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


@staff.callback_query(F.data.startswith("g:accept:"))
async def group_accept(cb: CallbackQuery, bot: Bot, db: DB, me: dict, notifier: Notifier):
    chat_id = int(cb.data.split(":")[2])
    c = await db.chat(chat_id)
    try:
        await bot.get_chat(chat_id)
    except TelegramAPIError:
        await cb.message.edit_reply_markup()
        return await cb.answer("Бота уже нет в этой группе", show_alert=True)
    await attach(db, notifier, chat_id, c["title"] if c else str(chat_id), me)
    await cb.message.edit_reply_markup()
    await cb.answer("Прикреплено")


@staff.callback_query(F.data.startswith("g:leave:"))
async def group_leave(cb: CallbackQuery, bot: Bot, db: DB):
    chat_id = int(cb.data.split(":")[2])
    try:
        await bot.leave_chat(chat_id)
    except TelegramAPIError:
        pass
    await db.execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (chat_id,))
    await cb.message.edit_text(cb.message.html_text + "\n\n🚪 Вышел из группы.")
    await cb.answer()
