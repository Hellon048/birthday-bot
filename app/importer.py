"""Импорт старой базы (birthday_calendar.db) со слиянием и резервные копии.

Слияние, а не замена:
  * человек ищется по старому id (legacy_id), а если его добавили в боте вручную —
    по ФИО + дню и месяцу рождения;
  * существующая запись обновляется, только если в файле она изменена ПОЗЖЕ,
    чем в боте (сравнение updated_at) — устаревший файл не затрёт правки из бота;
  * поля, которых в старой базе нет (характеристика, публичность, привязка Telegram,
    история поздравлений), не трогаются никогда;
  * то, что есть в боте, но пропало из файла, не удаляется — только попадает в отчёт.
"""
from __future__ import annotations

import difflib
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from .db import DEFAULT_SETTINGS, SCHEMA

GIFT_FIX = {"ерч": "мерч"}
EMP_COLS = ["full_name", "birth_day", "birth_month", "birth_year", "position", "department",
            "phone", "email", "gift_format", "comment", "remind_7", "remind_1", "is_active"]
PAR_COLS = ["full_name", "birth_day", "birth_month", "birth_year", "company", "contact_type",
            "category", "phone", "email", "messenger", "responsible_id", "gift_format",
            "congrat_method", "congratulator", "comment", "remind_7", "remind_1", "is_active"]
LABELS = {"full_name": "ФИО", "birth_day": "дата", "birth_month": "дата", "birth_year": "год",
          "position": "должность", "department": "отдел", "company": "компания",
          "responsible_id": "ответственный", "is_active": "активность", "gift_format": "формат",
          "phone": "телефон", "comment": "комментарий", "category": "категория"}


class NotLegacyDB(Exception):
    pass


@dataclass
class Report:
    added: dict = field(default_factory=lambda: {"employee": 0, "partner": 0})
    updated: list = field(default_factory=list)       # (ФИО, [поля])
    unchanged: int = 0
    skipped_older: int = 0                             # в боте правили позже, чем в файле
    missing: list = field(default_factory=list)       # есть в боте, нет в файле
    new_users: list = field(default_factory=list)
    merged_users: dict = field(default_factory=dict)  # каноническое имя -> варианты
    notes: list = field(default_factory=list)

    def text(self, html: bool = False) -> str:
        b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
        lines = [b("Итог импорта"),
                 f"Новых сотрудников: {self.added['employee']}, партнёров: {self.added['partner']}",
                 f"Обновлено: {len(self.updated)} · без изменений: {self.unchanged}"]
        if self.skipped_older:
            lines.append(f"Пропущено (в боте изменены позже, чем в файле): {self.skipped_older}")
        for name, cols in self.updated[:15]:
            lines.append(f"  • {name}: {', '.join(sorted(set(LABELS.get(c, c) for c in cols)))}")
        if len(self.updated) > 15:
            lines.append(f"  … и ещё {len(self.updated) - 15}")
        if self.new_users:
            lines.append("Новые ответственные (нужно выслать приглашения): " + ", ".join(self.new_users))
        for canon, variants in self.merged_users.items():
            lines.append(f"Ответственный «{canon}» склеен из: {', '.join(sorted(variants))}")
        if self.missing:
            lines.append(f"Есть в боте, но нет в файле ({len(self.missing)}) — оставлены как есть: "
                         + ", ".join(self.missing[:10]) + (" …" if len(self.missing) > 10 else ""))
        lines += self.notes
        return "\n".join(lines)


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def norm_name(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in (s or "").split())


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def canon_map(values: list[str], preferred: list[str] = ()) -> dict[str, str]:
    """Сводит варианты написания (регистр, пробелы, опечатки) к одному.
    Написание из `preferred` (то, что уже есть в боте) побеждает."""
    groups: dict[str, Counter] = defaultdict(Counter)
    for v in values:
        if squash(v):
            groups[squash(v).lower()][squash(v)] += 1
    for v in preferred:
        if squash(v):
            groups[squash(v).lower()][squash(v)] += 10_000
    keys = sorted(groups, key=lambda k: -sum(groups[k].values()))
    root: dict[str, str] = {}
    canon: list[str] = []
    for k in keys:
        m = difflib.get_close_matches(k, canon, n=1, cutoff=0.87)
        root[k] = m[0] if m else k
        if not m:
            canon.append(k)
    merged: dict[str, Counter] = defaultdict(Counter)
    for k, cnt in groups.items():
        merged[root[k]].update(cnt)
    out = {}
    for k in groups:
        best = merged[root[k]].most_common(1)[0][0]
        out[k] = best[:1].upper() + best[1:]
    return out


def split_date(s: str) -> tuple[int, int, int]:
    y, m, d = s.strip()[:10].split("-")
    return int(d), int(m), int(y)


def gift(v: str | None) -> str | None:
    v = squash(v).lower()
    return GIFT_FIX.get(v, v) or None


def detect(path: str) -> str:
    """'legacy' — старая база, 'bot' — резервная копия бота."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    except sqlite3.DatabaseError:
        raise NotLegacyDB("Это не файл SQLite")
    if {"employees", "contacts"} <= tables:
        return "legacy"
    if {"persons", "users", "events"} <= tables:
        return "bot"
    raise NotLegacyDB("Не узнаю структуру базы: нет таблиц employees/contacts")


def merge_legacy(src: str, dst: str, apply: bool) -> Report:
    if detect(src) != "legacy":
        raise NotLegacyDB("Это не старая база дней рождения")
    rep = Report()
    old = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    new = sqlite3.connect(dst, timeout=30)
    new.row_factory = sqlite3.Row
    new.executescript(SCHEMA)
    try:
        new.execute("BEGIN")
        today = date.today()
        emps = [dict(r) for r in old.execute("SELECT * FROM employees")]
        conts = [dict(r) for r in old.execute("SELECT * FROM contacts")]

        # настройки — только если в боте их ещё нет
        old_settings = {r["key"]: r["value"] for r in old.execute("SELECT * FROM settings")}
        settings = dict(DEFAULT_SETTINGS)
        if old_settings.get("company_name"):
            settings["company_name"] = old_settings["company_name"]
        rule = old_settings.get("feb29_rule") or ""
        if "1 марта" in rule or "01.03" in rule:
            settings["feb29_rule"] = "01.03"
        for k, v in settings.items():
            new.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

        # ---- ответственные: сопоставляем с пользователями бота или создаём заготовки
        users = [dict(r) for r in new.execute("SELECT * FROM users")]
        known = {norm_name(u["name"]): u["id"] for u in users}
        resp_ids: dict[str, int | None] = {}
        variants: dict[str, set] = defaultdict(set)
        raw_active = Counter(norm_name(c["responsible"]) for c in conts
                             if c["is_active"] and squash(c["responsible"]))
        for raw in sorted({norm_name(c["responsible"]) for c in conts if squash(c["responsible"])},
                          key=lambda n: -raw_active.get(n, 0)):
            match = difflib.get_close_matches(raw, list(known), n=1, cutoff=0.85)
            if match:
                resp_ids[raw] = known[match[0]]
                variants[match[0]].add(raw)
            elif raw_active.get(raw):  # заготовки — только для тех, у кого есть активные партнёры
                cur = new.execute("INSERT INTO users(name, role, created_at) VALUES (?, 'manager', ?)",
                                  (raw, now()))
                known[raw] = cur.lastrowid
                resp_ids[raw] = cur.lastrowid
                variants[raw].add(raw)
                rep.new_users.append(raw)
            else:
                resp_ids[raw] = None
        for canon, vs in variants.items():
            raw_variants = {squash(c["responsible"]) for c in conts
                            if norm_name(c["responsible"]) in vs}
            if len(raw_variants) > 1:
                rep.merged_users[canon] = raw_variants

        # ---- отделы: написание из бота в приоритете
        existing_deps = [r[0] for r in new.execute(
            "SELECT DISTINCT department FROM persons WHERE department IS NOT NULL")]
        dep_map = canon_map([e["department"] for e in emps], existing_deps)

        # ---- что уже есть в боте
        persons = [dict(r) for r in new.execute("SELECT * FROM persons")]
        by_legacy = {(p["legacy_table"], p["legacy_id"]): p for p in persons if p["legacy_id"]}
        by_name = defaultdict(list)
        for p in persons:
            if not p["legacy_id"]:
                by_name[(p["kind"], p["full_name"].lower(), p["birth_day"], p["birth_month"])].append(p)

        def upsert(kind: str, table: str, row: dict, data: dict):
            cols = EMP_COLS if kind == "employee" else PAR_COLS
            cur = by_legacy.get((table, row["id"]))
            if not cur:
                cands = by_name.get((kind, data["full_name"].lower(), data["birth_day"], data["birth_month"]))
                if cands:
                    cur = cands.pop(0)
                    new.execute("UPDATE persons SET legacy_table=?, legacy_id=? WHERE id=?",
                                (table, row["id"], cur["id"]))
            if not cur:
                vals = {**{c: data.get(c) for c in cols}, "kind": kind, "legacy_table": table,
                        "legacy_id": row["id"], "created_at": row["created_at"] or now(),
                        "updated_at": row["updated_at"] or now()}
                new.execute(f"INSERT INTO persons({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                            list(vals.values()))
                rep.added[kind] += 1
                return
            changed = {c: data[c] for c in cols
                       if data.get(c) is not None and data[c] != cur[c]}
            if not changed:
                rep.unchanged += 1
                return
            if (row["updated_at"] or "") <= (cur["updated_at"] or ""):
                rep.skipped_older += 1
                return
            changed["updated_at"] = row["updated_at"]
            new.execute(f"UPDATE persons SET {', '.join(f'{c}=?' for c in changed)} WHERE id=?",
                        (*changed.values(), cur["id"]))
            rep.updated.append((data["full_name"], [c for c in changed if c != "updated_at"]))

        # ---- сотрудники
        no_dep = []
        for e in emps:
            d, m, y = split_date(e["birthday"])
            dep = dep_map.get(squash(e["department"]).lower()) or None
            if not dep and e["is_active"]:
                no_dep.append(squash(e["full_name"]))
            upsert("employee", "employees", e, {
                "full_name": squash(e["full_name"]), "birth_day": d, "birth_month": m, "birth_year": y,
                "position": squash(e["position"]) or None, "department": dep,
                "phone": squash(e["phone"]) or None, "email": squash(e["email"]) or None,
                "gift_format": gift(e["congratulation_format"]), "comment": squash(e["comment"]) or None,
                "remind_7": e["remind_7_days"], "remind_1": e["remind_1_day"], "is_active": e["is_active"]})
        if no_dep:
            rep.notes.append(f"Сотрудники без отдела ({len(no_dep)}): " + ", ".join(no_dep))

        # ---- партнёры
        dropped = 0
        for c in conts:
            d, m, y = split_date(c["birthday"])
            if y >= today.year - 14:   # заглушка вместо неизвестного года
                y, dropped = None, dropped + 1
            upsert("partner", "contacts", c, {
                "full_name": squash(c["full_name"]), "birth_day": d, "birth_month": m, "birth_year": y,
                "company": squash(c["company"]) or None, "contact_type": squash(c["contact_type"]) or None,
                "category": squash(c["category"]) or None, "phone": squash(c["phone"]) or None,
                "email": squash(c["email"]) or None, "messenger": squash(c["messenger"]) or None,
                "responsible_id": resp_ids.get(norm_name(c["responsible"])),
                "gift_format": gift(c["congratulation_format"]),
                "congrat_method": squash(c["congratulation_method"]).lower() or None,
                "congratulator": squash(c["congratulated_by"]) or None,
                "comment": squash(c["comment"]) or None,
                "remind_7": c["remind_7_days"], "remind_1": c["remind_1_day"], "is_active": c["is_active"]})
        if dropped:
            rep.notes.append(f"Год рождения-заглушка у {dropped} партнёров → «год неизвестен».")

        # ---- пропавшие из файла
        src_keys = {("employees", e["id"]) for e in emps} | {("contacts", c["id"]) for c in conts}
        rep.missing = [p["full_name"] for k, p in by_legacy.items() if k not in src_keys and p["is_active"]]

        # ---- подозрительные дубли
        same = Counter(squash(c["full_name"]).lower() for c in conts if c["is_active"])
        risky = [n for n, k in same.items() if k > 1 and len(re.sub(r"\(.*?\)", "", n).split()) == 1]
        if risky:
            rep.notes.append("Партнёры записаны только именем и повторяются: "
                             + ", ".join(f"{n.title()} ×{same[n]}" for n in risky))

        if apply:
            new.commit()
        else:
            new.rollback()
    except Exception:
        new.rollback()
        raise
    finally:
        new.close()
        old.close()
    return rep


def make_backup(db_path: str, backup_dir: str, tag: str = "auto", keep: int = 14) -> str:
    """Консистентная копия работающей базы (sqlite backup API, учитывает WAL)."""
    os.makedirs(backup_dir, exist_ok=True)
    out = os.path.join(backup_dir, f"birthdays_{datetime.now():%Y%m%d_%H%M%S}_{tag}.db")
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    files = sorted(f for f in os.listdir(backup_dir) if f.startswith("birthdays_") and f.endswith(f"_{tag}.db"))
    for f in files[:-keep]:
        os.remove(os.path.join(backup_dir, f))
    return out


def restore_backup(backup_path: str, db_path: str) -> None:
    """Заменяет содержимое рабочей базы содержимым копии (на лету, без перезапуска)."""
    if detect(backup_path) != "bot":
        raise NotLegacyDB("Это не резервная копия бота")
    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
