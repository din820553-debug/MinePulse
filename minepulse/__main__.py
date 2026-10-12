"""Run with: python -m minepulse"""
import asyncio
import logging

from .config import Config
from .database import Database
from .service import MinePulse
from .telegram import Telegram


async def main():
    conf = Config.from_env()
    db = Database(conf.db_path)
    app = MinePulse(conf, db, Telegram(conf.token))
    task = asyncio.create_task(app.monitor(), name='minecraft-monitor')
    try:
        await app.poll()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('MinePulse остановлен.')
    except ValueError as error:
        print(f'Ошибка конфигурации: {error}')
        raise SystemExit(2)
