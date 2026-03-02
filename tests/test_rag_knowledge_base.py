import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services.rag import add_rag_message, get_all_rag_messages, rank_rag_messages


async def _prepare_messages() -> list[str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await add_rag_message(
            session,
            chat_id=100,
            message_text="Шлагбаум открывается через приложение УК.",
            added_by_user_id=1,
        )
        await add_rag_message(
            session,
            chat_id=100,
            message_text="Для заявки по лифту пишите в топик Жалобы.",
            added_by_user_id=1,
        )
        await session.commit()

        all_messages = await get_all_rag_messages(session, 100)
        ranked = rank_rag_messages(all_messages, query="Как открыть шлагбаум?")

    await engine.dispose()
    return [msg.message_text for msg in ranked]


def test_rag_message_is_available_immediately_after_save() -> None:
    messages = asyncio.run(_prepare_messages())
    assert messages[0] == "Шлагбаум открывается через приложение УК."


def test_rag_ranking_keeps_all_knowledge_base_records() -> None:
    messages = asyncio.run(_prepare_messages())
    assert len(messages) == 2
    assert "Для заявки по лифту пишите в топик Жалобы." in messages
