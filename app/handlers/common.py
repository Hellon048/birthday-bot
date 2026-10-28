"""/start, приём приглашений, меню, отмена, «нет доступа»."""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from ..access import Private
from ..db import DB, now_iso
from ..notifier import Notifier
from ..ui import BTN_CANCEL, ROLE_LABEL, h, main_menu

router = Router(name="common")
router.message.filter(Private())

fallback = Router(name="fallback")


HELP = {
    "admin": "Вы администратор: управляете доступами, группами и всеми карточками.",
    "hr": "Вы HR: ведёте сотрудников, подтверждаете поздравления в группе.",
    "manager": "Вы менеджер: бот напомнит о днях рождения ваших партнёров "
               "за 7 дней, за день и в сам день.",
}


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("inv_")))
async def accept_invite(message: Message, command: CommandObject, db: DB, notifier: Notifier,
                        me: dict | None, state: FSMContext):
    await state.clear()
    token = command.args[4:]
    inv = await db.invite(token)
    tg = message.from_user
    if not inv:
        return await message.answer("Ссылка недействительна. Попросите администратора прислать новую.")
    if inv["used_at"]:
        if inv["used_by_tg"] == tg.id and me:
            return await message.answer("Вы уже подключены.", reply_markup=main_menu(me["role"]))
        return await message.answer("Эта ссылка уже использована. Попросите новую.")
    if datetime.fromisoformat(inv["expires_at"]) < datetime.now():
        return await message.answer("Срок действия ссылки истёк. Попросите администратора прислать новую.")
    user = await db.user(inv["user_id"])
    if not user or not user["is_active"]:
        return await message.answer("Доступ для этой ссылки отключён.")
    existing = await db.user_by_tg(tg.id)
    if existing and existing["id"] != user["id"]:
        return await message.answer(
            f"Ваш Telegram уже привязан к «{h(existing['name'])}». Обратитесь к администратору.")
    if user["tg_id"] and user["tg_id"] != tg.id:
        return await message.answer("К этой записи уже привязан другой Telegram-аккаунт.")

    await db.update_user(user["id"], tg_id=tg.id, username=tg.username)
    await db.execute("UPDATE invites SET used_at=?, used_by_tg=? WHERE id=?",
                     (now_iso(), tg.id, inv["id"]))
    user = await db.user(user["id"])
    cnt = await db.fetchone("SELECT COUNT(*) c FROM persons WHERE responsible_id=? AND is_active=1",
                            (user["id"],))
    extra = f"\nЗа вами закреплено партнёров: {cnt['c']}." if cnt["c"] else ""
    await message.answer(
        f"Готово, {h(user['name'])}! Роль: {ROLE_LABEL[user['role']]}.\n{HELP[user['role']]}{extra}",
        reply_markup=main_menu(user["role"]))
    if inv["created_by"]:
        creator = await db.user(inv["created_by"])
        if creator and creator["tg_id"]:
            await notifier.send(creator["tg_id"],
                                f"✅ {h(user['name'])} подключился по приглашению"
                                + (f" (@{h(tg.username)})" if tg.username else "") + ".")
    # если у него завтра/сегодня кто-то — сразу пришлём
    for p in await db.persons(responsible_id=user["id"]):
        await notifier.process_person(p)


@router.message(CommandStart())
async def start(message: Message, me: dict | None, state: FSMContext):
    await state.clear()
    if not me:
        return await message.answer(
            "Это закрытый бот дней рождения компании.\n"
            "Чтобы получить доступ, попросите администратора прислать ссылку-приглашение.\n\n"
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>")
    await message.answer(f"Здравствуйте, {h(me['name'])}! {HELP[me['role']]}",
                         reply_markup=main_menu(me["role"]))


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel(message: Message, state: FSMContext, me: dict | None):
    await state.clear()
    if me:
        await message.answer("Отменено.", reply_markup=main_menu(me["role"]))
    else:
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("menu"))
async def menu(message: Message, me: dict | None, state: FSMContext):
    await state.clear()
    if me:
        await message.answer("Меню:", reply_markup=main_menu(me["role"]))


@router.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


# ---- последний рубеж: всё, что не обработали ----
@fallback.message(F.chat.type == "private")
async def no_access(message: Message, me: dict | None):
    if not me:
        await message.answer("Нет доступа. Попросите администратора прислать ссылку-приглашение.")
    else:
        await message.answer("Не понял. Воспользуйтесь меню 👇", reply_markup=main_menu(me["role"]))


@fallback.callback_query()
async def cb_no_access(cb: CallbackQuery, me: dict | None):
    await cb.answer("Нет доступа к этому действию" if not me else "Кнопка устарела", show_alert=not me)
