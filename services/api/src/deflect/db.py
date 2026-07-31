from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deflect.config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionFactory() as session:
        yield session


# Every route takes a session, so the dependency is declared once here. Annotated is
# also what keeps Depends() out of argument defaults, which ruff's B008 rejects.
SessionDep = Annotated[AsyncSession, Depends(get_session)]
