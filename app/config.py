"""Настройки из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo


def _time(value: str, default: str) -> time:
    raw = (value or default).strip()
    h, m = raw.split(":")
    return time(int(h), int(m))


def _ids(value: str) -> list[int]:
    return [int(x) for x in (value or "").replace(";", ",").split(",") if x.strip()]


@dataclass
class Config:
    bot_token: str
    admin_ids: list[int]
    db_path: str
    tz: ZoneInfo
    morning_time: time      # напоминания + черновики для HR
    post_time: time         # публикация поздравлений сотрудникам в группы
    escalate_time: time     # эскалация неотмеченных партнёров руководителю
    digest_weekday: int     # 0 = понедельник
    anthropic_key: str
    anthropic_model: str
    anthropic_proxy: str | None
    invite_ttl_days: int = 7
    extra: dict = field(default_factory=dict)


def load_config() -> Config:
    return Config(
        bot_token=os.environ.get("BOT_TOKEN", ""),
        admin_ids=_ids(os.environ.get("ADMIN_IDS", "")),
        db_path=os.environ.get("DB_PATH", "data/birthdays.db"),
        tz=ZoneInfo(os.environ.get("TZ_NAME", "Asia/Yekaterinburg")),
        morning_time=_time(os.environ.get("MORNING_TIME", ""), "09:00"),
        post_time=_time(os.environ.get("POST_TIME", ""), "10:00"),
        escalate_time=_time(os.environ.get("ESCALATE_TIME", ""), "18:00"),
        digest_weekday=int(os.environ.get("DIGEST_WEEKDAY", "0")),
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        anthropic_proxy=os.environ.get("ANTHROPIC_PROXY") or None,
        invite_ttl_days=int(os.environ.get("INVITE_TTL_DAYS", "7")),
    )
