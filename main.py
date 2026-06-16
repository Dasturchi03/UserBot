import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from parser_app.admin_bot import AdminPanel
from parser_app.config import load_config
from parser_app.db import Database
from parser_app.userbot import UserbotParser


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(config.db_path)
    await db.init()

    parser = UserbotParser(config, db)
    await parser.start()

    scheduler = AsyncIOScheduler(timezone=config.timezone)
    hour, minute = config.auto_parse_time
    scheduler.add_job(parser.parse_all_donors, "cron", hour=hour, minute=minute)
    scheduler.start()

    admin = AdminPanel(config, db, parser)
    try:
        await admin.run()
    finally:
        scheduler.shutdown(wait=False)
        await parser.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
