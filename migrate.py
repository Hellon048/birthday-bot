"""Перенос старой базы birthday_calendar.db в новую схему бота с чисткой данных.

    python migrate.py старый.db data/birthdays.db [--force]

Что делает:
  * employees + contacts -> persons (kind = employee / partner), старые id сохраняются в legacy_id
  * текстовые «ответственные» склеиваются по опечаткам/регистру и превращаются в
    пользователей-заготовки (role=manager, без Telegram). Админ потом шлёт каждому
    ссылку-приглашение — и все его партнёры автоматически привязываются.
  * год-заглушка у партнёров (текущий год и «младенцы» < 14 лет) -> NULL
  * отделы и форматы поздравления приводятся к одному написанию
  * печатает отчёт, что исправлено и что надо проверить руками
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import DEFAULT_SETTINGS, SCHEMA  # noqa: E402

GIFT_FIX = {"ерч": "мерч"}


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def norm_name(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in (s or "").split())


def canon_map(values: list[str]) -> dict[str, str]:
    """Сводит варианты написания (регистр/пробелы) к самому частому, с заглавной буквы."""
    groups: dict[str, Counter] = defaultdict(Counter)
    for v in values:
        v2 = " ".join((v or "").split())
        if v2:
            groups[v2.lower()][v2] += 1
    # склеиваем ещё и опечатки («учесток фасовки» -> «участок фасовки»)
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


def cluster_people(names: list[str]) -> dict[str, str]:
    """'иванов Иван', 'Иваонв Иван' -> 'Иванов Иван'."""
    counts = Counter(norm_name(n) for n in names if (n or "").strip())
    canon: list[str] = []
    mapping: dict[str, str] = {}
    for name, _ in counts.most_common():
        match = difflib.get_close_matches(name, canon, n=1, cutoff=0.85)
        target = match[0] if match else name
        if not match:
            canon.append(name)
        mapping[name] = target
    return mapping


def split_date(s: str) -> tuple[int, int, int]:
    y, m, d = s.strip()[:10].split("-")
    return int(d), int(m), int(y)


def migrate(src: str, dst: str, force: bool) -> None:
    if os.path.exists(dst):
        if not force:
            sys.exit(f"{dst} уже существует. Добавьте --force, чтобы перезаписать.")
        os.remove(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    old = sqlite3.connect(src)
    old.row_factory = sqlite3.Row
    new = sqlite3.connect(dst)
    new.executescript(SCHEMA)
    report: list[str] = []
    today = date.today()

    emps = [dict(r) for r in old.execute("SELECT * FROM employees")]
    conts = [dict(r) for r in old.execute("SELECT * FROM contacts")]

    # --- настройки ---
    old_settings = {r["key"]: r["value"] for r in old.execute("SELECT * FROM settings")}
    settings = dict(DEFAULT_SETTINGS)
    if old_settings.get("company_name"):
        settings["company_name"] = old_settings["company_name"]
    if "1 марта" in (old_settings.get("feb29_rule") or "") or "01.03" in (old_settings.get("feb29_rule") or ""):
        settings["feb29_rule"] = "01.03"
    for k, v in settings.items():
        new.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, v))

    # --- ответственные -> пользователи-заготовки ---
    # заготовки создаём только для ответственных по активным партнёрам
    resp_raw = [c["responsible"] for c in conts if c["is_active"] and (c["responsible"] or "").strip()]
    resp_map = cluster_people(resp_raw)
    user_ids: dict[str, int] = {}
    for canon_name in sorted(set(resp_map.values())):
        cur = new.execute(
            "INSERT INTO users(name, role, created_at) VALUES (?, 'manager', ?)", (canon_name, now()))
        user_ids[canon_name] = cur.lastrowid
    merged = defaultdict(set)
    for raw in resp_raw:
        merged[resp_map[norm_name(raw)]].add(raw.strip())
    report.append("Ответственные (созданы как менеджеры без Telegram, нужно выслать приглашения):")
    for name, variants in sorted(merged.items()):
        cnt = sum(1 for r in resp_raw if resp_map[norm_name(r)] == name)
        extra = f"  ← склеено из: {', '.join(sorted(variants))}" if len(variants) > 1 else ""
        report.append(f"  • {name}: {cnt} партнёров{extra}")

    # --- нормализация справочников ---
    dep_map = canon_map([e["department"] for e in emps])
    fixed_deps = sorted({e["department"] for e in emps
                         if e["department"] and dep_map.get(" ".join(e["department"].split()).lower()) != e["department"]})
    if fixed_deps:
        report.append("Отделы приведены к одному написанию: " + ", ".join(
            f"«{d}» → «{dep_map[' '.join(d.split()).lower()]}»" for d in fixed_deps))

    def gift(v: str | None) -> str | None:
        v = " ".join((v or "").split()).lower()
        return GIFT_FIX.get(v, v) or None

    # --- сотрудники ---
    no_dep = []
    for e in emps:
        d, m, y = split_date(e["birthday"])
        dep = dep_map.get(" ".join((e["department"] or "").split()).lower()) or None
        if not dep and e["is_active"]:
            no_dep.append(e["full_name"])
        new.execute(
            "INSERT INTO persons(kind, full_name, birth_day, birth_month, birth_year, position, "
            "department, phone, email, gift_format, comment, remind_7, remind_1, is_active, "
            "legacy_table, legacy_id, created_at, updated_at) "
            "VALUES ('employee',?,?,?,?,?,?,?,?,?,?,?,?,?, 'employees', ?, ?, ?)",
            (" ".join(e["full_name"].split()), d, m, y, e["position"] or None, dep,
             e["phone"] or None, e["email"] or None, gift(e["congratulation_format"]),
             e["comment"] or None, e["remind_7_days"], e["remind_1_day"], e["is_active"],
             e["id"], e["created_at"], e["updated_at"]))
    if no_dep:
        report.append(f"Сотрудники без отдела ({len(no_dep)}): " + ", ".join(no_dep))

    # --- партнёры ---
    dropped_years = []
    for c in conts:
        d, m, y = split_date(c["birthday"])
        if y >= today.year - 14:
            dropped_years.append(y)
            y = None
        resp = norm_name(c["responsible"] or "")
        rid = user_ids.get(resp_map.get(resp, ""))
        new.execute(
            "INSERT INTO persons(kind, full_name, birth_day, birth_month, birth_year, company, "
            "contact_type, category, phone, email, messenger, responsible_id, gift_format, "
            "congrat_method, congratulator, comment, remind_7, remind_1, is_active, "
            "legacy_table, legacy_id, created_at, updated_at) "
            "VALUES ('partner',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'contacts', ?, ?, ?)",
            (" ".join(c["full_name"].split()), d, m, y, c["company"] or None,
             c["contact_type"] or None, c["category"] or None, c["phone"] or None,
             c["email"] or None, c["messenger"] or None, rid, gift(c["congratulation_format"]),
             (c["congratulation_method"] or "").strip().lower() or None,
             c["congratulated_by"] or None, c["comment"] or None,
             c["remind_7_days"], c["remind_1_day"], c["is_active"], c["id"],
             c["created_at"], c["updated_at"]))
    if dropped_years:
        report.append(f"У {len(dropped_years)} партнёров год рождения был заглушкой "
                      f"({', '.join(map(str, sorted(set(dropped_years))))}) → «год неизвестен».")

    # --- журнал поздравлений (если был) ---
    logs = [dict(r) for r in old.execute("SELECT * FROM congratulation_logs")]
    if logs:
        report.append(f"Журнал поздравлений: {len(logs)} записей — перенос не реализован, проверьте.")

    # --- что проверить руками ---
    dup = Counter((" ".join(c["full_name"].split()).lower(), c["company"]) for c in conts if c["is_active"])
    same_name = Counter(" ".join(c["full_name"].split()).lower() for c in conts if c["is_active"])
    risky = [n for n, k in same_name.items()
             if k > 1 and len(re.sub(r"\(.*?\)", "", n).split()) == 1]
    if risky:
        report.append("Партнёры записаны только именем и повторяются — проверьте, что это разные люди: "
                      + ", ".join(f"{n.title()} ×{same_name[n]}" for n in risky))
    exact = [f"{n.title()} ({co})" for (n, co), k in dup.items() if k > 1]
    if exact:
        report.append("Точные дубли (имя + компания): " + ", ".join(exact))
    no_resp = [c["full_name"] for c in conts if c["is_active"] and not (c["responsible"] or "").strip()]
    if no_resp:
        report.append(f"Партнёры без ответственного ({len(no_resp)}): напоминания уйдут админам.")

    new.commit()
    a = new.execute("SELECT kind, is_active, COUNT(*) FROM persons GROUP BY 1, 2").fetchall()
    print(f"Готово: {dst}")
    print("Перенесено:", ", ".join(f"{k}{'' if act else ' (архив)'}: {n}" for k, act, n in a))
    print()
    print("\n".join(report))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    migrate(a.src, a.dst, a.force)
