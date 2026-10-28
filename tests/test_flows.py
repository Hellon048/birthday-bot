"""Сквозные сценарии на эмуляции Telegram (без сети).

    python -m tests.test_flows путь/к/мигрированной.db
"""
from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import sys
import tempfile
from datetime import datetime, time, timedelta

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (EditMessageReplyMarkup, EditMessageText, GetChat, GetFile, GetMe,
                             SendDocument, SendMessage, TelegramMethod)
from aiogram.types import (CallbackQuery, Chat, ChatFullInfo, ChatMemberLeft, ChatMemberMember,
                           ChatMemberUpdated, Document, File, Message, Update, User)

from app.config import load_config
from app.db import DB
from app.main import build

ADMIN, MANAGER, STRANGER, GROUP = 100, 200, 300, -100500
BOT_USER = User(id=42, is_bot=True, first_name="BDay", username="bday_test_bot")
_ids = itertools.count(1000)


class AutoSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.log: list[TelegramMethod] = []

    async def close(self):
        pass

    upload: str = ""

    async def stream_content(self, *a, **k):
        with open(self.upload, "rb") as f:
            yield f.read()

    async def make_request(self, bot, method, timeout=None):
        self.log.append(method)
        now = datetime.now()
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, GetChat):
            return ChatFullInfo.model_construct(id=method.chat_id, type="supergroup", title="Тест-группа")
        if isinstance(method, (SendMessage, EditMessageText, SendDocument)):
            cid = getattr(method, "chat_id", None) or ADMIN
            return Message(message_id=next(_ids), date=now, chat=Chat(id=cid, type="private"),
                           from_user=BOT_USER, text=getattr(method, "text", None) or "doc")
        if isinstance(method, EditMessageReplyMarkup):
            return True
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="docs/x.db")
        return True


def user(uid: int, name: str = "Test") -> User:
    return User(id=uid, is_bot=False, first_name=name, username=f"u{uid}")


def msg(uid: int, text: str) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=user(uid), text=text))


def cb(uid: int, data: str) -> Update:
    m = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                from_user=BOT_USER, text="…")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=user(uid), chat_instance="x", message=m, data=data))


def sent(session: AutoSession, chat_id: int | None = None) -> list[str]:
    out = []
    for m in session.log:
        if isinstance(m, (SendMessage, EditMessageText)) and (chat_id is None or m.chat_id == chat_id):
            out.append(m.text)
    return out


def buttons(session: AutoSession) -> list[str]:
    last = [m for m in session.log if isinstance(m, (SendMessage, EditMessageText)) and m.reply_markup]
    rm = last[-1].reply_markup
    if hasattr(rm, "inline_keyboard"):
        return [b.callback_data or b.url for row in rm.inline_keyboard for b in row]
    return [b.text for row in rm.keyboard for b in row]


ok_count = 0
session_samples: list = []


def check(cond, label):
    global ok_count
    if not cond:
        raise AssertionError(label)
    ok_count += 1
    print("  ✓", label)


async def run(src_db: str):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    shutil.copy(src_db, path)
    os.environ.update(BOT_TOKEN="42:TEST", ADMIN_IDS=str(ADMIN), DB_PATH=path)
    cfg = load_config()
    cfg.morning_time = time(0, 0)   # «утро» уже наступило
    cfg.post_time = time(23, 59)    # публикация ещё впереди
    db = DB(path)
    await db.connect()
    await db.ensure_admins(cfg.admin_ids)
    session = AutoSession()
    bot = Bot("42:TEST", session=session)
    dp, n = build(cfg, bot, db)
    try:
        await scenario(dp, bot, db, n, session)
    finally:
        await db.close()


async def scenario(dp, bot, db, n, session):
    feed = lambda u: dp.feed_update(bot, u)  # noqa: E731
    today = n.today()

    print("Доступ")
    await feed(msg(STRANGER, "/start"))
    check("закрытый бот" in sent(session, STRANGER)[-1], "посторонний не получает доступ")
    await feed(msg(STRANGER, "Ниязов"))
    check("Нет доступа" in sent(session, STRANGER)[-1], "поиск посторонним закрыт")
    await feed(msg(ADMIN, "/start"))
    check("администратор" in sent(session, ADMIN)[-1], "админ из ADMIN_IDS входит")

    print("Приглашение менеджера по ссылке")
    await feed(msg(ADMIN, "👥 Доступы"))
    om = await db.fetchone(
        "SELECT u.*, COUNT(p.id) cnt FROM users u JOIN persons p ON p.responsible_id=u.id AND p.is_active=1 "
        "WHERE u.tg_id IS NULL GROUP BY u.id ORDER BY cnt DESC LIMIT 1")
    check(f"u:view:{om['id']}" in buttons(session), "заготовка ответственного есть в списке")
    await feed(cb(ADMIN, f"u:view:{om['id']}"))
    check(f"u:link:{om['id']}" in buttons(session), "у неподключённого есть кнопка ссылки")
    await feed(cb(ADMIN, f"u:link:{om['id']}"))
    link_text = sent(session, ADMIN)[-1]
    check("t.me/bday_test_bot?start=inv_" in link_text, "сгенерирована deep-link ссылка")
    token = link_text.split("start=inv_")[1].split()[0]
    await feed(msg(MANAGER, f"/start inv_{token}"))
    check(f"За вами закреплено партнёров: {om['cnt']}" in sent(session, MANAGER)[0],
          "менеджер привязан, партнёры закрепились")
    check(any("подключился по приглашению" in t for t in sent(session, ADMIN)), "админу пришло уведомление")
    await feed(msg(STRANGER, f"/start inv_{token}"))
    check("уже использована" in sent(session, STRANGER)[-1], "ссылка одноразовая")

    print("Новый человек по приглашению (HR)")
    await feed(cb(ADMIN, "u:new"))
    await feed(cb(ADMIN, "inv:role:hr"))
    await feed(msg(ADMIN, "Петрова Ольга"))
    check("Ссылка для <b>Петрова Ольга</b> (HR)" in sent(session, ADMIN)[-1], "создан HR и ссылка")

    print("Менеджер видит только своих")
    await feed(msg(MANAGER, "🎂 Ближайшие"))
    mine = {r["id"] for r in await db.persons(responsible_id=om["id"])}
    ids = [int(b.split(":")[2]) for b in buttons(session) if b.startswith("p:view:")]
    check(all(i in mine for i in ids), "в «Ближайших» только свои партнёры")
    other = await db.fetchone("SELECT id FROM persons WHERE kind='employee' LIMIT 1")
    before = len(sent(session, MANAGER))
    await feed(cb(MANAGER, f"p:view:{other['id']}"))
    from aiogram.methods import AnswerCallbackQuery
    denied = [m for m in session.log if isinstance(m, AnswerCallbackQuery)][-1]
    check(len(sent(session, MANAGER)) == before and denied.text == "Нет доступа",
          "чужую карточку открыть нельзя")

    print("Менеджер добавляет партнёра")
    await feed(msg(MANAGER, "➕ Добавить"))
    await feed(msg(MANAGER, "Сергей (закупщик)"))
    await feed(msg(MANAGER, "31 февраля"))
    check("⚠️" in sent(session, MANAGER)[-1], "кривая дата отклонена")
    await feed(msg(MANAGER, "12.03"))
    await feed(msg(MANAGER, "Дымный бар (Пермь)"))
    await feed(cb(MANAGER, "add:gift:0"))
    await feed(cb(MANAGER, "add:skip"))
    check("Проверьте" in sent(session, MANAGER)[-1], "показано подтверждение")
    await feed(cb(MANAGER, "add:save"))
    p = await db.fetchone("SELECT * FROM persons WHERE full_name='Сергей (закупщик)'")
    check(p and p["responsible_id"] == om["id"] and p["birth_year"] is None and p["gift_format"] == "мерч",
          "партнёр сохранён, ответственный = сам менеджер, год неизвестен")

    print("Сотрудник с ДР завтра → черновик HR")
    tomorrow = today + timedelta(days=1)
    session.log.clear()
    await feed(msg(ADMIN, "➕ Добавить"))
    await feed(cb(ADMIN, "add:kind:employee"))
    await feed(msg(ADMIN, "Тестова Анна Петровна"))
    await feed(msg(ADMIN, f"{tomorrow:%d.%m}.1990"))
    await feed(msg(ADMIN, "Технолог"))
    await feed(cb(ADMIN, "add:dep:0"))
    await feed(msg(ADMIN, "Придумала половину вкусов линейки, спокойная и надёжная"))
    await feed(cb(ADMIN, "add:save"))
    emp = await db.fetchone("SELECT * FROM persons WHERE full_name='Тестова Анна Петровна'")
    ev = await db.fetchone("SELECT * FROM events WHERE person_id=?", (emp["id"],))
    session_samples.extend(m for m in session.log if isinstance(m, SendMessage) and m.text and "🎂" in m.text)
    check(ev and ev["status"] == "draft" and ev["draft_text"], "черновик сгенерирован сразу после добавления")
    check(any("ev:ok:" in b for b in buttons(session)), "у черновика есть кнопки модерации")
    check(any("группа не прикреплена" in t for t in sent(session, ADMIN)), "предупреждение: группы нет")

    print("Прикрепление группы")
    await dp.feed_update(bot, Update(update_id=next(_ids), my_chat_member=ChatMemberUpdated(
        chat=Chat(id=GROUP, type="supergroup", title="Коллеги"), from_user=user(ADMIN), date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=BOT_USER), new_chat_member=ChatMemberMember(user=BOT_USER))))
    check(await db.chat(GROUP), "группа прикрепилась при добавлении бота админом")
    await dp.feed_update(bot, Update(update_id=next(_ids), my_chat_member=ChatMemberUpdated(
        chat=Chat(id=-999, type="group", title="Чужая"), from_user=user(STRANGER), date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=BOT_USER), new_chat_member=ChatMemberMember(user=BOT_USER))))
    c999 = await db.chat(-999)
    check(not c999["is_active"], "посторонний не может прикрепить группу")
    check(any("Меня добавили в группу «Чужая»" in t for t in sent(session, ADMIN)), "бот не ушёл молча, а спросил админа")
    await feed(cb(ADMIN, "g:accept:-999"))
    check((await db.chat(-999))["is_active"], "админ прикрепил группу кнопкой")
    await feed(cb(ADMIN, "g:off:-999"))

    anon = User(id=1087968824, is_bot=True, first_name="Group", username="GroupAnonymousBot")
    await dp.feed_update(bot, Update(update_id=next(_ids), my_chat_member=ChatMemberUpdated(
        chat=Chat(id=-777, type="supergroup", title="Анонимная"), from_user=anon, date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=BOT_USER), new_chat_member=ChatMemberMember(user=BOT_USER))))
    check(any("анонимный администратор" in t for t in sent(session, ADMIN)), "анонимный админ → вопрос админам")

    def gmsg(uid, text, chat_id=-555):
        return Update(update_id=next(_ids), message=Message(
            message_id=next(_ids), date=datetime.now(), chat=Chat(id=chat_id, type="supergroup", title="Склад"),
            from_user=user(uid), text=text))
    session.log.clear()
    await feed(gmsg(STRANGER, "/bind"))
    check(not await db.chat(-555) and "только администратор" in sent(session, -555)[-1], "/bind от постороннего отклонён")
    await feed(gmsg(ADMIN, "/bind@bday_test_bot"))
    check((await db.chat(-555))["is_active"], "/bind от админа прикрепил группу")
    await feed(gmsg(ADMIN, "/start@bday_test_bot bday", -556))
    check((await db.chat(-556))["is_active"], "/start после ссылки «добавить в группу» прикрепил группу")
    for cid in (-555, -556):
        await feed(cb(ADMIN, f"g:off:{cid}"))

    print("Модерация и публикация")
    await feed(cb(ADMIN, f"ev:re:{ev['id']}"))
    await feed(cb(ADMIN, f"ev:ok:{ev['id']}"))
    check((await db.event(ev["id"]))["status"] == "approved", "одобрено")
    session.log.clear()
    await n.post_today(tomorrow)
    check((await db.event(ev["id"]))["status"] == "sent", "в день рождения опубликовано")
    check(sent(session, GROUP), "сообщение ушло в группу")
    await n.post_today(tomorrow)
    check(len(sent(session, GROUP)) == 1, "повторный запуск не дублирует")

    print("Сотрудник без характеристики → вопрос HR, без ответа → автопубликация")
    nochar = await db.create_person({"kind": "employee", "full_name": "Безхарактерный Иван",
                                     "birth_day": tomorrow.day, "birth_month": tomorrow.month,
                                     "department": "Склад"})
    session.log.clear()
    await n.morning(today)
    check(any("Напишите 1–2 предложения" in t for t in sent(session)), "HR спросили характеристику")
    session.log.clear()
    await n.post_today(tomorrow)
    check(sent(session, GROUP), "без ответа HR всё равно опубликовано")

    print("Отказ от публикации")
    skip_id = await db.create_person({"kind": "employee", "full_name": "Скромный Пётр",
                                      "birth_day": tomorrow.day, "birth_month": tomorrow.month})
    await n.morning(today)
    sev = await db.fetchone("SELECT * FROM events WHERE person_id=?", (skip_id,))
    await feed(cb(ADMIN, f"ev:no:{sev['id']}"))
    session.log.clear()
    await n.post_today(tomorrow)
    check(not sent(session, GROUP), "отменённое не публикуется")

    print("Напоминания по партнёрам")
    target = await db.fetchone("SELECT * FROM persons WHERE kind='partner' AND is_active=1 AND responsible_id=?",
                               (om["id"],))
    from datetime import date
    bd = date(today.year, target["birth_month"], target["birth_day"])
    fake_today = bd - timedelta(days=7)
    session.log.clear()
    await n.morning(fake_today)
    texts = sent(session, MANAGER)
    session_samples.extend(m for m in session.log if isinstance(m, SendMessage) and m.chat_id == MANAGER)
    check(any(target["full_name"] in t and "Через 7 дней" in t for t in texts), "за 7 дней — ответственному")
    await n.morning(fake_today)
    check(len([t for t in sent(session, MANAGER) if target["full_name"] in t]) == 1, "без дублей")
    unlinked = await db.fetchone("SELECT p.* FROM persons p JOIN users u ON u.id=p.responsible_id "
                                 "WHERE u.tg_id IS NULL AND p.is_active=1 AND p.kind='partner' LIMIT 1")
    bd2 = date(today.year, unlinked["birth_month"], unlinked["birth_day"])
    session.log.clear()
    await n.morning(bd2 - timedelta(days=1))
    check(any("не подключён к боту" in t for t in sent(session, ADMIN)), "неподключённый → напоминание админу")
    session.log.clear()
    await n.morning(bd)
    pev = await db.fetchone("SELECT * FROM events WHERE person_id=? AND year=?", (target["id"], bd.year))
    await feed(cb(MANAGER, f"pt:gen:{pev['id']}"))
    check(any("<code>" in t for t in sent(session, MANAGER)), "сгенерирован текст для партнёра")
    await feed(cb(MANAGER, f"pt:done:{pev['id']}"))
    await feed(cb(MANAGER, f"pt:m:{pev['id']}:merch"))
    check((await db.event(pev["id"]))["status"] == "done", "отмечено «поздравил»")
    session.log.clear()
    await n.escalate(bd)
    check(not any(target["full_name"] in t for t in sent(session, ADMIN)), "отмеченного нет в эскалации")

    print("Сводка, поиск, правка, экспорт")
    session.log.clear()
    await n.digest(today)
    check(sent(session), "недельная сводка отправлена")
    await feed(msg(ADMIN, "ниязов"))
    check(any(b.startswith("p:view:") for b in buttons(session)), "поиск по кириллице без учёта регистра")
    pid = int([b for b in buttons(session) if b.startswith("p:view:")][0].split(":")[2])
    await feed(cb(ADMIN, f"p:f:{pid}:birthday"))
    await feed(msg(ADMIN, "07.11"))
    pp = await db.person(pid)
    check((pp["birth_day"], pp["birth_month"]) == (7, 11), "дата изменена")
    session.log.clear()
    await feed(msg(ADMIN, "📤 Экспорт"))
    check(any(isinstance(m, SendDocument) for m in session.log), "Excel выгружен")
    await feed(msg(MANAGER, "📤 Экспорт"))
    check(not any(isinstance(m, SendDocument) and m.chat_id == MANAGER for m in session.log),
          "менеджеру экспорт недоступен")

    legacy = os.environ.get("LEGACY_DB")
    if not legacy:
        print("(LEGACY_DB не задан — проверки загрузки базы пропущены)")
        return await scheduler_check(db, n)
    print("Загрузка базы через бота")
    import sqlite3
    upd = os.path.join(os.path.dirname(db.path), "legacy_upd.db")
    shutil.copy(legacy, upd)
    c = sqlite3.connect(upd)
    c.execute("insert into contacts(full_name,company,birthday,responsible,is_active,created_at,updated_at) "
              "values('Загруженный Партнёр','Бар (Омск)','2026-12-01','Новый Менеджер',1,'2026-10-28T12:00:00','2026-10-28T12:00:00')")
    c.commit(); c.close()
    session.upload = upd

    def doc_msg(uid, name):
        return Update(update_id=next(_ids), message=Message(
            message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=user(uid),
            document=Document(file_id="f1", file_unique_id="u1", file_name=name, file_size=200000)))
    session.log.clear()
    await feed(doc_msg(MANAGER, "birthday_calendar.db"))
    check(not await db.fetchone("SELECT 1 FROM persons WHERE full_name='Загруженный Партнёр'"),
          "менеджер не может загрузить базу")
    await feed(doc_msg(ADMIN, "birthday_calendar.db"))
    preview = sent(session, ADMIN)[-1]
    check("Пробный прогон" in preview and "партнёров: 1" in preview, "показан пробный прогон")
    check(not await db.fetchone("SELECT 1 FROM persons WHERE full_name='Загруженный Партнёр'"),
          "до подтверждения ничего не записано")
    tok = [b for b in buttons(session) if b.startswith("imp:apply:")][0]
    await feed(cb(ADMIN, tok))
    row = await db.fetchone("SELECT p.*, u.name un FROM persons p JOIN users u ON u.id=p.responsible_id "
                            "WHERE full_name='Загруженный Партнёр'")
    check(row and row["un"] == "Новый Менеджер", "после подтверждения партнёр добавлен с новым ответственным")
    linked = await db.user_by_tg(MANAGER)
    check(linked and linked["id"] == om["id"], "привязка Telegram менеджера сохранилась")
    check((await db.fetchone("SELECT characteristic FROM persons WHERE full_name='Тестова Анна Петровна'"))
          ["characteristic"], "характеристика из бота сохранилась")
    check(os.listdir(os.path.join(os.path.dirname(db.path), "backups")), "перед импортом сделан бэкап")
    await feed(cb(ADMIN, tok))
    check(sum("Импорт применён" in t for t in sent(session, ADMIN)) == 1, "повторное нажатие не применяет дважды")

    print("Восстановление из копии")
    session.log.clear()
    await feed(cb(ADMIN, "s:backup"))
    bk = [m for m in session.log if isinstance(m, SendDocument)]
    check(bk, "резервная копия отправлена")
    bdir = os.path.join(os.path.dirname(db.path), "backups")
    first = sorted(f for f in os.listdir(bdir) if f.endswith("_import.db"))[0]
    session.upload = os.path.join(bdir, first)
    await feed(doc_msg(ADMIN, "backup.db"))
    tok = [b for b in buttons(session) if b.startswith("imp:restore:")][0]
    await feed(cb(ADMIN, tok))
    check(not await db.fetchone("SELECT 1 FROM persons WHERE full_name='Загруженный Партнёр'"),
          "восстановлено состояние до импорта")
    check(await db.user_by_tg(ADMIN), "админ не потерял доступ после восстановления")

    await scheduler_check(db, n)


async def scheduler_check(db, n):
    today = n.today()
    print("Планировщик")
    for k in ("morning", "post", "escalate", "digest"):
        await db.set_setting(f"last_{k}", "")
    from app.notifier import Scheduler
    await Scheduler(n).tick()
    check(await db.get_setting("last_morning") == today.isoformat(), "задачи отмечены выполненными")

    if os.environ.get("SHOW"):
        for t in sent(session)[:0] + [m.text for m in session_samples]:
            print("-----\n" + t)
    print(f"\nВсе проверки пройдены: {ok_count}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "data/birthdays.db"))
