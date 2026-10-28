"""Кнопки под черновиками (HR) и под напоминаниями о партнёрах (менеджеры)."""
from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import Private, Role
from ..db import DB, now_iso
from ..notifier import Notifier
from ..ui import METHODS, MENU_BUTTONS, cancel_kb, h, is_staff, main_menu

router = Router(name="events")
router.callback_query.filter(Role())
router.message.filter(Private(), Role())


class DraftSG(StatesGroup):
    own_text = State()
    characteristic = State()


async def safe_edit(msg: Message, text: str, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def load_emp(cb: CallbackQuery, db: DB, me: dict):
    ev = await db.event(int(cb.data.split(":")[2]))
    if not ev:
        await cb.answer("Событие не найдено", show_alert=True)
        return None, None
    p = await db.person(ev["person_id"])
    if p["kind"] != "employee" or not is_staff(me):
        await cb.answer("Нет доступа", show_alert=True)
        return None, None
    return ev, p


def finished(ev: dict) -> str | None:
    if ev["status"] == "sent":
        return "Уже опубликовано"
    return None


async def refresh_draft(cb_msg: Message, notifier: Notifier, ev_id: int, p: dict):
    ev = await notifier.db.event(ev_id)
    markup = None if ev["status"] in ("sent", "skipped") else notifier.draft_kb(ev, bool(p.get("characteristic")))
    if ev["status"] == "skipped":
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ Всё-таки опубликовать", callback_data=f"ev:undo:{ev_id}")
        markup = kb.as_markup()
    await safe_edit(cb_msg, await notifier.draft_text_html(ev, p), markup)


async def maybe_publish_now(notifier: Notifier, ev_id: int, p: dict) -> bool:
    ev = await notifier.db.event(ev_id)
    if (ev["status"] == "approved" and date.fromisoformat(ev["celebrate_on"]) <= notifier.today()
            and notifier.past_post_time()):
        return bool(await notifier.publish(ev, p))
    return False


# ---------- сотрудники ----------
@router.callback_query(F.data.startswith("ev:ok:"))
async def ev_ok(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    if msg := finished(ev):
        return await cb.answer(msg, show_alert=True)
    if not ev["draft_text"]:
        return await cb.answer("Сначала нужен текст", show_alert=True)
    await db.update_event(ev["id"], status="approved", done_by=me["id"])
    await cb.answer("Одобрено")
    await maybe_publish_now(notifier, ev["id"], p)
    await refresh_draft(cb.message, notifier, ev["id"], p)


@router.callback_query(F.data.startswith("ev:re:") | F.data.startswith("ev:gen:"))
async def ev_regen(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    if msg := finished(ev):
        return await cb.answer(msg, show_alert=True)
    await cb.answer("Генерирую…")
    async with notifier.lock(ev["id"]):
        ev = await notifier.make_draft(ev, p)
    await refresh_draft(cb.message, notifier, ev["id"], p)


@router.callback_query(F.data.startswith("ev:no:"))
async def ev_skip(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    if msg := finished(ev):
        return await cb.answer(msg, show_alert=True)
    await db.update_event(ev["id"], status="skipped", done_by=me["id"])
    await cb.answer("Не публикуем")
    if ev["draft_text"]:
        await refresh_draft(cb.message, notifier, ev["id"], p)
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ Всё-таки опубликовать", callback_data=f"ev:undo:{ev['id']}")
        await safe_edit(cb.message, cb.message.html_text + "\n\n🚫 Публикация отменена.", kb.as_markup())


@router.callback_query(F.data.startswith("ev:undo:"))
async def ev_undo(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    if ev["status"] != "skipped":
        return await cb.answer("Уже не актуально")
    if not ev["draft_text"]:
        await cb.answer("Генерирую…")
        ev = await notifier.make_draft(ev, p)
    else:
        await db.update_event(ev["id"], status="draft")
        await cb.answer()
    await refresh_draft(cb.message, notifier, ev["id"], p)


@router.callback_query(F.data.startswith("ev:ed:"))
async def ev_own_text(cb: CallbackQuery, db: DB, me: dict, state: FSMContext):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    if msg := finished(ev):
        return await cb.answer(msg, show_alert=True)
    await state.set_state(DraftSG.own_text)
    await state.update_data(ev_id=ev["id"])
    await cb.message.answer(f"Пришлите текст поздравления для {h(p['full_name'])} целиком — "
                            "он уйдёт в группу без изменений.", reply_markup=cancel_kb())
    await cb.answer()


@router.message(DraftSG.own_text, F.text, ~F.text.in_(MENU_BUTTONS))
async def ev_own_text_set(message: Message, state: FSMContext, db: DB, me: dict, notifier: Notifier):
    ev_id = (await state.get_data())["ev_id"]
    await state.clear()
    ev = await db.event(ev_id)
    p = await db.person(ev["person_id"])
    await db.update_event(ev_id, draft_text=message.text.strip(), status="approved", done_by=me["id"])
    await message.answer("Текст сохранён ✅", reply_markup=main_menu(me["role"]))
    published = await maybe_publish_now(notifier, ev_id, p)
    ev = await db.event(ev_id)
    if not published:
        await message.answer(await notifier.draft_text_html(ev, p),
                             reply_markup=notifier.draft_kb(ev, bool(p.get("characteristic"))))


@router.callback_query(F.data.startswith("ev:ch:"))
async def ev_char(cb: CallbackQuery, db: DB, me: dict, state: FSMContext):
    ev, p = await load_emp(cb, db, me)
    if not ev:
        return
    await state.set_state(DraftSG.characteristic)
    await state.update_data(ev_id=ev["id"])
    cur = f"\nСейчас: <i>{h(p['characteristic'])}</i>" if p.get("characteristic") else ""
    await cb.message.answer(
        f"Пара слов о {h(p['full_name'])}: какой человек, за что ценят, чем увлекается.{cur}",
        reply_markup=cancel_kb())
    await cb.answer()


@router.message(DraftSG.characteristic, F.text, ~F.text.in_(MENU_BUTTONS))
async def ev_char_set(message: Message, state: FSMContext, db: DB, me: dict, notifier: Notifier):
    ev_id = (await state.get_data())["ev_id"]
    await state.clear()
    ev = await db.event(ev_id)
    await db.update_person(ev["person_id"], characteristic=message.text.strip()[:500])
    p = await db.person(ev["person_id"])
    await message.answer("Характеристика сохранена ✅", reply_markup=main_menu(me["role"]))
    if ev["status"] == "sent":
        return
    d = date.fromisoformat(ev["celebrate_on"])
    if (d - notifier.today()).days > 1:
        return await message.answer("Черновик пришлю накануне дня рождения.")
    wait = await message.answer("Генерирую поздравление…")
    async with notifier.lock(ev_id):
        ev = await notifier.make_draft(ev, p)
        await db.update_event(ev_id, hr_notified=1)
    await safe_edit(wait, await notifier.draft_text_html(ev, p), notifier.draft_kb(ev, True))


# ---------- партнёры ----------
async def load_partner(cb: CallbackQuery, db: DB, me: dict):
    ev = await db.event(int(cb.data.split(":")[2]))
    p = await db.person(ev["person_id"]) if ev else None
    if not ev or p["kind"] != "partner":
        await cb.answer("Не найдено", show_alert=True)
        return None, None
    if not (is_staff(me) or p["responsible_id"] == me["id"]):
        await cb.answer("Это не ваш партнёр", show_alert=True)
        return None, None
    return ev, p


@router.callback_query(F.data.startswith("pt:gen:"))
async def pt_gen(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_partner(cb, db, me)
    if not ev:
        return
    await cb.answer("Генерирую…")
    text = await notifier.gen.partner(await notifier.company(), p, me["name"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Другой вариант", callback_data=f"pt:gen:{ev['id']}")
    if ev["status"] != "done":
        kb.button(text="✅ Поздравил", callback_data=f"pt:done:{ev['id']}")
    await cb.message.answer(f"Текст для {h(p['full_name'])} — нажмите, чтобы скопировать:\n\n"
                            f"<code>{h(text)}</code>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("pt:done:"))
async def pt_done(cb: CallbackQuery, db: DB, me: dict):
    ev, p = await load_partner(cb, db, me)
    if not ev:
        return
    if ev["status"] == "done":
        return await cb.answer("Уже отмечено", show_alert=True)
    kb = InlineKeyboardBuilder()
    for code, label in METHODS.items():
        kb.button(text=label, callback_data=f"pt:m:{ev['id']}:{code}")
    kb.adjust(3)
    await cb.message.answer(f"Как поздравили {h(p['full_name'])}?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("pt:m:"))
async def pt_method(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    ev, p = await load_partner(cb, db, me)
    if not ev:
        return
    code = cb.data.split(":")[3]
    if ev["status"] == "done":
        return await cb.answer("Уже отмечено", show_alert=True)
    await db.update_event(ev["id"], status="done", method=METHODS.get(code, code),
                          done_by=me["id"], done_at=now_iso())
    await safe_edit(cb.message, f"✅ {h(p['full_name'])} — поздравлен ({METHODS.get(code, code)}). Спасибо!")
    await cb.answer("Отмечено")
    # если отметил не ответственный — сообщим ответственному
    if p["responsible_id"] and p["responsible_id"] != me["id"] and p.get("responsible_tg"):
        await notifier.send(p["responsible_tg"],
                            f"ℹ️ {h(me['name'])} отметил поздравление {h(p['full_name'])} "
                            f"({METHODS.get(code, code)}).")
