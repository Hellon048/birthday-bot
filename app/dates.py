"""Разбор дат и расчёт ближайшего дня рождения."""
from __future__ import annotations

import calendar
import re
from datetime import date

MONTHS_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_MONTH_STEMS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class DateParseError(ValueError):
    pass


def _check(day: int, month: int, year: int | None) -> tuple[int, int, int | None]:
    if not 1 <= month <= 12:
        raise DateParseError("Месяц должен быть от 1 до 12")
    # 29 февраля без года допустимо, поэтому проверяем по високосному 2000
    max_day = calendar.monthrange(year or 2000, month)[1]
    if not 1 <= day <= max_day:
        raise DateParseError("В этом месяце нет такого числа")
    if year is not None:
        if year < 100:
            year += 1900 if year > 30 else 2000
        if not 1920 <= year <= date.today().year:
            raise DateParseError("Странный год рождения")
    return day, month, year


def parse_birthday(text: str) -> tuple[int, int, int | None]:
    """'14.01', '14.01.1991', '14/01/91', '14 января', '14 января 1991', '1991-01-14'."""
    s = re.sub(r"\s*(г|год|года)\.?$", "", text.strip().lower()).strip()
    s = re.sub(r"\s+", " ", s)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _check(int(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})[.\-/ ](\d{1,2})(?:[.\-/ ](\d{2,4}))?", s)
    if m:
        return _check(int(m[1]), int(m[2]), int(m[3]) if m[3] else None)
    m = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\.?(?:\s+(\d{4}))?", s)
    if m:
        stem = m[2][:3]
        if stem not in _MONTH_STEMS:
            raise DateParseError("Не понял месяц")
        return _check(int(m[1]), _MONTH_STEMS[stem], int(m[3]) if m[3] else None)
    raise DateParseError("Не понял дату. Примеры: 14.01, 14.01.1991, 14 января")


def celebrate_date(day: int, month: int, year: int, feb29_rule: str = "28.02") -> date:
    """Дата празднования в конкретном году (с правилом для 29 февраля)."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 3, 1) if feb29_rule == "01.03" else date(year, 2, 28)
    return date(year, month, day)


def next_occurrence(day: int, month: int, today: date, feb29_rule: str = "28.02") -> date:
    d = celebrate_date(day, month, today.year, feb29_rule)
    if d < today:
        d = celebrate_date(day, month, today.year + 1, feb29_rule)
    return d


def fmt_birthday(day: int, month: int, year: int | None) -> str:
    base = f"{day} {MONTHS_GEN[month]}"
    return f"{base} {year}" if year else base


def fmt_short(d: date) -> str:
    return f"{d:%d.%m} ({WEEKDAYS[d.weekday()]})"


def age_on(year: int | None, d: date) -> int | None:
    return d.year - year if year else None


def when_label(delta: int) -> str:
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Завтра"
    if delta in (2, 3, 4):
        return f"Через {delta} дня"
    return f"Через {delta} дней"
