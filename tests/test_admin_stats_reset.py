import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import RagMessage, UserStat
from app.services.admin_stats_reset import reset_runtime_statistics


async def _run_reset_and_check_rag() -> tuple[int, int]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(UserStat(chat_id=1, user_id=10, coins=120, games_played=5, wins=2))
        session.add(
            RagMessage(
                chat_id=1,
                message_text="Шлагбаум открывается из приложения.",
                added_by_user_id=1,
            )
        )
        await session.commit()

        deleted = await reset_runtime_statistics(session)
        await session.commit()

        rag_count = len((await session.execute(RagMessage.__table__.select())).all())

    await engine.dispose()
    return deleted["user_stats"], rag_count


def test_reset_runtime_statistics_does_not_touch_rag() -> None:
    deleted_user_stats, rag_count = asyncio.run(_run_reset_and_check_rag())
    assert deleted_user_stats == 1
    assert rag_count == 1
