from __future__ import annotations

import asyncio
import logging
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, InviteRequestSent

from parser_app.config import Config
from parser_app.db import Database

log = logging.getLogger(__name__)


GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ADMIN_STATUSES = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


class ProxyUnavailableError(RuntimeError):
    pass


class UserbotSessionError(RuntimeError):
    pass


class UserbotParser:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.client = self._create_client()
        self._admin_cache: dict[tuple[int, int], bool] = {}
        self._parse_lock = asyncio.Lock()
        self._started = False
        self.last_status = 'Ожидание'

    def _create_client(self) -> Client:
        return Client(
            name=self.config.session_name,
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            proxy=self.config.proxy,
            workdir='.',
        )

    async def start(self) -> None:
        try:
            await self.client.start()
            me = await self.client.get_me()
        except Exception as error:
            if _looks_like_proxy_error(error):
                raise self._proxy_error(error) from error
            if _looks_like_session_error(error):
                raise self._session_error(error) from error
            raise
        self._started = True
        self.last_status = f'Юзербот подключен как {me.id}'
        log.info(self.last_status)

    async def stop(self) -> None:
        try:
            await self.client.stop()
        except Exception:
            log.debug('Юзербот уже остановлен или не был подключен.', exc_info=True)
        finally:
            self._started = False

    async def replace_session(self, uploaded_session_path: Path) -> str:
        if self._parse_lock.locked():
            raise RuntimeError('Сейчас идет парсинг. Дождитесь завершения и загрузите session еще раз.')

        async with self._parse_lock:
            await self.stop()
            session_path = _session_file_path(self.config.session_name)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            _remove_session_files(session_path)
            shutil.move(str(uploaded_session_path), session_path)
            self.client = self._create_client()
            self._admin_cache.clear()
            await self.start()
            return str(session_path)

    async def import_account_groups(self) -> int:
        self._ensure_started()
        count = 0
        try:
            async for dialog in self.client.get_dialogs():
                chat = dialog.chat
                if chat.type in GROUP_TYPES:
                    await self.db.upsert_donor(
                        chat_id=chat.id,
                        title=chat.title,
                        username=chat.username,
                        status='active',
                    )
                    count += 1
        except Exception as error:
            if _looks_like_proxy_error(error):
                raise self._proxy_error(error) from error
            if _looks_like_session_error(error):
                raise self._session_error(error) from error
            raise
        return count

    async def add_donor_by_link(self, link: str) -> str:
        self._ensure_started()
        target = link.strip()
        if not target:
            raise ValueError('Ссылка на донора пустая.')
        chat_ref = _normalize_chat_ref(target)
        try:
            chat = await self.client.join_chat(chat_ref) if _looks_like_invite(chat_ref) else await self.client.get_chat(chat_ref)
        except InviteRequestSent:
            await self.db.add_pending_donor(target)
            return 'Заявка на вступление отправлена. Донор сохранен в статусе ожидания одобрения.'
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await self.add_donor_by_link(target)
        except Exception as error:
            if _looks_like_proxy_error(error):
                raise self._proxy_error(error) from error
            if _looks_like_session_error(error):
                raise self._session_error(error) from error
            raise

        if chat.type not in GROUP_TYPES:
            raise ValueError('Эта ссылка не относится к группе или супергруппе Telegram.')
        await self.db.upsert_donor(chat.id, chat.title, chat.username, target, 'active')
        if _looks_like_invite(target):
            await asyncio.sleep(self.config.join_delay_seconds)
        return f'Донор добавлен: {chat.title or chat.id}'

    async def parse_all_donors(self) -> dict[str, Any]:
        self._ensure_started()
        if self._parse_lock.locked():
            return {'status': 'busy', 'message': 'Парсинг уже выполняется.'}

        async with self._parse_lock:
            donors = await self.db.get_active_donors()
            depth_days = await self.db.get_depth_days()
            total_new = 0
            parsed_chats = 0
            self.last_status = f'Парсинг запущен: доноров {len(donors)}, глубина {depth_days} дн.'
            cutoff = datetime.now(timezone.utc) - timedelta(days=depth_days)

            for index, donor in enumerate(donors):
                try:
                    result = await self.parse_donor(donor, cutoff, depth_days)
                    total_new += result['saved']
                    parsed_chats += 1
                except ProxyUnavailableError:
                    raise
                except UserbotSessionError:
                    raise
                except Exception:
                    log.exception('Ошибка парсинга донора: %s', donor['chat_id'])
                if index < len(donors) - 1:
                    await asyncio.sleep(random.randint(*self.config.chat_delay))

            self.last_status = f'Готово. Чатов: {parsed_chats}, сохранено/обновлено пользователей: {total_new}'
            return {'status': 'ok', 'chats': parsed_chats, 'saved': total_new}

    async def parse_donor(self, donor: Any, cutoff: datetime, depth_days: int) -> dict[str, int]:
        chat_id = int(donor['chat_id'])
        last_message_id = int(donor['last_message_id'] or 0)
        use_min_id = depth_days <= 1 and last_message_id > 0
        saved = 0
        max_seen_id = last_message_id
        seen_since_delay = 0

        try:
            async for message in self.client.get_chat_history(chat_id=chat_id):
                seen_since_delay += 1
                if seen_since_delay >= 100:
                    await self._sleep_between_requests()
                    seen_since_delay = 0

                if use_min_id and message.id <= last_message_id:
                    break
                if message.id > max_seen_id:
                    max_seen_id = message.id

                message_date = _as_utc(message.date)
                if message_date < cutoff:
                    break

                sender = message.from_user
                if not sender or sender.is_bot:
                    continue
                if await self._is_admin(chat_id, sender.id):
                    continue

                changed = await self.db.upsert_user(
                    user_id=sender.id,
                    username=sender.username,
                    first_name=sender.first_name,
                    last_activity_date=message_date.isoformat(),
                    source_chat_id=chat_id,
                )
                if changed:
                    saved += 1
        except ProxyUnavailableError:
            raise
        except UserbotSessionError:
            raise
        except Exception as error:
            if _looks_like_proxy_error(error):
                raise self._proxy_error(error) from error
            if _looks_like_session_error(error):
                raise self._session_error(error) from error
            raise

        if max_seen_id:
            await self.db.update_donor_progress(chat_id, max_seen_id)
        return {'saved': saved, 'max_seen_id': max_seen_id}

    async def _is_admin(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        if key in self._admin_cache:
            return self._admin_cache[key]
        try:
            member = await self.client.get_chat_member(chat_id, user_id)
            is_admin = member.status in ADMIN_STATUSES
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await self._is_admin(chat_id, user_id)
        except Exception as error:
            if _looks_like_proxy_error(error):
                raise self._proxy_error(error) from error
            if _looks_like_session_error(error):
                raise self._session_error(error) from error
            is_admin = False
        self._admin_cache[key] = is_admin
        return is_admin

    async def _sleep_between_requests(self) -> None:
        await asyncio.sleep(random.randint(*self.config.request_delay))

    def _proxy_error(self, error: Exception) -> ProxyUnavailableError:
        self.last_status = f'Прокси не работает: {error}'
        log.exception('Прокси недоступен или не дает подключиться к Telegram.')
        return ProxyUnavailableError(self.last_status)

    def _session_error(self, error: Exception) -> UserbotSessionError:
        self._started = False
        self.last_status = f'Session недействителен: {error}'
        log.exception('Session файл недействителен или был отозван Telegram.')
        return UserbotSessionError(self.last_status)

    def _ensure_started(self) -> None:
        if not self._started:
            self.last_status = 'Юзербот не подключен. Нужно загрузить новый .session файл.'
            raise UserbotSessionError(self.last_status)


def _looks_like_invite(value: str) -> bool:
    return 't.me/+' in value or 'joinchat/' in value


def _normalize_chat_ref(value: str) -> str:
    target = value.strip()
    if not target:
        return target
    if _looks_like_invite(target):
        return target
    if target.startswith('@'):
        return target
    if '://' not in target and target.startswith('t.me/'):
        target = f'https://{target}'
    parsed = urlparse(target)
    if parsed.netloc in {'t.me', 'telegram.me'}:
        username = parsed.path.strip('/').split('/', 1)[0]
        if username:
            return f'@{username}'
    return target


def _session_file_path(session_name: str) -> Path:
    path = Path(session_name)
    if path.suffix == '.session':
        return path
    return path.with_suffix('.session')


def _remove_session_files(session_path: Path) -> None:
    for path in (session_path, Path(f'{session_path}-journal')):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _looks_like_proxy_error(error: BaseException) -> bool:
    proxy_markers = (
        'proxy',
        'socks',
        'connection',
        'connect',
        'network',
        'socket',
        'timeout',
        'timed out',
        'connection refused',
        'host unreachable',
        'no route',
    )
    current: BaseException | None = error
    while current:
        if isinstance(current, (OSError, ConnectionError, TimeoutError, EOFError)):
            return True
        text = f'{current.__class__.__name__}: {current}'.lower()
        if any(marker in text for marker in proxy_markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def _looks_like_session_error(error: BaseException) -> bool:
    session_markers = (
        'session_revoked',
        'session expired',
        'session password needed',
        'auth_key_unregistered',
        'auth_key_duplicated',
        'user_deactivated',
        'unauthorized',
        '401',
    )
    current: BaseException | None = error
    while current:
        text = f'{current.__class__.__name__}: {current}'.lower()
        if any(marker in text for marker in session_markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
