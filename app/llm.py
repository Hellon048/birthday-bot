"""Генерация поздравлений через Claude API (прямой HTTP, без SDK)."""
from __future__ import annotations

import logging
import random

import aiohttp

log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"

EMPLOYEE_SYSTEM = """Ты пишешь поздравления с днём рождения от имени компании {company} \
для общего рабочего чата сотрудников в Telegram.

Требования:
- 3–5 предложений, по-русски, тепло и по-человечески, как написал бы внимательный коллега.
- В первом предложении назови сотрудника так, чтобы все поняли, о ком речь: имя и фамилия; \
для старших коллег и руководителей уместно имя-отчество. Склоняй ФИО грамматически правильно.
- Опирайся только на переданные факты (должность, отдел, характеристика). Ничего не выдумывай: \
ни хобби, ни достижений, ни семьи, если этого нет в данных.
- Если есть характеристика от HR — сделай её стержнем текста, но перескажи своими словами.
- Не упоминай возраст. Без канцелярита и штампов вроде «желаем счастья, здоровья, успехов во \
всех начинаниях», «крепкого здоровья и семейного благополучия».
- Не больше 2 эмодзи, без хештегов, без подписи в конце.
- Выведи только сам текст поздравления, без кавычек и пояснений."""

PARTNER_SYSTEM = """Ты помогаешь менеджеру компании {company} написать личное поздравление \
с днём рождения клиенту или партнёру. Сообщение отправят в мессенджер от первого лица менеджера.

Требования:
- 2–4 предложения, деловой, но тёплый тон, обращение на «Вы».
- Обратись по имени (или имени-отчеству, если оно известно). Если в имени есть пометка в \
скобках вроде «(РОП)» — это служебная пометка, в текст её не включай.
- Уместно коротко упомянуть сотрудничество с компанией партнёра, если она указана.
- Ничего не выдумывай о человеке. Не упоминай возраст. Без штампов. Не больше 1 эмодзи.
- Выведи только текст сообщения, без кавычек и подписи."""


def _clean_partner_name(name: str) -> str:
    import re
    return re.sub(r"\s*\(.*?\)", "", name).strip()


class Generator:
    def __init__(self, api_key: str, model: str, proxy: str | None = None):
        self.api_key = api_key
        self.model = model
        self.proxy = proxy

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _ask(self, system: str, user: str) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self.model,
            "max_tokens": 700,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(API_URL, json=payload, headers=headers, proxy=self.proxy) as r:
                    data = await r.json(content_type=None)
                    if r.status != 200:
                        log.error("Claude API %s: %s", r.status, data)
                        return None
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return text.strip().strip('"«»').strip() or None
        except Exception:
            log.exception("Claude API недоступен")
            return None

    async def employee(self, company: str, p: dict, previous: list[str]) -> str:
        facts = [f"ФИО: {p['full_name']}"]
        if p.get("position"):
            facts.append(f"Должность: {p['position']}")
        if p.get("department"):
            facts.append(f"Отдел: {p['department']}")
        if p.get("characteristic"):
            facts.append(f"Характеристика от HR: {p['characteristic']}")
        if p.get("comment"):
            facts.append(f"Заметка: {p['comment']}")
        user = "\n".join(facts)
        if previous:
            user += "\n\nПоздравления прошлых лет (не повторяй их формулировки и структуру):\n" + \
                    "\n---\n".join(previous)
        text = await self._ask(EMPLOYEE_SYSTEM.format(company=company), user)
        return text or self.fallback_employee(p)

    async def partner(self, company: str, p: dict, manager_name: str | None) -> str:
        facts = [f"Имя: {_clean_partner_name(p['full_name'])}"]
        if p.get("company"):
            facts.append(f"Компания партнёра: {p['company']}")
        if p.get("category"):
            facts.append(f"Категория клиента: {p['category']}")
        if p.get("comment"):
            facts.append(f"Заметка менеджера: {p['comment']}")
        if manager_name:
            facts.append(f"Пишет менеджер: {manager_name}")
        text = await self._ask(PARTNER_SYSTEM.format(company=company), "\n".join(facts))
        return text or self.fallback_partner(p)

    @staticmethod
    def fallback_employee(p: dict) -> str:
        parts = p["full_name"].split()
        name = " ".join(parts[1:3]) if len(parts) >= 2 else p["full_name"]
        role = f" — {p['position'].lower()}" if p.get("position") else ""
        return random.choice([
            f"Сегодня свой день рождения празднует {p['full_name']}{role}! 🎉 {name}, спасибо за вашу работу "
            f"и за то, что с вами всегда можно договориться. Пусть этот год будет спокойным, "
            f"интересным и щедрым на хорошие новости.",
            f"Коллеги, сегодня празднует {p['full_name']}{role} 🎂 {name}, поздравляем! "
            f"Пусть работа радует, а всё задуманное получается легко.",
        ])

    @staticmethod
    def fallback_partner(p: dict) -> str:
        name = _clean_partner_name(p["full_name"])
        return (f"{name}, с днём рождения! Спасибо за доверие и наше сотрудничество. "
                f"Желаю отличного года, сильных продаж и хорошего настроения.")
