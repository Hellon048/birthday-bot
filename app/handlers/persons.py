"""Добавление, поиск, карточки, редактирование, ближайшие дни рождения."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import Private, Role
from ..dates import DateParseError, fmt_birthday, fmt_short, next_occurrence, parse_birthday
from ..db import DB, EMPLOYEE_FIELDS, PARTNER_FIELDS
from ..notifier import Notifier
from ..ui import (BTN_ADD, BTN_SEARCH, BTN_UPCOMING, KIND_LABEL, MENU_BUTTONS, ROLE_LABEL,
                  can_access, cancel_kb, h, is_staff, main_menu, person_card, person_kb,
                  person_title)

router = Router(name="persons")
router.message.filter(Private(), Role())
router.callback_query.filter(Role())

STEPS = {
    "employee": ["full_name", "birthday", "position", "department", "characteristic"],
    "partner": ["full_name", "birthday", "company", "responsible_id", "gift_format", "comment"],
}
OPTIONAL = {"position", "department", "characteristic", "gift_format", "comment"}
GIFTS = ["мерч", "подарок", "сообщение", "звонок", "открытка"]
PROMPTS = {
    ("employee", "full_name"): "ФИО полностью, например: <i>Иванова Мария Сергеевна</i>",
    ("partner", "full_name"): "ФИО партнёра. Если знаете только имя — имя и пометку, "
                              "например: <i>Игорь (РОП)</i>",
    ("*", "birthday"): "Дата рождения: <i>14.01.1991</i>, или <i>14.01</i> / <i>14 января</i>, "
                       "если год неизвестен",
    ("*", "position"): "Должность",
    ("*", "department"): "Отдел — выберите или напишите новый",
    ("*", "characteristic"): "Пара слов о человеке для поздравления: какой он, за что ценят "
                             "коллеги. Например: <i>душа склада, всегда выручит, любит рыбалку</i>",
    ("*", "company"): "Компания и город, например: <i>Хука простор (Великий Новгород)</i>\n"
                      "Обязательно — иначе по одному имени не понять, кто это.",
    ("*", "responsible_id"): "Кто отвечает за поздравление?",
    ("*", "gift_format"): "Что дарим / как поздравляем — выберите или напишите",
    ("*", "comment"): "Комментарий (что важно знать менеджеру)",
    ("*", "email"): "Email",
    ("*", "phone"): "Телефон",
    ("*", "messenger"): "Мессенджер (Telegram, WhatsApp, @username…)",
    ("*", "category"): "Категория (ключевой, постоянный…)",
    ("*", "contact_type"): "Тип: клиент или партнёр",
    ("*", "congrat_method"): "Как поздравляем (сообщение, звонок, открытка…)",
    ("*", "congratulator"): "Кто поздравляет (ОП, руководитель…)",
}


class AddSG(StatesGroup):
    step = State()
    confirm = State()


class EditSG(StatesGroup):
    value = State()


class SearchSG(StatesGroup):
    query = State()


def prompt(kind: str, field: str) -> str:
    return PROMPTS.get((kind, field)) or PROMPTS[("*", field)]


async def choice_kb(db: DB, field: str, prefix: str, optional: bool):
    kb = InlineKeyboardBuilder()
    if field == "department":
        for i, d in enumerate((await db.departments())[:24]):
            kb.button(text=d, callback_data=f"{prefix}:dep:{i}")
    elif field == "responsible_id":
        for u in await db.users():
            kb.button(text=f"{u['name']} · {ROLE_LABEL[u['role']]}", callback_data=f"{prefix}:resp:{u['id']}")
    elif field == "gift_format":
        for i, g in enumerate(GIFTS):
            kb.button(text=g, callback_data=f"{prefix}:gift:{i}")
    if optional:
        kb.button(text="⏭ Пропустить", callback_data=f"{prefix}:skip")
    kb.adjust(2)
    return kb.as_markup()


# ================= добавление =================
@router.message(F.text == BTN_ADD)
async def add_start(message: Message, me: dict, state: FSMContext, db: DB):
    await state.clear()
    if not is_staff(me):
        return await start_kind(message, state, "partner", me, db)
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Сотрудник", callback_data="add:kind:employee")
    kb.button(text="🤝 Партнёр / клиент", callback_data="add:kind:partner")
    await message.answer("Кого добавляем?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("add:kind:"))
async def add_kind(cb: CallbackQuery, state: FSMContext, me: dict, db: DB):
    kind = cb.data.split(":")[2]
    if kind == "employee" and not is_staff(me):
        return await cb.answer("Нет прав", show_alert=True)
    await cb.message.edit_reply_markup()
    await start_kind(cb.message, state, kind, me, db)
    await cb.answer()


async def start_kind(message: Message, state: FSMContext, kind: str, me: dict, db: DB):
    data = {"kind": kind, "i": 0, "p": {}}
    if kind == "partner" and not is_staff(me):
        data["p"]["responsible_id"] = me["id"]
    await state.set_state(AddSG.step)
    await state.set_data(data)
    await message.answer(f"Новый: {KIND_LABEL[kind].lower()}. Отменить — кнопкой внизу.",
                         reply_markup=cancel_kb())
    await ask_next(message, state, db)


async def ask_next(message: Message, state: FSMContext, db: DB):
    data = await state.get_data()
    steps = STEPS[data["kind"]]
    i = data["i"]
    # пропускаем уже заполненные шаги (например, ответственный у менеджера)
    while i < len(steps) and steps[i] in data["p"]:
        i += 1
    await state.update_data(i=i)
    if i >= len(steps):
        return await show_confirm(message, state, db)
    field = steps[i]
    markup = None
    if field in ("department", "responsible_id", "gift_format") or field in OPTIONAL:
        markup = await choice_kb(db, field, "add", field in OPTIONAL)
    await message.answer(prompt(data["kind"], field), reply_markup=markup)


async def store(state: FSMContext, field: str, value):
    data = await state.get_data()
    data["p"][field] = value
    data["i"] += 1
    await state.set_data(data)


def validate(field: str, text: str) -> dict:
    """Возвращает dict колонок для записи; бросает ValueError с понятным текстом."""
    text = " ".join(text.split())
    if field == "birthday":
        d, m, y = parse_birthday(text)
        return {"birth_day": d, "birth_month": m, "birth_year": y}
    if field == "full_name":
        if len(text) < 2:
            raise ValueError("Слишком коротко")
        return {"full_name": text[:120]}
    if field == "company" and len(text) < 2:
        raise ValueError("Укажите компанию")
    return {field: text[:500]}


@router.message(AddSG.step, F.text, ~F.text.in_(MENU_BUTTONS))
async def add_text(message: Message, state: FSMContext, db: DB):
    data = await state.get_data()
    field = STEPS[data["kind"]][data["i"]]
    if field == "responsible_id":
        return await message.answer("Выберите ответственного кнопкой выше 👆")
    try:
        cols = validate(field, message.text)
    except (DateParseError, ValueError) as e:
        return await message.answer(f"⚠️ {e}. Попробуйте ещё раз.")
    data["p"].update(cols)
    data["p"][field] = True if field == "birthday" else cols.get(field)
    data["i"] += 1
    await state.set_data(data)
    await ask_next(message, state, db)


@router.callback_query(AddSG.step, F.data.startswith("add:"))
async def add_choice(cb: CallbackQuery, state: FSMContext, db: DB):
    data = await state.get_data()
    field = STEPS[data["kind"]][data["i"]]
    parts = cb.data.split(":")
    action = parts[1]
    if action == "skip" and field in OPTIONAL:
        await store(state, field, None)
    elif action == "dep" and field == "department":
        deps = await db.departments()
        await store(state, field, deps[int(parts[2])])
    elif action == "resp" and field == "responsible_id":
        await store(state, field, int(parts[2]))
    elif action == "gift" and field == "gift_format":
        await store(state, field, GIFTS[int(parts[2])])
    else:
        return await cb.answer("Эта кнопка уже не актуальна")
    await cb.message.edit_reply_markup()
    await cb.answer()
    await ask_next(cb.message, state, db)


def draft_person(data: dict) -> dict:
    p = {k: v for k, v in data["p"].items() if k != "birthday"}
    p["kind"] = data["kind"]
    return p


async def show_confirm(message: Message, state: FSMContext, db: DB):
    data = await state.get_data()
    p = draft_person(data)
    lines = [f"<b>Проверьте:</b>", f"{KIND_LABEL[p['kind']]}: <b>{h(p['full_name'])}</b>",
             f"🎂 {fmt_birthday(p['birth_day'], p['birth_month'], p['birth_year'])}"]
    labels = EMPLOYEE_FIELDS if p["kind"] == "employee" else PARTNER_FIELDS
    for k, v in p.items():
        if k in ("kind", "full_name", "birth_day", "birth_month", "birth_year") or not v:
            continue
        if k == "responsible_id":
            u = await db.user(v)
            v = u["name"] if u else v
        lines.append(f"{labels.get(k, k)}: {h(v)}")
    dup = [x for x in await db.persons(active=None)
           if x["full_name"].lower() == p["full_name"].lower()
           and x["birth_day"] == p["birth_day"] and x["birth_month"] == p["birth_month"]]
    if dup:
        lines.append("\n⚠️ Похоже, такой человек уже есть: " + ", ".join(person_title(x) for x in dup))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сохранить", callback_data="add:save")
    kb.button(text="✖️ Отмена", callback_data="add:cancel")
    await state.set_state(AddSG.confirm)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(AddSG.confirm, F.data == "add:save")
async def add_save(cb: CallbackQuery, state: FSMContext, db: DB, me: dict, notifier: Notifier):
    data = await state.get_data()
    p = draft_person(data)
    pid = await db.create_person(p)
    await state.clear()
    await cb.message.edit_reply_markup()
    person = await db.person(pid)
    await cb.message.answer("Сохранено ✅", reply_markup=main_menu(me["role"]))
    await cb.message.answer(person_card(person, notifier.today(), await notifier.feb29()),
                            reply_markup=person_kb(person, me))
    await cb.answer()
    await notifier.refresh_person(pid)


@router.callback_query(AddSG.confirm, F.data == "add:cancel")
async def add_cancel(cb: CallbackQuery, state: FSMContext, me: dict):
    await state.clear()
    await cb.message.edit_reply_markup()
    await cb.message.answer("Отменено.", reply_markup=main_menu(me["role"]))
    await cb.answer()


# ================= ближайшие =================
@router.message(F.text == BTN_UPCOMING)
async def upcoming(message: Message, db: DB, me: dict, notifier: Notifier, state: FSMContext):
    await state.clear()
    today = notifier.today()
    feb29 = await notifier.feb29()
    rows = await db.persons(responsible_id=None if is_staff(me) else me["id"])
    if not is_staff(me):
        rows = [r for r in rows if r["kind"] == "partner"]
    items = []
    for p in rows:
        d = next_occurrence(p["birth_day"], p["birth_month"], today, feb29)
        if (d - today).days <= 30:
            items.append((d, p))
    items.sort(key=lambda x: (x[0], x[1]["full_name"]))
    if not items:
        return await message.answer("В ближайшие 30 дней дней рождения нет.")
    lines = ["<b>Ближайшие 30 дней</b>"]
    kb = InlineKeyboardBuilder()
    for d, p in items:
        icon = "👤" if p["kind"] == "employee" else "🤝"
        today_mark = " 🎉" if d == today else ""
        lines.append(f"{fmt_short(d)} {icon} {h(person_title(p))}{today_mark}")
    for d, p in items[:30]:
        kb.button(text=f"{d:%d.%m} {p['full_name'][:40]}", callback_data=f"p:view:{p['id']}")
    kb.adjust(2)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


# ================= поиск =================
@router.message(F.text == BTN_SEARCH)
async def search_start(message: Message, state: FSMContext, me: dict):
    await state.set_state(SearchSG.query)
    await message.answer("Введите часть ФИО или компании:", reply_markup=cancel_kb())
    kb = InlineKeyboardBuilder()
    if is_staff(me):
        kb.button(text="👤 Все сотрудники", callback_data="ls:employee:0")
        kb.button(text="🤝 Все партнёры", callback_data="ls:partner:0")
        kb.button(text="🗄 Архив", callback_data="ls:archive:0")
    else:
        kb.button(text="🤝 Мои партнёры", callback_data="ls:partner:0")
    kb.adjust(2)
    await message.answer("…или откройте список:", reply_markup=kb.as_markup())


PAGE = 20


@router.callback_query(F.data.startswith("ls:"))
async def list_people(cb: CallbackQuery, db: DB, me: dict, state: FSMContext):
    _, what, page = cb.data.split(":")
    page = int(page)
    mine = None if is_staff(me) else me["id"]
    if what == "archive" and is_staff(me):
        rows = await db.persons(active=False)
        title = "Архив"
    else:
        what = "partner" if what != "employee" or not is_staff(me) else what
        rows = await db.persons(kind=what, responsible_id=mine)
        title = "Сотрудники" if what == "employee" else ("Партнёры" if mine is None else "Мои партнёры")
    await state.clear()
    total = len(rows)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    kb = InlineKeyboardBuilder()
    for p in chunk:
        kb.button(text=person_title(p)[:60], callback_data=f"p:view:{p['id']}")
    nav = []
    if page > 0:
        nav.append(("⬅️", f"ls:{what}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(("➡️", f"ls:{what}:{page + 1}"))
    for t, d in nav:
        kb.button(text=t, callback_data=d)
    kb.adjust(*([1] * len(chunk)), len(nav) or 1)
    pages = max(1, -(-total // PAGE))
    text = f"<b>{title}</b>: {total} · стр. {page + 1}/{pages}" if total else f"<b>{title}</b>: пусто"
    if cb.message.text and cb.message.text.startswith(title):
        await cb.message.edit_text(text, reply_markup=kb.as_markup())
    else:
        await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


async def do_search(message: Message, db: DB, me: dict, q: str):
    hits = await db.search_persons(q, responsible_id=None if is_staff(me) else me["id"])
    if not hits:
        return await message.answer("Никого не нашёл. Попробуйте по-другому или ➕ Добавить.",
                                    reply_markup=main_menu(me["role"]))
    kb = InlineKeyboardBuilder()
    for p in hits:
        mark = "" if p["is_active"] else "🗄 "
        icon = "👤" if p["kind"] == "employee" else "🤝"
        kb.button(text=f"{mark}{icon} {person_title(p)[:55]}", callback_data=f"p:view:{p['id']}")
    kb.adjust(1)
    await message.answer(f"Найдено: {len(hits)}", reply_markup=kb.as_markup())


@router.message(SearchSG.query, F.text, ~F.text.in_(MENU_BUTTONS))
async def search_query(message: Message, state: FSMContext, db: DB, me: dict):
    await state.clear()
    await message.answer("Ищу…", reply_markup=main_menu(me["role"]))
    await do_search(message, db, me, message.text)


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def search_free(message: Message, db: DB, me: dict):
    """Любой текст вне сценариев — это поиск."""
    if len(message.text.strip()) >= 2:
        await do_search(message, db, me, message.text)


# ================= карточка =================
async def load(cb: CallbackQuery, db: DB, me: dict) -> dict | None:
    pid = int(cb.data.split(":")[2])
    p = await db.person(pid)
    if not can_access(me, p):
        await cb.answer("Нет доступа", show_alert=True)
        return None
    return p


async def render(cb: CallbackQuery, p: dict, me: dict, notifier: Notifier, new: bool = False):
    text = person_card(p, notifier.today(), await notifier.feb29())
    if new:
        await cb.message.answer(text, reply_markup=person_kb(p, me))
    else:
        await cb.message.edit_text(text, reply_markup=person_kb(p, me))


@router.callback_query(F.data.startswith("p:view:"))
async def card(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    if p := await load(cb, db, me):
        await render(cb, p, me, notifier, new=True)
        await cb.answer()


@router.callback_query(F.data.startswith("p:pub:"))
async def toggle_public(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    if p := await load(cb, db, me):
        await db.update_person(p["id"], public_congrats=0 if p["public_congrats"] else 1)
        await render(cb, await db.person(p["id"]), me, notifier)
        await cb.answer()


@router.callback_query(F.data.startswith("p:arch:"))
async def toggle_archive(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    if p := await load(cb, db, me):
        await db.update_person(p["id"], is_active=0 if p["is_active"] else 1)
        await render(cb, await db.person(p["id"]), me, notifier)
        await cb.answer("В архиве — напоминаний не будет" if p["is_active"] else "Восстановлено")


@router.callback_query(F.data.startswith("p:hist:"))
async def history(cb: CallbackQuery, db: DB, me: dict):
    if not (p := await load(cb, db, me)):
        return
    evs = await db.person_events(p["id"])
    if not evs:
        return await cb.answer("Истории пока нет", show_alert=True)
    status = {"pending": "ожидает", "draft": "черновик", "approved": "одобрено", "sent": "опубликовано",
              "skipped": "не публиковали", "done": "поздравлен"}
    lines = [f"<b>История: {h(p['full_name'])}</b>"]
    for e in evs:
        line = f"{e['year']}: {status.get(e['status'], e['status'])}"
        if e["method"]:
            line += f" ({h(e['method'])})"
        if e["done_by_name"]:
            line += f" — {h(e['done_by_name'])}"
        lines.append(line)
        if e["draft_text"] and p["kind"] == "employee":
            lines.append(f"<blockquote expandable>{h(e['draft_text'])}</blockquote>")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data.startswith("p:edit:"))
async def edit_menu(cb: CallbackQuery, db: DB, me: dict):
    if p := await load(cb, db, me):
        await show_edit_menu(cb, p, me)


async def show_edit_menu(cb: CallbackQuery, p: dict, me: dict):
    fields = EMPLOYEE_FIELDS if p["kind"] == "employee" else PARTNER_FIELDS
    kb = InlineKeyboardBuilder()
    for key, label in fields.items():
        if key == "responsible_id" and not is_staff(me):
            continue
        kb.button(text=label, callback_data=f"p:f:{p['id']}:{key}")
    kb.button(text=f"За 7 дней: {'🔔' if p['remind_7'] else '🔕'}", callback_data=f"p:r7:{p['id']}")
    kb.button(text=f"За 1 день: {'🔔' if p['remind_1'] else '🔕'}", callback_data=f"p:r1:{p['id']}")
    if me["role"] == "admin":
        kb.button(text="🗑 Удалить навсегда", callback_data=f"p:del:{p['id']}")
    kb.button(text="⬅️ Назад", callback_data=f"p:back:{p['id']}")
    kb.adjust(2)
    await cb.message.edit_reply_markup(reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("p:r7:") | F.data.startswith("p:r1:"))
async def toggle_remind(cb: CallbackQuery, db: DB, me: dict):
    if not (p := await load(cb, db, me)):
        return
    col = "remind_7" if cb.data.startswith("p:r7:") else "remind_1"
    await db.update_person(p["id"], **{col: 0 if p[col] else 1})
    await show_edit_menu(cb, await db.person(p["id"]), me)


@router.callback_query(F.data.startswith("p:del:"))
async def delete_ask(cb: CallbackQuery, db: DB, me: dict):
    if not (p := await load(cb, db, me)) or me["role"] != "admin":
        return await cb.answer("Только администратор", show_alert=True) if p else None
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"p:delok:{p['id']}")
    kb.button(text="⬅️ Нет", callback_data=f"p:back:{p['id']}")
    kb.adjust(2)
    await cb.message.edit_text(
        f"Удалить <b>{h(p['full_name'])}</b> навсегда вместе с историей поздравлений?\n"
        "Если человек просто уволился или вы больше не работаете — лучше «🗄 В архив».",
        reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("p:delok:"))
async def delete_ok(cb: CallbackQuery, db: DB, me: dict):
    if not (p := await load(cb, db, me)) or me["role"] != "admin":
        return
    await db.execute("DELETE FROM events WHERE person_id=?", (p["id"],))
    await db.execute("DELETE FROM persons WHERE id=?", (p["id"],))
    await cb.message.edit_text(f"🗑 {h(p['full_name'])} удалён(а).")
    await cb.answer()


@router.callback_query(F.data.startswith("p:back:"))
async def edit_back(cb: CallbackQuery, db: DB, me: dict, notifier: Notifier):
    if p := await load(cb, db, me):
        await render(cb, p, me, notifier)
        await cb.answer()


@router.callback_query(F.data.startswith("p:f:"))
async def edit_field(cb: CallbackQuery, db: DB, me: dict, state: FSMContext):
    if not (p := await load(cb, db, me)):
        return
    field = cb.data.split(":")[3]
    allowed = EMPLOYEE_FIELDS if p["kind"] == "employee" else PARTNER_FIELDS
    if field not in allowed or (field == "responsible_id" and not is_staff(me)):
        return await cb.answer("Нельзя", show_alert=True)
    await state.set_state(EditSG.value)
    await state.update_data(pid=p["id"], field=field)
    current = (fmt_birthday(p["birth_day"], p["birth_month"], p["birth_year"])
               if field == "birthday" else p.get("responsible_name") if field == "responsible_id"
               else p.get(field))
    kb = None
    if field in ("department", "responsible_id", "gift_format"):
        kb = await choice_kb(db, field, f"e:{p['id']}", False)
    hint = f"\nСейчас: <i>{h(current)}</i>" if current else ""
    clear = "\nЧтобы очистить поле, отправьте «-»." if field not in ("full_name", "birthday", "responsible_id") else ""
    await cb.message.answer(f"{allowed[field]}: {prompt(p['kind'], field)}{hint}{clear}",
                            reply_markup=kb)
    await cb.message.answer("Жду значение…", reply_markup=cancel_kb())
    await cb.answer()


async def finish_edit(message: Message, state: FSMContext, db: DB, me: dict, notifier: Notifier,
                      pid: int, cols: dict):
    await db.update_person(pid, **cols)
    await state.clear()
    p = await db.person(pid)
    await message.answer("Сохранено ✅", reply_markup=main_menu(me["role"]))
    await message.answer(person_card(p, notifier.today(), await notifier.feb29()),
                         reply_markup=person_kb(p, me))
    await notifier.refresh_person(pid)


@router.message(EditSG.value, F.text, ~F.text.in_(MENU_BUTTONS))
async def edit_value(message: Message, state: FSMContext, db: DB, me: dict, notifier: Notifier):
    data = await state.get_data()
    field, pid = data["field"], data["pid"]
    if field == "responsible_id":
        return await message.answer("Выберите кнопкой выше 👆")
    if message.text.strip() == "-" and field not in ("full_name", "birthday", "company"):
        cols = {field: None}
    else:
        try:
            cols = validate(field, message.text)
        except (DateParseError, ValueError) as e:
            return await message.answer(f"⚠️ {e}. Попробуйте ещё раз.")
    await finish_edit(message, state, db, me, notifier, pid, cols)


@router.callback_query(EditSG.value, F.data.startswith("e:"))
async def edit_choice(cb: CallbackQuery, state: FSMContext, db: DB, me: dict, notifier: Notifier):
    data = await state.get_data()
    _, pid, action, val = cb.data.split(":")
    if int(pid) != data["pid"]:
        return await cb.answer("Кнопка устарела")
    if action == "dep":
        cols = {"department": (await db.departments())[int(val)]}
    elif action == "resp" and is_staff(me):
        cols = {"responsible_id": int(val)}
    elif action == "gift":
        cols = {"gift_format": GIFTS[int(val)]}
    else:
        return await cb.answer("Нельзя")
    await cb.message.edit_reply_markup()
    await cb.answer()
    await finish_edit(cb.message, state, db, me, notifier, int(pid), cols)
