import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nexafreight.config import get_settings


async def check():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        )
        for r in result.fetchall():
            print(r[0])
    await engine.dispose()


asyncio.run(check())
