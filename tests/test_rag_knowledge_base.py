import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services.rag import (
    add_rag_message,
    build_rag_context,
    get_all_rag_messages,
    rank_rag_messages,
    systematize_rag,
)


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


async def _prepare_grouped_context() -> str:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await add_rag_message(
            session,
            chat_id=101,
            message_text="Шлагбаум открывается через приложение УК.",
            added_by_user_id=1,
        )
        await add_rag_message(
            session,
            chat_id=101,
            message_text="Шлагбаум можно открыть через приложение УК и номер квартиры.",
            added_by_user_id=2,
        )
        await add_rag_message(
            session,
            chat_id=101,
            message_text="Если лифт застрял, оставьте заявку в теме Жалобы.",
            added_by_user_id=3,
        )

        await systematize_rag(session, 101)
        await session.commit()

        context = await build_rag_context(
            session,
            chat_id=101,
            query="Как открыть шлагбаум?",
            top_k=5,
        )

    await engine.dispose()
    return context


def test_rag_message_is_available_immediately_after_save() -> None:
    messages = asyncio.run(_prepare_messages())
    assert messages[0] == "Шлагбаум открывается через приложение УК."


def test_rag_ranking_keeps_all_knowledge_base_records() -> None:
    messages = asyncio.run(_prepare_messages())
    assert len(messages) == 2
    assert "Для заявки по лифту пишите в топик Жалобы." in messages


def test_rag_systematization_merges_similar_messages_in_context() -> None:
    context = asyncio.run(_prepare_grouped_context())
    assert "(парковка)" in context
    assert "Шлагбаум открывается через приложение УК." in context
    assert "Шлагбаум можно открыть через приложение УК и номер квартиры." in context
