"""Доступы (приглашения по ссылке), настройки."""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import Private, Role
from ..config import Config
from ..db import DB
from ..notifier import Notifier
from ..importer import NotLegacyDB, detect, make_backup, merge_legacy, restore_backup
from ..ui import BTN_SETTINGS, BTN_USERS, MENU_BUTTONS, ROLE_LABEL, cancel_kb, h, main_menu

router = Router(name="admin")
router.message.filter(Private(), Role("admin"))
router.callback_query.filter(Role("admin"))


class InviteSG(StatesGroup):
    name = State()


class SettingsSG(StatesGroup):
    company = State()


# ---------- список пользователей ----------
async def users_view(db: DB):
    users = await db.users(only_active=False)
    kb = InlineKeyboardBuilder()
    for u in users:
        icon = "🚫" if not u["is_active"] else ("✅" if u["tg_id"] else "⏳")
        kb.button(text=f"{icon} {u['name']} · {ROLE_LABEL[u['role']]}", callback_data=f"u:view:{u['id']}")
    kb.button(text="➕ Пригласить", callback_data="u:new")
    kb.adjust(1)
    text = ("<b>Доступы</b>\n✅ подключён · ⏳ ждёт перехода по ссылке · 🚫 отключён\n\n"
            "Ответственные из старой базы уже заведены — откройте человека и отправьте ему ссылку.")
    return text, kb.as_markup()


@router.message(F.text == BTN_USERS)
async def users_list(message: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await users_view(db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "u:list")
async def users_list_cb(cb: CallbackQuery, db: DB):
    text, kb = await users_view(db)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


async def user_card(db: DB, uid: int, me: dict):
    u = await db.user(uid)
    cnt = (await db.fetchone(
        "SELECT COUNT(*) c FROM persons WHERE responsible_id=? AND is_active=1", (uid,)))["c"]
    status = "🚫 отключён" if not u["is_active"] else ("✅ подключён" if u["tg_id"] else "⏳ не подключён")
    lines = [f"<b>{h(u['name'])}</b>", f"Роль: {ROLE_LABEL[u['role']]}", f"Статус: {status}"]
    if u["username"]:
        lines.append(f"@{h(u['username'])}")
    if cnt:
        lines.append(f"Партнёров на нём: {cnt}")
    kb = InlineKeyboardBuilder()
    if u["is_active"] and not u["tg_id"]:
        kb.button(text="🔗 Ссылка-приглашение", callback_data=f"u:link:{uid}")
    for r in ("admin", "hr", "manager"):
        if r != u["role"]:
            kb.button(text=f"Сделать: {ROLE_LABEL[r]}", callback_data=f"u:role:{uid}:{r}")
    if cnt:
        kb.button(text="🔁 Передать партнёров", callback_data=f"u:tr:{uid}")
    if u["id"] != me["id"]:
        kb.button(text="🚫 Отключить" if u["is_active"] else "✅ Включить", callback_data=f"u:act:{uid}")
    if u["tg_id"] and u["id"] != me["id"]:
        kb.button(text="🔓 Отвязать Telegram", callback_data=f"u:unlink:{uid}")
    kb.button(text="⬅️ Назад", callback_data="u:list")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.callback_query(F.data.startswith("u:view:"))
async def user_view(cb: CallbackQuery, db: DB, me: dict):
    text, kb = await user_card(db, int(cb.data.split(":")[2]), me)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:role:"))
async def user_role(cb: CallbackQuery, db: DB, me: dict):
    _, _, uid, role = cb.data.split(":")
    if int(uid) == me["id"] and role != "admin":
        return await cb.answer("Нельзя снять роль администратора с себя", show_alert=True)
    await db.update_user(int(uid), role=role)
    text, kb = await user_card(db, int(uid), me)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Роль изменена")


@router.callback_query(F.data.startswith("u:act:"))
async def user_toggle(cb: CallbackQuery, db: DB, me: dict):
    uid = int(cb.data.split(":")[2])
    u = await db.user(uid)
    await db.update_user(uid, is_active=0 if u["is_active"] else 1)
    text, kb = await user_card(db, uid, me)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:unlink:"))
async def user_unlink(cb: CallbackQuery, db: DB, me: dict):
    uid = int(cb.data.split(":")[2])
    await db.update_user(uid, tg_id=None, username=None)
    text, kb = await user_card(db, uid, me)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Telegram отвязан — можно выслать новую ссылку")


@router.callback_query(F.data.startswith("u:tr:"))
async def user_transfer(cb: CallbackQuery, db: DB):
    uid = int(cb.data.split(":")[2])
    kb = InlineKeyboardBuilder()
    for u in await db.users():
        if u["id"] != uid:
            kb.button(text=f"{u['name']} · {ROLE_LABEL[u['role']]}", callback_data=f"u:trto:{uid}:{u['id']}")
    kb.button(text="⬅️ Назад", callback_data=f"u:view:{uid}")
    kb.adjust(1)
    await cb.message.edit_text("Кому передать всех партнёров?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("u:trto:"))
async def user_transfer_do(cb: CallbackQuery, db: DB, me: dict):
    _, _, src, dst = cb.data.split(":")
    await db.execute("UPDATE persons SET responsible_id=? WHERE responsible_id=?", (int(dst), int(src)))
    text, kb = await user_card(db, int(src), me)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Партнёры переданы")


# ---------- приглашения ----------
async def send_link(target: Message, bot: Bot, db: DB, cfg: Config, user: dict, me: dict,
                    edit: bool = False):
    token = await db.create_invite(user["id"], me["id"], cfg.invite_ttl_days)
    bot_me = await bot.me()
    link = f"https://t.me/{bot_me.username}?start=inv_{token}"
    share = ("https://t.me/share/url?url=" + quote(link) + "&text="
             + quote("Подключитесь к боту дней рождения компании"))
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Переслать в Telegram", url=share)
    kb.button(text="⬅️ К пользователю", callback_data=f"u:view:{user['id']}")
    kb.adjust(1)
    text = (f"Ссылка для <b>{h(user['name'])}</b> ({ROLE_LABEL[user['role']]}):\n\n"
            f"{link}\n\nОдноразовая, действует {cfg.invite_ttl_days} дн. "
            "Человек открывает её, нажимает «Старт» — и сразу получает доступ.")
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("u:link:"))
async def user_link(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, me: dict):
    user = await db.user(int(cb.data.split(":")[2]))
    await send_link(cb.message, bot, db, cfg, user, me, edit=True)
    await cb.answer()


@router.callback_query(F.data == "u:new")
async def invite_start(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="Менеджер (свои партнёры)", callback_data="inv:role:manager")
    kb.button(text="HR (сотрудники и поздравления)", callback_data="inv:role:hr")
    kb.button(text="Администратор (всё)", callback_data="inv:role:admin")
    kb.button(text="⬅️ Назад", callback_data="u:list")
    kb.adjust(1)
    await cb.message.edit_text("Кого приглашаем?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("inv:role:"))
async def invite_role(cb: CallbackQuery, db: DB, state: FSMContext):
    role = cb.data.split(":")[2]
    placeholders = [u for u in await db.users(roles=[role]) if not u["tg_id"]]
    await state.update_data(invite_role=role)
    if placeholders:
        kb = InlineKeyboardBuilder()
        for u in placeholders:
            kb.button(text=f"⏳ {u['name']}", callback_data=f"u:link:{u['id']}")
        kb.button(text="➕ Новый человек", callback_data="inv:new")
        kb.adjust(1)
        return await cb.message.edit_text(
            "Это кто-то из уже заведённых (из старой базы)? Тогда за ним сразу закрепятся его партнёры.",
            reply_markup=kb.as_markup())
    await state.set_state(InviteSG.name)
    await cb.message.answer("Как зовут человека? (Фамилия Имя — так он будет в списке ответственных)",
                            reply_markup=cancel_kb())
    await cb.answer()


@router.callback_query(F.data == "inv:new")
async def invite_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(InviteSG.name)
    await cb.message.answer("Как зовут человека? (Фамилия Имя)", reply_markup=cancel_kb())
    await cb.answer()


@router.message(InviteSG.name, F.text, ~F.text.in_(MENU_BUTTONS))
async def invite_name(message: Message, bot: Bot, db: DB, cfg: Config, state: FSMContext, me: dict):
    data = await state.get_data()
    role = data.get("invite_role", "manager")
    name = " ".join(message.text.split())[:80]
    uid = await db.create_user(name, role)
    await state.clear()
    await message.answer("Создано.", reply_markup=main_menu(me["role"]))
    await send_link(message, bot, db, cfg, await db.user(uid), me)


# ---------- настройки ----------
async def settings_view(db: DB, cfg: Config, notifier: Notifier):
    company = await db.get_setting("company_name")
    feb = await db.get_setting("feb29_rule", "28.02")
    ai = f"Claude ({cfg.anthropic_model})" if notifier.gen.enabled else "выключена (нет ANTHROPIC_API_KEY) — шаблоны"
    text = (f"<b>Настройки</b>\n"
            f"Компания: {h(company)}\n"
            f"29 февраля в невисокосный год: {feb}\n"
            f"Генерация: {h(ai)}\n\n"
            f"Расписание ({cfg.tz.key}):\n"
            f"• {cfg.morning_time:%H:%M} — напоминания и черновики для HR\n"
            f"• {cfg.post_time:%H:%M} — публикация поздравлений в группах\n"
            f"• {cfg.escalate_time:%H:%M} — сводка неотмеченных партнёров админам\n"
            f"• сводка на неделю — по {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][cfg.digest_weekday]}\n"
            f"<i>Время меняется в .env</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Название компании", callback_data="s:company")
    kb.button(text=f"29.02 → {'01.03' if feb == '28.02' else '28.02'}", callback_data="s:feb29")
    kb.button(text="🧪 Тест генерации", callback_data="s:test")
    kb.button(text="▶️ Проверить дни рождения сейчас", callback_data="s:run")
    kb.button(text="📥 Загрузить базу (.db)", callback_data="s:upload")
    kb.button(text="💾 Резервная копия", callback_data="s:backup")
    kb.adjust(1)
    return text, kb.as_markup()


@router.message(F.text == BTN_SETTINGS)
async def settings(message: Message, db: DB, cfg: Config, notifier: Notifier, state: FSMContext):
    await state.clear()
    text, kb = await settings_view(db, cfg, notifier)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "s:feb29")
async def settings_feb(cb: CallbackQuery, db: DB, cfg: Config, notifier: Notifier):
    cur = await db.get_setting("feb29_rule", "28.02")
    await db.set_setting("feb29_rule", "01.03" if cur == "28.02" else "28.02")
    text, kb = await settings_view(db, cfg, notifier)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "s:company")
async def settings_company(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsSG.company)
    await cb.message.answer("Новое название компании (так оно попадёт в поздравления):",
                            reply_markup=cancel_kb())
    await cb.answer()


@router.message(SettingsSG.company, F.text, ~F.text.in_(MENU_BUTTONS))
async def settings_company_set(message: Message, db: DB, state: FSMContext, me: dict):
    await db.set_setting("company_name", message.text.strip()[:120])
    await state.clear()
    await message.answer("Сохранено.", reply_markup=main_menu(me["role"]))


@router.callback_query(F.data == "s:test")
async def settings_test(cb: CallbackQuery, db: DB, notifier: Notifier):
    await cb.answer("Генерирую…")
    p = await db.fetchone("SELECT * FROM persons WHERE kind='employee' AND is_active=1 "
                          "ORDER BY RANDOM() LIMIT 1")
    if not p:
        return await cb.message.answer("Нет сотрудников для теста.")
    text = await notifier.gen.employee(await notifier.company(), p, [])
    await cb.message.answer(f"Пример для {h(p['full_name'])} (никуда не отправлено):\n\n"
                            f"<blockquote>{h(text)}</blockquote>")


@router.callback_query(F.data == "s:run")
async def settings_run(cb: CallbackQuery, notifier: Notifier):
    await cb.answer("Запускаю проверку…")
    await notifier.morning()
    await cb.message.answer("Готово: напоминания и черновики разосланы (уже отправленное не дублируется).")


# ---------- загрузка базы и резервные копии ----------
def _dirs(cfg: Config) -> tuple[str, str]:
    base = os.path.dirname(os.path.abspath(cfg.db_path))
    return os.path.join(base, "uploads"), os.path.join(base, "backups")


def _upload_path(cfg: Config, token: str) -> str | None:
    if not re.fullmatch(r"\d{8}_\d{6}", token):
        return None
    return os.path.join(_dirs(cfg)[0], f"{token}.db")


@router.callback_query(F.data == "s:upload")
async def upload_help(cb: CallbackQuery):
    await cb.message.answer(
        "Отправьте сюда файл базы документом:\n"
        "• <b>старая база</b> (birthday_calendar.db) — данные сольются с ботом: новые люди добавятся, "
        "изменённые обновятся, правки из бота не затрутся. Перед применением покажу, что изменится;\n"
        "• <b>резервная копия бота</b> — можно восстановить всё целиком.")
    await cb.answer()


@router.callback_query(F.data == "s:backup")
async def backup_send(cb: CallbackQuery, cfg: Config):
    await cb.answer("Готовлю копию…")
    path = await asyncio.to_thread(make_backup, cfg.db_path, _dirs(cfg)[1], "manual", 5)
    await cb.message.answer_document(
        FSInputFile(path, filename=os.path.basename(path)),
        caption="💾 Резервная копия. В ней персональные данные — не пересылайте и не храните в открытых местах.")


@router.message(F.document)
async def upload_db(message: Message, bot: Bot, cfg: Config, state: FSMContext):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".db", ".sqlite", ".sqlite3")):
        return await message.answer("Нужен файл базы .db")
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        return await message.answer("Файл больше 20 МБ — Telegram не даст боту его скачать.")
    await state.clear()
    token = f"{datetime.now():%Y%m%d_%H%M%S}"
    path = _upload_path(cfg, token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    await bot.download(doc, destination=path)
    try:
        kind = await asyncio.to_thread(detect, path)
        kb = InlineKeyboardBuilder()
        if kind == "legacy":
            rep = await asyncio.to_thread(merge_legacy, path, cfg.db_path, False)
            kb.button(text="✅ Применить", callback_data=f"imp:apply:{token}")
            text = "🔎 <b>Пробный прогон</b> — пока ничего не записано.\n\n" + h(rep.text())
        else:
            kb.button(text="♻️ Восстановить из копии", callback_data=f"imp:restore:{token}")
            text = ("Это резервная копия бота. Восстановление <b>заменит всё</b>: людей, доступы, "
                    "группы и историю. Текущее состояние я сначала сохраню в копию.")
        kb.button(text="✖️ Отмена", callback_data=f"imp:cancel:{token}")
        kb.adjust(1)
        await message.answer(text[:4000], reply_markup=kb.as_markup())
    except NotLegacyDB as e:
        os.remove(path)
        await message.answer(f"⚠️ {h(e)}")


@router.callback_query(F.data.startswith("imp:"))
async def upload_action(cb: CallbackQuery, cfg: Config, notifier: Notifier, db: DB):
    _, action, token = cb.data.split(":")
    path = _upload_path(cfg, token)
    if not path or not os.path.exists(path):
        return await cb.answer("Файл уже обработан или устарел — отправьте заново", show_alert=True)
    await cb.message.edit_reply_markup()
    if action == "cancel":
        os.remove(path)
        return await cb.answer("Отменено")
    await cb.answer("Выполняю…")
    backup = await asyncio.to_thread(make_backup, cfg.db_path, _dirs(cfg)[1], "import", 10)
    try:
        if action == "apply":
            rep = await asyncio.to_thread(merge_legacy, path, cfg.db_path, True)
            text = "✅ <b>Импорт применён.</b>\n\n" + h(rep.text())
        else:
            await asyncio.to_thread(restore_backup, path, cfg.db_path)
            await db.ensure_admins(cfg.admin_ids)  # админы из .env не должны потерять доступ
            text = "✅ База восстановлена из копии."
    except Exception as e:
        return await cb.message.answer(f"⚠️ Ошибка: {h(e)}. База не изменена.")
    finally:
        if os.path.exists(path):
            os.remove(path)
    await cb.message.answer(text[:4000] + f"\n\nПредыдущее состояние сохранено: {os.path.basename(backup)}")
    await notifier.morning()  # новые люди с близким ДР получат напоминания сразу
