from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from parser_app.config import Config
from parser_app.db import Database
from parser_app.exporter import export_users
from parser_app.userbot import UserbotParser


class AdminState(StatesGroup):
    waiting_depth = State()
    waiting_donor = State()
    waiting_remove = State()


class AdminPanel:
    def __init__(self, config: Config, db: Database, parser: UserbotParser) -> None:
        self.config = config
        self.db = db
        self.parser = parser
        self.bot = Bot(config.bot_token)
        self.dp = Dispatcher()
        self.router = Router()
        self._register()
        self.dp.include_router(self.router)

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)

    def _register(self) -> None:
        self.router.message.filter(F.from_user.id == self.config.admin_id)
        self.router.callback_query.filter(F.from_user.id == self.config.admin_id)
        self.router.message(CommandStart())(self.start)
        self.router.message(Command('menu'))(self.start)
        self.router.callback_query(F.data == 'menu')(self.menu_callback)
        self.router.callback_query(F.data == 'depth')(self.depth_menu)
        self.router.callback_query(F.data.startswith('depth:'))(self.set_depth)
        self.router.callback_query(F.data == 'depth_custom')(self.ask_depth)
        self.router.message(AdminState.waiting_depth)(self.save_custom_depth)
        self.router.callback_query(F.data == 'donors')(self.show_donors)
        self.router.callback_query(F.data == 'donor_add')(self.ask_donor)
        self.router.message(AdminState.waiting_donor)(self.add_donor)
        self.router.callback_query(F.data == 'donor_remove')(self.ask_remove_donor)
        self.router.message(AdminState.waiting_remove)(self.remove_donor)
        self.router.callback_query(F.data == 'donor_import')(self.import_groups)
        self.router.callback_query(F.data == 'parse_now')(self.parse_now)
        self.router.callback_query(F.data == 'export_new')(self.export_new)
        self.router.callback_query(F.data == 'export_all')(self.export_all)
        self.router.callback_query(F.data == 'status')(self.status)

    async def start(self, message: Message) -> None:
        await message.answer(await self._dashboard_text(), reply_markup=await self._main_keyboard())

    async def menu_callback(self, callback: CallbackQuery) -> None:
        await callback.message.edit_text(await self._dashboard_text(), reply_markup=await self._main_keyboard())
        await callback.answer()

    async def depth_menu(self, callback: CallbackQuery) -> None:
        current = await self.db.get_depth_days()
        kb = InlineKeyboardBuilder()
        for days in (1, 3, 7, 14, 30):
            label = f'{days} дн.'
            if days == current:
                label = f'* {label}'
            kb.button(text=label, callback_data=f'depth:{days}')
        kb.button(text='Ввести вручную', callback_data='depth_custom')
        kb.button(text='Назад', callback_data='menu')
        kb.adjust(3, 2, 1, 1)
        await callback.message.edit_text(f'Текущая глубина: {current} дн.', reply_markup=kb.as_markup())
        await callback.answer()

    async def set_depth(self, callback: CallbackQuery) -> None:
        days = int(callback.data.split(':', 1)[1])
        await self.db.set_depth_days(days)
        await callback.answer(f'Глубина установлена: {days} дн.')
        await self.depth_menu(callback)

    async def ask_depth(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminState.waiting_depth)
        await callback.message.answer('За сколько дней проверять историю? Например: 10')
        await callback.answer()

    async def save_custom_depth(self, message: Message, state: FSMContext) -> None:
        try:
            days = int(message.text.strip())
            if days < 1:
                raise ValueError
        except ValueError:
            await message.answer('Отправьте положительное целое число.')
            return
        await self.db.set_depth_days(days)
        await state.clear()
        await message.answer(f'Глубина установлена: {days} дн.', reply_markup=await self._main_keyboard())

    async def show_donors(self, callback: CallbackQuery) -> None:
        donors = await self.db.get_all_donors()
        if not donors:
            text = 'Список доноров пуст.'
        else:
            lines = ['Доноры:']
            for row in donors[:50]:
                title = row['title'] or row['username'] or row['chat_id']
                status = self._format_donor_status(row['status'])
                chat_id = row['chat_id']
                lines.append(f'{chat_id} | {status} | {title}')
            text = '\n'.join(lines)
        kb = InlineKeyboardBuilder()
        kb.button(text='Добавить донора', callback_data='donor_add')
        kb.button(text='Удалить донора', callback_data='donor_remove')
        kb.button(text='Импорт групп аккаунта', callback_data='donor_import')
        kb.button(text='Назад', callback_data='menu')
        kb.adjust(1)
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await callback.answer()

    async def ask_donor(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminState.waiting_donor)
        await callback.message.answer('Отправьте ссылку на донора: @username, t.me/... или t.me/+...')
        await callback.answer()

    async def add_donor(self, message: Message, state: FSMContext) -> None:
        try:
            result = await self.parser.add_donor_by_link(message.text.strip())
        except Exception as e:
            await message.answer(f'Донор не добавлен: {e}')
            return
        await state.clear()
        await message.answer(result, reply_markup=await self._main_keyboard())

    async def ask_remove_donor(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminState.waiting_remove)
        await callback.message.answer('Отправьте chat_id донора, которого нужно удалить.')
        await callback.answer()

    async def remove_donor(self, message: Message, state: FSMContext) -> None:
        try:
            chat_id = int(message.text.strip())
        except ValueError:
            await message.answer('Некорректный chat_id. Отправьте числовой chat_id еще раз.')
            return

        donor = await self.db.fetchone('SELECT chat_id FROM donors WHERE chat_id = ?', (chat_id,))
        if not donor:
            await message.answer('Донор с таким chat_id не найден. Отправьте chat_id еще раз.')
            return

        await self.db.remove_donor(chat_id)
        await state.clear()
        await message.answer('Донор удален.', reply_markup=await self._main_keyboard())

    async def import_groups(self, callback: CallbackQuery) -> None:
        await callback.answer('Импорт запущен.')
        count = await self.parser.import_account_groups()
        await callback.message.answer(f'Из диалогов аккаунта импортировано групп-доноров: {count}.')

    async def parse_now(self, callback: CallbackQuery) -> None:
        await callback.answer('Парсинг запущен в фоне.')
        asyncio.create_task(self._run_parse_and_report(callback.message.chat.id))

    async def _run_parse_and_report(self, chat_id: int) -> None:
        result = await self.parser.parse_all_donors()
        await self.bot.send_message(chat_id, f'Парсинг завершен: {result}')

    async def export_new(self, callback: CallbackQuery) -> None:
        await self._send_export(callback, only_new=True)

    async def export_all(self, callback: CallbackQuery) -> None:
        await self._send_export(callback, only_new=False)

    async def _send_export(self, callback: CallbackQuery, only_new: bool) -> None:
        await callback.answer('Файл готовится.')
        path = await export_users(self.db, self.config.export_dir, only_new=only_new)
        await callback.message.answer_document(FSInputFile(Path(path)))

    async def status(self, callback: CallbackQuery) -> None:
        try:
            await callback.message.edit_text(await self._dashboard_text(), reply_markup=await self._main_keyboard())
        except TelegramBadRequest as error:
            if 'message is not modified' not in str(error).lower():
                raise
        finally:
            await callback.answer()

    async def _dashboard_text(self) -> str:
        depth = await self.db.get_depth_days()
        donors = len(await self.db.get_all_donors())
        total = await self.db.count_users()
        new = await self.db.count_users('New')
        return (
            'Админ-панель юзербота-парсера\n\n'
            f'Глубина: {depth} дн.\n'
            f'Доноры: {donors}\n'
            f'Пользователи: всего {total}, новых {new}\n'
            f'Статус: {self.parser.last_status}'
        )

    async def _main_keyboard(self):
        kb = InlineKeyboardBuilder()
        kb.button(text='Глубина', callback_data='depth')
        kb.button(text='Доноры', callback_data='donors')
        kb.button(text='Запустить парсинг', callback_data='parse_now')
        kb.button(text='Выгрузить новых', callback_data='export_new')
        kb.button(text='Выгрузить всех', callback_data='export_all')
        kb.button(text='Статус', callback_data='status')
        kb.adjust(2, 1, 1, 1, 1)
        return kb.as_markup()

    @staticmethod
    def _format_donor_status(status: str) -> str:
        return {
            'active': 'активен',
            'pending_approval': 'ожидает одобрения',
        }.get(status, status)
