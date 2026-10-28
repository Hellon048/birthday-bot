"""SQLite-хранилище (aiosqlite, без ORM)."""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER UNIQUE,              -- NULL пока человек не перешёл по приглашению
    name        TEXT NOT NULL,               -- как его зовут в базе («Иванов Иван»)
    username    TEXT,
    role        TEXT NOT NULL CHECK (role IN ('admin','hr','manager')),
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by  INTEGER REFERENCES users(id),
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    used_by_tg  INTEGER
);

CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('employee','partner')),
    full_name       TEXT NOT NULL,
    birth_day       INTEGER NOT NULL,
    birth_month     INTEGER NOT NULL,
    birth_year      INTEGER,                 -- NULL = год неизвестен
    position        TEXT,
    department      TEXT,
    company         TEXT,
    contact_type    TEXT,                    -- клиент / партнер
    category        TEXT,                    -- ключевой / постоянный
    phone           TEXT,
    email           TEXT,
    messenger       TEXT,
    responsible_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    gift_format     TEXT,                    -- премия / мерч / подарок ...
    congrat_method  TEXT,                    -- как планируется поздравить (сообщение, открытка...)
    congratulator   TEXT,                    -- кто поздравляет (ОП, коммерческий директор...)
    characteristic  TEXT,                    -- краткая характеристика от HR для генерации
    comment         TEXT,
    public_congrats INTEGER NOT NULL DEFAULT 1,  -- поздравлять в общей группе
    remind_7        INTEGER NOT NULL DEFAULT 1,
    remind_1        INTEGER NOT NULL DEFAULT 1,
    is_active       INTEGER NOT NULL DEFAULT 1,
    legacy_table    TEXT,
    legacy_id       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persons_bd ON persons(birth_month, birth_day);

CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    departments TEXT NOT NULL DEFAULT '',     -- '|'-список отделов, пусто = все
    is_active   INTEGER NOT NULL DEFAULT 1,
    added_by    INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);

-- Одна строка на человека на каждый год празднования
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    year          INTEGER NOT NULL,
    celebrate_on  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
        -- сотрудник: pending -> draft -> approved | skipped -> sent
        -- партнёр:   pending -> done
    draft_text    TEXT,
    method        TEXT,
    done_by       INTEGER REFERENCES users(id),
    done_at       TEXT,
    r7            INTEGER NOT NULL DEFAULT 0,
    r1            INTEGER NOT NULL DEFAULT 0,
    r0            INTEGER NOT NULL DEFAULT 0,
    hr_notified   INTEGER NOT NULL DEFAULT 0,
    escalated     INTEGER NOT NULL DEFAULT 0,
    posted_at     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (person_id, year)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "company_name": 'ООО "Русская табачная компания"',
    "feb29_rule": "28.02",
}

# поля, которые можно редактировать из бота
EMPLOYEE_FIELDS = {
    "full_name": "ФИО", "birthday": "Дата рождения", "position": "Должность",
    "department": "Отдел", "characteristic": "Характеристика от HR",
    "gift_format": "Формат (премия и т.п.)", "phone": "Телефон", "email": "Email",
    "comment": "Комментарий",
}
PARTNER_FIELDS = {
    "full_name": "ФИО", "birthday": "Дата рождения", "company": "Компания (город)",
    "category": "Категория", "responsible_id": "Ответственный",
    "contact_type": "Тип (клиент/партнёр)", "gift_format": "Что дарим",
    "congrat_method": "Как поздравляем", "congratulator": "Кто поздравляет",
    "phone": "Телефон", "messenger": "Мессенджер", "email": "Email", "comment": "Комментарий",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- низкоуровневое ---
    async def fetchone(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, tuple(args)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, tuple(args)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def execute(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = await self.conn.execute(sql, tuple(args))
        await self.conn.commit()
        return cur.lastrowid

    # --- настройки ---
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row and row["value"] is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # --- пользователи ---
    async def user_by_tg(self, tg_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    async def user(self, user_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM users WHERE id=?", (user_id,))

    async def users(self, roles: Iterable[str] | None = None, only_active: bool = True,
                    only_linked: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM users WHERE 1=1", []
        if roles:
            roles = list(roles)
            sql += f" AND role IN ({','.join('?' * len(roles))})"
            args += roles
        if only_active:
            sql += " AND is_active=1"
        if only_linked:
            sql += " AND tg_id IS NOT NULL"
        sql += " ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'hr' THEN 1 ELSE 2 END, name"
        return await self.fetchall(sql, args)

    async def create_user(self, name: str, role: str, tg_id: int | None = None) -> int:
        return await self.execute(
            "INSERT INTO users(name, role, tg_id, created_at) VALUES (?, ?, ?, ?)",
            (name, role, tg_id, now_iso()))

    async def ensure_admins(self, admin_ids: list[int]) -> None:
        for tg in admin_ids:
            u = await self.user_by_tg(tg)
            if not u:
                await self.create_user("Администратор", "admin", tg)
            elif u["role"] != "admin" or not u["is_active"]:
                await self.execute("UPDATE users SET role='admin', is_active=1 WHERE id=?", (u["id"],))

    async def update_user(self, user_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))

    # --- приглашения ---
    async def create_invite(self, user_id: int, created_by: int | None, ttl_days: int) -> str:
        token = secrets.token_urlsafe(12)
        expires = (datetime.now() + timedelta(days=ttl_days)).replace(microsecond=0).isoformat()
        # старые неиспользованные ссылки этого человека гасим
        await self.execute(
            "UPDATE invites SET expires_at=? WHERE user_id=? AND used_at IS NULL",
            (now_iso(), user_id))
        await self.execute(
            "INSERT INTO invites(token, user_id, created_by, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, created_by, expires))
        return token

    async def invite(self, token: str) -> dict | None:
        return await self.fetchone("SELECT * FROM invites WHERE token=?", (token,))

    # --- люди ---
    async def person(self, pid: int) -> dict | None:
        return await self.fetchone(
            "SELECT p.*, u.name AS responsible_name, u.tg_id AS responsible_tg, "
            "u.is_active AS responsible_active "
            "FROM persons p LEFT JOIN users u ON u.id = p.responsible_id WHERE p.id=?", (pid,))

    async def persons(self, kind: str | None = None, active: bool | None = True,
                      responsible_id: int | None = None) -> list[dict]:
        sql = ("SELECT p.*, u.name AS responsible_name, u.tg_id AS responsible_tg, "
               "u.is_active AS responsible_active "
               "FROM persons p LEFT JOIN users u ON u.id = p.responsible_id WHERE 1=1")
        args: list = []
        if kind:
            sql += " AND p.kind=?"
            args.append(kind)
        if active is not None:
            sql += " AND p.is_active=?"
            args.append(1 if active else 0)
        if responsible_id is not None:
            sql += " AND p.responsible_id=?"
            args.append(responsible_id)
        return await self.fetchall(sql + " ORDER BY p.full_name", args)

    async def search_persons(self, q: str, responsible_id: int | None = None,
                             limit: int = 20) -> list[dict]:
        # SQLite LOWER не работает с кириллицей — фильтруем в Python
        rows = await self.persons(active=None, responsible_id=responsible_id)
        q = q.lower().strip()
        hits = [r for r in rows
                if q in (r["full_name"] or "").lower() or q in (r["company"] or "").lower()]
        hits.sort(key=lambda r: (not r["is_active"], r["full_name"]))
        return hits[:limit]

    async def create_person(self, data: dict) -> int:
        data = {**data, "created_at": now_iso(), "updated_at": now_iso()}
        cols = ", ".join(data)
        return await self.execute(
            f"INSERT INTO persons({cols}) VALUES ({','.join('?' * len(data))})",
            list(data.values()))

    async def update_person(self, pid: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.execute(f"UPDATE persons SET {sets} WHERE id=?", (*fields.values(), pid))

    async def departments(self) -> list[str]:
        rows = await self.fetchall(
            "SELECT department, COUNT(*) c FROM persons WHERE kind='employee' AND is_active=1 "
            "AND department IS NOT NULL AND department<>'' GROUP BY department ORDER BY c DESC")
        return [r["department"] for r in rows]

    # --- события ---
    async def get_event(self, person_id: int, on: date) -> dict:
        ev = await self.fetchone(
            "SELECT * FROM events WHERE person_id=? AND year=?", (person_id, on.year))
        if ev:
            return ev
        await self.execute(
            "INSERT OR IGNORE INTO events(person_id, year, celebrate_on, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)", (person_id, on.year, on.isoformat(), now_iso(), now_iso()))
        return await self.fetchone(
            "SELECT * FROM events WHERE person_id=? AND year=?", (person_id, on.year))

    async def event(self, event_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM events WHERE id=?", (event_id,))

    async def update_event(self, event_id: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.execute(f"UPDATE events SET {sets} WHERE id=?", (*fields.values(), event_id))

    async def previous_texts(self, person_id: int, before_year: int, limit: int = 3) -> list[str]:
        rows = await self.fetchall(
            "SELECT draft_text FROM events WHERE person_id=? AND year<? AND draft_text IS NOT NULL "
            "ORDER BY year DESC LIMIT ?", (person_id, before_year, limit))
        return [r["draft_text"] for r in rows]

    async def person_events(self, person_id: int) -> list[dict]:
        return await self.fetchall(
            "SELECT e.*, u.name AS done_by_name FROM events e LEFT JOIN users u ON u.id=e.done_by "
            "WHERE person_id=? ORDER BY year DESC LIMIT 10", (person_id,))

    # --- группы ---
    async def chats(self, only_active: bool = True) -> list[dict]:
        sql = "SELECT * FROM chats" + (" WHERE is_active=1" if only_active else "")
        return await self.fetchall(sql + " ORDER BY title")

    async def chat(self, chat_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM chats WHERE chat_id=?", (chat_id,))

    async def upsert_chat(self, chat_id: int, title: str, added_by: int | None) -> None:
        await self.execute(
            "INSERT INTO chats(chat_id, title, added_by, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, is_active=1",
            (chat_id, title, added_by, now_iso()))
