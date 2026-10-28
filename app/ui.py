"""Общие тексты, клавиатуры и проверки прав."""
from __future__ import annotations

from datetime import date
from html import escape

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .dates import fmt_birthday, fmt_short, next_occurrence, when_label

ROLE_LABEL = {"admin": "Администратор", "hr": "HR", "manager": "Менеджер"}
KIND_LABEL = {"employee": "Сотрудник", "partner": "Партнёр"}

BTN_UPCOMING = "🎂 Ближайшие"
BTN_ADD = "➕ Добавить"
BTN_SEARCH = "🔍 Поиск"
BTN_USERS = "👥 Доступы"
BTN_GROUPS = "💬 Группы"
BTN_SETTINGS = "⚙️ Настройки"
BTN_EXPORT = "📤 Экспорт"
BTN_CANCEL = "✖️ Отмена"
MENU_BUTTONS = {BTN_UPCOMING, BTN_ADD, BTN_SEARCH, BTN_USERS, BTN_GROUPS, BTN_SETTINGS,
                BTN_EXPORT, BTN_CANCEL}

METHODS = {"msg": "сообщение", "call": "звонок", "gift": "подарок", "merch": "мерч",
           "meet": "лично"}


def h(v) -> str:
    return escape(str(v)) if v not in (None, "") else ""


def main_menu(role: str) -> ReplyKeyboardMarkup:
    rows = [[BTN_UPCOMING, BTN_ADD]]
    if role == "admin":
        rows += [[BTN_SEARCH, BTN_USERS], [BTN_GROUPS, BTN_SETTINGS], [BTN_EXPORT]]
    elif role == "hr":
        rows += [[BTN_SEARCH, BTN_GROUPS], [BTN_EXPORT]]
    else:
        rows += [[BTN_SEARCH]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def is_staff(me: dict | None) -> bool:
    return bool(me) and me["role"] in ("admin", "hr")


def can_access(me: dict | None, p: dict | None) -> bool:
    if not me or not p:
        return False
    if is_staff(me):
        return True
    return p["kind"] == "partner" and p["responsible_id"] == me["id"]


def person_title(p: dict) -> str:
    extra = p.get("company") if p["kind"] == "partner" else p.get("department")
    return f"{p['full_name']}" + (f" · {extra}" if extra else "")


def person_card(p: dict, today: date, feb29: str) -> str:
    d = next_occurrence(p["birth_day"], p["birth_month"], today, feb29)
    delta = (d - today).days
    lines = [
        f"<b>{h(p['full_name'])}</b>" + ("  <i>(в архиве)</i>" if not p["is_active"] else ""),
        f"{KIND_LABEL[p['kind']]}",
        f"🎂 {fmt_birthday(p['birth_day'], p['birth_month'], p['birth_year'])}"
        + ("" if p["birth_year"] else " (год неизвестен)")
        + f" — {when_label(delta).lower()}" + (f", {fmt_short(d)}" if delta else ""),
    ]
    if p["kind"] == "employee":
        for label, key in (("Должность", "position"), ("Отдел", "department"),
                           ("Формат", "gift_format"), ("Телефон", "phone")):
            if p.get(key):
                lines.append(f"{label}: {h(p[key])}")
        lines.append(f"Характеристика: {h(p['characteristic']) or '— не заполнена'}")
        lines.append("Публичное поздравление: " + ("включено" if p["public_congrats"] else "выключено"))
    else:
        for label, key in (("Компания", "company"), ("Категория", "category"),
                           ("Дарим", "gift_format"), ("Как поздравляем", "congrat_method"),
                           ("Кто поздравляет", "congratulator"), ("Телефон", "phone"),
                           ("Мессенджер", "messenger")):
            if p.get(key):
                lines.append(f"{label}: {h(p[key])}")
        resp = p.get("responsible_name")
        status = "" if not resp else ("" if p.get("responsible_tg") else " (не подключён к боту)")
        lines.append(f"Ответственный: {h(resp) or '— не назначен'}{status}")
    if p.get("comment"):
        lines.append(f"Комментарий: {h(p['comment'])}")
    return "\n".join(lines)


def person_kb(p: dict, me: dict):
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить", callback_data=f"p:edit:{p['id']}")
    if p["kind"] == "employee":
        kb.button(text="✍️ Характеристика", callback_data=f"p:f:{p['id']}:characteristic")
        kb.button(text="🔕 Не поздравлять публично" if p["public_congrats"] else "🔔 Поздравлять публично",
                  callback_data=f"p:pub:{p['id']}")
    kb.button(text="🗓 История", callback_data=f"p:hist:{p['id']}")
    kb.button(text="🗄 В архив" if p["is_active"] else "♻️ Восстановить",
              callback_data=f"p:arch:{p['id']}")
    kb.adjust(2)
    return kb.as_markup()
