"""Environment-only configuration. No third-party packages."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import re


def load_env(path: str = '.env') -> None:
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key.isidentifier():
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_int(key: str, default: int, low: int, high: int) -> int:
    try:
        val = int(os.getenv(key, default))
    except ValueError as exc:
        raise ValueError(f'{key}: требуется целое число') from exc
    if not low <= val <= high:
        raise ValueError(f'{key}: допустимо {low}–{high}')
    return val


@dataclass(frozen=True)
class Config:
    token: str
    db_path: str = 'data/minepulse.db'
    admins: frozenset[int] = frozenset()
    check_interval: int = 180
    max_servers_per_user: int = 5
    promo_stars: int = 50
    promo_days: int = 7
    concurrency: int = 12

    @classmethod
    def from_env(cls) -> 'Config':
        load_env()
        token = os.getenv('BOT_TOKEN', '').strip()
        if not re.fullmatch(r'[0-9]{6,}:[A-Za-z0-9_-]{30,}', token) or 'INSERT' in token:
            raise ValueError('Укажи действительный BOT_TOKEN в .env (получить у @BotFather)')
        ids = os.getenv('ADMIN_IDS', '').strip()
        try:
            admins = frozenset(int(x.strip()) for x in ids.split(',') if x.strip())
        except ValueError as exc:
            raise ValueError('ADMIN_IDS: только числовые Telegram ID через запятую') from exc
        return cls(
            token=token,
            db_path=os.getenv('DATABASE_PATH', 'data/minepulse.db'),
            admins=admins,
            check_interval=get_int('CHECK_INTERVAL', 180, 60, 86400),
            max_servers_per_user=get_int('MAX_SERVERS_PER_USER', 5, 1, 50),
            promo_stars=get_int('PROMO_STARS', 50, 1, 100000),
            promo_days=get_int('PROMO_DAYS', 7, 1, 365),
            concurrency=get_int('CHECK_CONCURRENCY', 12, 1, 50),
        )
