import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nexafreight.config import get_settings


async def drop_tables():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS events;"))
        await conn.execute(text("DROP TABLE IF EXISTS reroute_options;"))
        print("Dropped tables.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(drop_tables())
