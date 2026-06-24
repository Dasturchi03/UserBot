import asyncio
import logging
import os
import sys

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from parser_app.admin_bot import AdminPanel
from parser_app.config import ADMIN_IDS, ProxyConfigError, load_config
from parser_app.db import Database
from parser_app.userbot import ProxyUnavailableError, UserbotParser

log = logging.getLogger(__name__)


async def main() -> None:
    try:
        config = load_config()
    except ProxyConfigError as error:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        _report_proxy_error('Прокси обязателен, но он не настроен корректно.', error)
        await _notify_admin_from_env(error)
        raise SystemExit(1) from error

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(config.db_path)
    await db.init()

    parser = UserbotParser(config, db)
    admin = AdminPanel(config, db, parser)
    try:
        await parser.start()
    except ProxyUnavailableError as error:
        try:
            _report_proxy_error('Юзербот не запущен из-за ошибки прокси.', error)
            await admin.notify_proxy_error(error)
        finally:
            await admin.close()
            await db.close()
        raise SystemExit(1) from error

    scheduler = AsyncIOScheduler(timezone=config.timezone)
    hour, minute = config.auto_parse_time
    scheduler.add_job(admin.run_scheduled_parse, "cron", hour=hour, minute=minute)
    scheduler.start()

    try:
        await admin.run()
    finally:
        scheduler.shutdown(wait=False)
        await parser.stop()
        await admin.close()
        await db.close()


async def _notify_admin_from_env(error: BaseException) -> None:
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or not ADMIN_IDS:
        return
    bot = Bot(token)
    try:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    'Прокси обязателен, но он не настроен корректно.\n'
                    'Юзербот не запущен. Проверьте PROXY/Прокси.txt и перезапустите проект.\n\n'
                    f'Ошибка: {error}',
                )
            except Exception:
                log.exception('Не удалось отправить сообщение админу %s', admin_id)
    finally:
        await bot.session.close()


def _report_proxy_error(message: str, error: BaseException) -> None:
    full_message = f'{message} Ошибка: {error}'
    log.error(full_message)
    print(full_message, file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
