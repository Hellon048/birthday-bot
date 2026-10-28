"""Вся «автоматика»: напоминания, черновики для HR, публикация, эскалация, сводка.

Каждая операция идемпотентна (флаги в events), поэтому повторный запуск
после рестарта ничего не задублирует.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramMigrateToChat
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Config
from .dates import fmt_short, next_occurrence, when_label
from .db import DB, now_iso
from .llm import Generator
from .ui import h

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, db: DB, cfg: Config, gen: Generator):
        self.bot, self.db, self.cfg, self.gen = bot, db, cfg, gen
        self._locks: dict[int, asyncio.Lock] = {}

    # ---------- общие ----------
    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def today(self) -> date:
        return self.now().date()

    def past_post_time(self) -> bool:
        return self.now().time() >= self.cfg.post_time

    async def company(self) -> str:
        return await self.db.get_setting("company_name")

    async def feb29(self) -> str:
        return await self.db.get_setting("feb29_rule", "28.02")

    def lock(self, event_id: int) -> asyncio.Lock:
        return self._locks.setdefault(event_id, asyncio.Lock())

    async def send(self, chat_id: int, text: str, markup=None) -> bool:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup,
                                        disable_web_page_preview=True)
            return True
        except TelegramAPIError as e:
            log.warning("Не удалось отправить в %s: %s", chat_id, e)
            return False

    async def hr_targets(self) -> list[dict]:
        hr = await self.db.users(roles=["hr"], only_linked=True)
        return hr or await self.db.users(roles=["admin"], only_linked=True)

    async def admin_targets(self) -> list[dict]:
        return await self.db.users(roles=["admin"], only_linked=True)

    # ---------- клавиатуры ----------
    @staticmethod
    def draft_kb(ev: dict, has_char: bool):
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Отправить", callback_data=f"ev:ok:{ev['id']}")
        kb.button(text="🔄 Другой вариант", callback_data=f"ev:re:{ev['id']}")
        kb.button(text="✏️ Свой текст", callback_data=f"ev:ed:{ev['id']}")
        kb.button(text="✍️ Изменить характеристику" if has_char else "✍️ Добавить характеристику",
                  callback_data=f"ev:ch:{ev['id']}")
        kb.button(text="🚫 Не публиковать", callback_data=f"ev:no:{ev['id']}")
        kb.adjust(2, 1, 1, 1)
        return kb.as_markup()

    @staticmethod
    def ask_char_kb(ev: dict):
        kb = InlineKeyboardBuilder()
        kb.button(text="✍️ Написать характеристику", callback_data=f"ev:ch:{ev['id']}")
        kb.button(text="⚡ Сгенерировать без неё", callback_data=f"ev:gen:{ev['id']}")
        kb.button(text="🚫 Не публиковать", callback_data=f"ev:no:{ev['id']}")
        kb.adjust(1)
        return kb.as_markup()

    @staticmethod
    def partner_kb(ev: dict):
        kb = InlineKeyboardBuilder()
        kb.button(text="✍️ Текст поздравления", callback_data=f"pt:gen:{ev['id']}")
        kb.button(text="✅ Поздравил", callback_data=f"pt:done:{ev['id']}")
        kb.adjust(2)
        return kb.as_markup()

    # ---------- тексты ----------
    def _who(self, p: dict) -> str:
        bits = [f"<b>{h(p['full_name'])}</b>"]
        extra = [x for x in (p.get("position"), p.get("department")) if x]
        if extra:
            bits.append(h(", ".join(extra)))
        return " — ".join(bits)

    async def chats_for(self, p: dict) -> list[dict]:
        out = []
        for c in await self.db.chats():
            deps = [d for d in (c["departments"] or "").split("|") if d]
            if not deps or (p.get("department") in deps):
                out.append(c)
        return out

    async def draft_text_html(self, ev: dict, p: dict) -> str:
        d = date.fromisoformat(ev["celebrate_on"])
        delta = (d - self.today()).days
        chats = await self.chats_for(p)
        where = ", ".join(f"«{h(c['title'])}»" for c in chats) or "⚠️ группа не прикреплена"
        if ev["status"] == "sent":
            footer = "✅ Опубликовано."
        elif ev["status"] == "skipped":
            footer = "🚫 Публикация отменена."
        elif ev["status"] == "approved":
            footer = ("✅ Одобрено, публикую." if delta <= 0 and self.past_post_time()
                      else f"✅ Одобрено — опубликую {fmt_short(d)} в {self.cfg.post_time:%H:%M}.")
        elif delta <= 0 and self.past_post_time():
            footer = "Нажмите «Отправить», чтобы опубликовать сейчас."
        else:
            footer = (f"Если ничего не нажать — опубликую как есть {fmt_short(d)} "
                      f"в {self.cfg.post_time:%H:%M}.")
        char = f"\nХарактеристика: <i>{h(p['characteristic'])}</i>" if p.get("characteristic") else ""
        return (f"🎂 {when_label(max(delta, 0))}, {fmt_short(d)}, день рождения: {self._who(p)}{char}\n"
                f"Куда: {where}\n\n"
                f"<blockquote>{h(ev['draft_text'])}</blockquote>\n\n{footer}")

    # ---------- сотрудники ----------
    async def make_draft(self, ev: dict, p: dict) -> dict:
        prev = await self.db.previous_texts(p["id"], ev["year"])
        text = await self.gen.employee(await self.company(), p, prev)
        await self.db.update_event(ev["id"], draft_text=text, status="draft")
        return await self.db.event(ev["id"])

    async def send_draft(self, ev: dict, p: dict, chat_ids: list[int] | None = None) -> None:
        targets = chat_ids or [u["tg_id"] for u in await self.hr_targets()]
        text = await self.draft_text_html(ev, p)
        kb = self.draft_kb(ev, bool(p.get("characteristic")))
        for tg in targets:
            await self.send(tg, text, kb)

    async def _employee(self, p: dict, ev: dict, delta: int) -> None:
        targets = await self.hr_targets()
        d = date.fromisoformat(ev["celebrate_on"])
        if delta == 7 and p["remind_7"] and not ev["r7"]:
            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Характеристика", callback_data=f"ev:ch:{ev['id']}")
            msg = (f"📅 Через неделю, {fmt_short(d)}, день рождения: {self._who(p)}"
                   + (f"\nФормат: {h(p['gift_format'])}" if p.get("gift_format") else ""))
            if p["public_congrats"] and not p.get("characteristic"):
                msg += "\n\nМожно заранее написать пару слов о человеке — по ним сгенерирую поздравление."
            for u in targets:
                await self.send(u["tg_id"], msg, kb.as_markup() if p["public_congrats"] else None)
            await self.db.update_event(ev["id"], r7=1)

        if delta > 1 or ev["hr_notified"]:
            return
        if not p["public_congrats"]:
            msg = (f"🎂 {when_label(delta)}, {fmt_short(d)}, день рождения: {self._who(p)}\n"
                   f"Публичное поздравление выключено"
                   + (f". Формат: {h(p['gift_format'])}" if p.get("gift_format") else "."))
            for u in targets:
                await self.send(u["tg_id"], msg)
            await self.db.update_event(ev["id"], hr_notified=1)
            return
        if not p.get("characteristic") and ev["status"] == "pending":
            msg = (f"🎂 {when_label(delta)}, {fmt_short(d)}, день рождения: {self._who(p)}"
                   + (f"\nФормат: {h(p['gift_format'])}" if p.get("gift_format") else "")
                   + "\n\nНапишите 1–2 предложения о человеке (какой он, за что ценят) — "
                     "по ним сгенерирую поздравление для группы.\n"
                     f"Если не ответить — сгенерирую сам и опубликую {fmt_short(d)} "
                     f"в {self.cfg.post_time:%H:%M}.")
            for u in targets:
                await self.send(u["tg_id"], msg, self.ask_char_kb(ev))
        else:
            if not ev["draft_text"]:
                ev = await self.make_draft(ev, p)
            await self.send_draft(ev, p)
        await self.db.update_event(ev["id"], hr_notified=1)

    async def publish(self, ev: dict, p: dict) -> int:
        """Публикует поздравление во все подходящие группы. Возвращает число групп."""
        async with self.lock(ev["id"]):
            ev = await self.db.event(ev["id"])
            if ev["status"] in ("sent", "skipped"):
                return 0
            if not ev["draft_text"]:
                ev = await self.make_draft(ev, p)
            sent = 0
            for c in await self.chats_for(p):
                try:
                    await self.bot.send_message(c["chat_id"], ev["draft_text"], parse_mode=None)
                    sent += 1
                except TelegramMigrateToChat as e:
                    await self.db.execute("UPDATE chats SET chat_id=? WHERE chat_id=?",
                                          (e.migrate_to_chat_id, c["chat_id"]))
                    await self.bot.send_message(e.migrate_to_chat_id, ev["draft_text"], parse_mode=None)
                    sent += 1
                except TelegramAPIError as e:
                    log.warning("Группа %s недоступна: %s", c["chat_id"], e)
                    for u in await self.hr_targets():
                        await self.send(u["tg_id"], f"⚠️ Не смог написать в группу «{h(c['title'])}»: {h(e.message)}")
            if sent:
                await self.db.update_event(ev["id"], status="sent", posted_at=now_iso())
                for u in await self.hr_targets():
                    await self.send(u["tg_id"], f"✅ Поздравление для {self._who(p)} опубликовано ({sent} гр.).")
            else:
                for u in await self.hr_targets():
                    await self.send(u["tg_id"], f"⚠️ Поздравление для {self._who(p)} не опубликовано: "
                                                "нет прикреплённой группы для этого отдела.")
            return sent

    # ---------- партнёры ----------
    async def partner_text(self, ev: dict, p: dict, delta: int) -> str:
        d = date.fromisoformat(ev["celebrate_on"])
        lines = [f"🎁 {when_label(delta)}, {fmt_short(d)}, день рождения партнёра",
                 f"<b>{h(p['full_name'])}</b>"]
        sub = " · ".join(h(x) for x in (p.get("company"), p.get("category")) if x)
        if sub:
            lines.append(sub)
        plan = " · ".join(x for x in (
            f"Дарим: {h(p['gift_format'])}" if p.get("gift_format") else "",
            f"Как: {h(p['congrat_method'])}" if p.get("congrat_method") else "",
            f"Поздравляет: {h(p['congratulator'])}" if p.get("congratulator") else "") if x)
        if plan:
            lines.append(plan)
        for key, label in (("phone", "📞"), ("messenger", "💬"), ("comment", "📝")):
            if p.get(key):
                lines.append(f"{label} {h(p[key])}")
        return "\n".join(lines)

    async def _partner(self, p: dict, ev: dict, delta: int) -> None:
        flag = {7: "r7", 1: "r1", 0: "r0"}.get(delta)
        if not flag or ev[flag] or ev["status"] == "done":
            return
        if (delta == 7 and not p["remind_7"]) or (delta == 1 and not p["remind_1"]):
            return
        text = await self.partner_text(ev, p, delta)
        if p.get("responsible_tg") and p.get("responsible_active"):
            ok = await self.send(p["responsible_tg"], text, self.partner_kb(ev))
        else:
            ok = False
        if not ok:
            who = p.get("responsible_name")
            note = (f"\n\n⚠️ Ответственный {h(who)} не подключён к боту — напоминание пришло вам."
                    if who else "\n\n⚠️ Ответственный не назначен — напоминание пришло вам.")
            for u in await self.admin_targets():
                await self.send(u["tg_id"], text + note, self.partner_kb(ev))
        await self.db.update_event(ev["id"], **{flag: 1})

    async def backup(self) -> None:
        """Ежедневная копия базы рядом с ней (хранится 14 штук)."""
        import os
        from .importer import make_backup
        base = os.path.dirname(os.path.abspath(self.cfg.db_path))
        await asyncio.to_thread(make_backup, self.cfg.db_path, os.path.join(base, "backups"), "auto", 14)

    # ---------- задачи по расписанию ----------
    async def process_person(self, p: dict, today: date | None = None) -> None:
        today = today or self.today()
        if not p["is_active"]:
            return
        d = next_occurrence(p["birth_day"], p["birth_month"], today, await self.feb29())
        delta = (d - today).days
        if delta > 7:
            return
        ev = await self.db.get_event(p["id"], d)
        if p["kind"] == "partner":
            # если пропустили 7-дневное (человека добавили позже) — шлём то, что актуально
            await self._partner(p, ev, delta)
        else:
            await self._employee(p, ev, delta)

    async def refresh_person(self, pid: int) -> None:
        """Вызывается после добавления/правки — чтобы не ждать утра."""
        p = await self.db.person(pid)
        if p and self.now().time() >= self.cfg.morning_time:
            await self.process_person(p)
            if p["kind"] == "employee" and self.past_post_time():
                d = next_occurrence(p["birth_day"], p["birth_month"], self.today(), await self.feb29())
                if d == self.today():
                    ev = await self.db.get_event(p["id"], d)
                    if ev["status"] == "approved":
                        await self.publish(ev, p)

    async def morning(self, today: date | None = None) -> None:
        for p in await self.db.persons():
            try:
                await self.process_person(p, today)
            except Exception:
                log.exception("Ошибка обработки %s", p["id"])

    async def post_today(self, today: date | None = None) -> None:
        today = today or self.today()
        feb29 = await self.feb29()
        for p in await self.db.persons(kind="employee"):
            if not p["public_congrats"]:
                continue
            if next_occurrence(p["birth_day"], p["birth_month"], today, feb29) != today:
                continue
            ev = await self.db.get_event(p["id"], today)
            if ev["status"] in ("pending", "draft", "approved"):
                try:
                    await self.publish(ev, p)
                except Exception:
                    log.exception("Ошибка публикации %s", p["id"])

    async def escalate(self, today: date | None = None) -> None:
        today = today or self.today()
        feb29 = await self.feb29()
        pending = []
        for p in await self.db.persons(kind="partner"):
            if next_occurrence(p["birth_day"], p["birth_month"], today, feb29) != today:
                continue
            ev = await self.db.get_event(p["id"], today)
            if ev["status"] != "done" and not ev["escalated"]:
                pending.append((p, ev))
        if not pending:
            return
        lines = ["⏰ Сегодня не отмечено поздравление партнёров:"]
        for p, ev in pending:
            lines.append(f"• <b>{h(p['full_name'])}</b> ({h(p.get('company'))}) — "
                         f"отв. {h(p.get('responsible_name')) or 'не назначен'}")
        kb = InlineKeyboardBuilder()
        for p, ev in pending[:20]:
            kb.button(text=f"✅ {p['full_name'][:30]}", callback_data=f"pt:done:{ev['id']}")
        kb.adjust(1)
        for u in await self.admin_targets():
            await self.send(u["tg_id"], "\n".join(lines), kb.as_markup())
        for p, ev in pending:
            await self.db.update_event(ev["id"], escalated=1)

    async def digest(self, today: date | None = None) -> None:
        today = today or self.today()
        feb29 = await self.feb29()
        week = []
        for p in await self.db.persons():
            d = next_occurrence(p["birth_day"], p["birth_month"], today, feb29)
            if (d - today).days < 7:
                week.append((d, p))
        week.sort(key=lambda x: (x[0], x[1]["full_name"]))

        def fmt(items):
            return "\n".join(
                f"• {fmt_short(d)} — {h(p['full_name'])}"
                + (f" ({h(p['company'])})" if p["kind"] == "partner" and p.get("company") else "")
                + (f" ({h(p['department'])})" if p["kind"] == "employee" and p.get("department") else "")
                for d, p in items)

        staff = await self.db.users(roles=["admin", "hr"], only_linked=True)
        emp = [x for x in week if x[1]["kind"] == "employee"]
        par = [x for x in week if x[1]["kind"] == "partner"]
        if week:
            text = "📆 <b>Дни рождения на этой неделе</b>"
            if emp:
                text += "\n\n<b>Сотрудники</b>\n" + fmt(emp)
            if par:
                text += "\n\n<b>Партнёры</b>\n" + fmt(par)
            for u in staff:
                await self.send(u["tg_id"], text)
        for m in await self.db.users(roles=["manager"], only_linked=True):
            mine = [x for x in par if x[1]["responsible_id"] == m["id"]]
            if mine:
                await self.send(m["tg_id"], "📆 <b>Ваши партнёры с днём рождения на этой неделе</b>\n" + fmt(mine))


class Scheduler:
    """Простой планировщик: раз в 30 с проверяет, не пора ли выполнить задачу.
    Отметка о выполнении хранится в БД, поэтому после рестарта пропущенное догоняется."""

    def __init__(self, n: Notifier):
        self.n = n
        self._task: asyncio.Task | None = None

    async def tick(self) -> None:
        now = self.n.now()
        today = now.date().isoformat()
        cfg = self.n.cfg
        jobs = [
            ("morning", cfg.morning_time, True, self.n.morning),
            ("digest", cfg.morning_time, now.weekday() == cfg.digest_weekday, self.n.digest),
            ("post", cfg.post_time, True, self.n.post_today),
            ("escalate", cfg.escalate_time, True, self.n.escalate),
            ("backup", cfg.escalate_time, True, self.n.backup),
        ]
        for name, at, cond, fn in jobs:
            if not cond or now.time() < at:
                continue
            if await self.n.db.get_setting(f"last_{name}") == today:
                continue
            log.info("Запуск задачи %s", name)
            try:
                await fn()
            except Exception:
                log.exception("Задача %s упала", name)
            await self.n.db.set_setting(f"last_{name}", today)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick")
            await asyncio.sleep(30)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())
