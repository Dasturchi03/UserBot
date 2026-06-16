from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, InviteRequestSent

from parser_app.config import Config
from parser_app.db import Database

log = logging.getLogger(__name__)


GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ADMIN_STATUSES = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


class UserbotParser:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.client = Client(
            name=config.session_name,
            api_id=config.api_id,
            api_hash=config.api_hash,
            proxy=config.proxy,
            workdir='.',
        )
        self._admin_cache: dict[tuple[int, int], bool] = {}
        self._parse_lock = asyncio.Lock()
        self.last_status = 'Ожидание'

    async def start(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        self.last_status = f'Юзербот подключен как {me.id}'
        log.info(self.last_status)

    async def stop(self) -> None:
        await self.client.stop()

    async def import_account_groups(self) -> int:
        count = 0
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
        return count

    async def add_donor_by_link(self, link: str) -> str:
        target = link.strip()
        if not target:
            raise ValueError('Ссылка на донора пустая.')
        try:
            chat = await self.client.join_chat(target) if _looks_like_invite(target) else await self.client.get_chat(target)
        except InviteRequestSent:
            await self.db.add_pending_donor(target)
            return 'Заявка на вступление отправлена. Донор сохранен в статусе ожидания одобрения.'
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await self.add_donor_by_link(target)

        if chat.type not in GROUP_TYPES:
            raise ValueError('Эта ссылка не относится к группе или супергруппе Telegram.')
        await self.db.upsert_donor(chat.id, chat.title, chat.username, target, 'active')
        if _looks_like_invite(target):
            await asyncio.sleep(self.config.join_delay_seconds)
        return f'Донор добавлен: {chat.title or chat.id}'

    async def parse_all_donors(self) -> dict[str, Any]:
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
        except Exception:
            is_admin = False
        self._admin_cache[key] = is_admin
        return is_admin

    async def _sleep_between_requests(self) -> None:
        await asyncio.sleep(random.randint(*self.config.request_delay))


def _looks_like_invite(value: str) -> bool:
    return 't.me/+' in value or 'joinchat/' in value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
