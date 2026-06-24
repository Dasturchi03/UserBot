from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv


ADMIN_IDS = [
    1030165038,
    290757505
]


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    api_id: int
    api_hash: str
    session_name: str
    proxy: dict[str, Any]
    db_path: Path
    export_dir: Path
    log_level: str
    auto_parse_time: tuple[int, int]
    request_delay: tuple[int, int]
    chat_delay: tuple[int, int]
    join_delay_seconds: int
    timezone: str


class ProxyConfigError(RuntimeError):
    pass


def load_config() -> Config:
    load_dotenv()

    account_dir = Path(os.getenv('ACCOUNT_DIR', 'Данные аккаунта Казахстан'))
    account_json = _find_account_json(account_dir)
    account_data = _read_json(account_json) if account_json else {}

    api_id = _int_env('API_ID') or _first_int(account_data, 'api_id', 'app_id')
    api_hash = os.getenv('API_HASH') or _first_str(account_data, 'api_hash', 'app_hash')
    if not api_id or not api_hash:
        raise RuntimeError('API_ID/API_HASH не найдены. Укажите их в .env или в JSON аккаунта.')

    bot_token = os.getenv('BOT_TOKEN', '').strip()
    if not bot_token:
        raise RuntimeError('BOT_TOKEN должен быть указан в .env.')
    if not ADMIN_IDS:
        raise RuntimeError('ADMIN_IDS должен содержать хотя бы один Telegram ID администратора.')

    session_name = os.getenv('SESSION_NAME', '').strip()
    if not session_name:
        session_path = _find_session_file(account_dir)
        session_name = str(session_path.with_suffix('')) if session_path else 'userbot'

    return Config(
        bot_token=bot_token,
        admin_ids=tuple(ADMIN_IDS),
        api_id=int(api_id),
        api_hash=str(api_hash),
        session_name=session_name,
        proxy=_load_proxy(),
        db_path=Path(os.getenv('DB_PATH', 'data/userbot_parser.sqlite3')),
        export_dir=Path(os.getenv('EXPORT_DIR', 'exports')),
        log_level=os.getenv('LOG_LEVEL', 'INFO'),
        auto_parse_time=_parse_time(os.getenv('AUTO_PARSE_TIME', '03:00')),
        request_delay=(_int_env('REQUEST_DELAY_MIN', 2), _int_env('REQUEST_DELAY_MAX', 5)),
        chat_delay=(_int_env('CHAT_DELAY_MIN', 300), _int_env('CHAT_DELAY_MAX', 600)),
        join_delay_seconds=_int_env('JOIN_DELAY_SECONDS', 900),
        timezone=os.getenv('TIMEZONE', 'Asia/Tashkent'),
    )


def _find_account_json(account_dir: Path) -> Path | None:
    explicit = os.getenv('ACCOUNT_JSON', '').strip()
    if explicit:
        return Path(explicit)
    if account_dir.exists():
        return next(account_dir.glob('*.json'), None)
    return None


def _find_session_file(account_dir: Path) -> Path | None:
    if account_dir.exists():
        return next(account_dir.glob('*.session'), None)
    return None


def _read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return None


def _first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if value:
            return int(value)
    return None


def _int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == '':
        return default
    return int(value)


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.strip().split(':', 1)
    return int(hour), int(minute)


def _load_proxy() -> dict[str, Any]:
    raw = os.getenv('PROXY', '').strip()
    proxy_file = Path(os.getenv('PROXY_FILE', 'Прокси.txt'))
    if not raw and proxy_file.exists():
        raw = proxy_file.read_text(encoding='utf-8').strip()
    if not raw:
        raise ProxyConfigError('Прокси обязателен. Укажите PROXY в .env или заполните Прокси.txt.')
    return _parse_proxy(raw)


def _parse_proxy(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if '://' not in raw and '@' in raw:
        return _parse_proxy(f'http://{raw}')
    if '://' in raw:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            raise ProxyConfigError('Неверный формат прокси. Используйте http://user:pass@host:port или host:port:user:pass.')
        port = _parse_url_proxy_port(parsed)
        return {
            'scheme': parsed.scheme,
            'hostname': parsed.hostname,
            'port': port,
            'username': parsed.username,
            'password': parsed.password,
        }

    parts = raw.split(':')
    if len(parts) == 2:
        host, port = parts
        if not host:
            raise ProxyConfigError('Неверный формат прокси. Используйте host:port или host:port:user:pass.')
        return {'scheme': 'http', 'hostname': host, 'port': _parse_proxy_port(port)}
    if len(parts) == 4:
        host, port, username, password = parts
        if not host:
            raise ProxyConfigError('Неверный формат прокси. Используйте host:port:user:pass.')
        return {
            'scheme': 'http',
            'hostname': host,
            'port': _parse_proxy_port(port),
            'username': username,
            'password': password,
        }
    raise ProxyConfigError('Неверный формат прокси. Используйте http://user:pass@host:port или host:port:user:pass.')


def _parse_proxy_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ProxyConfigError('Порт прокси должен быть числом.') from error
    if port <= 0:
        raise ProxyConfigError('Порт прокси должен быть положительным числом.')
    return port


def _parse_url_proxy_port(parsed: Any) -> int:
    try:
        port = parsed.port
    except ValueError as error:
        raise ProxyConfigError('Порт прокси должен быть числом.') from error
    return _parse_proxy_port(str(port) if port else '')
