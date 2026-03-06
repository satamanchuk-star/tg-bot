import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services.rag import (
    add_rag_message,
    build_rag_context,
    classify_rag_message,
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


async def _prepare_parking_messages() -> list[str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await add_rag_message(
            session,
            chat_id=102,
            message_text="Парковка оформляется через управляющую компанию по заявлению.",
            added_by_user_id=1,
        )
        await add_rag_message(
            session,
            chat_id=102,
            message_text="По вопросам лифта пишите в тему Жалобы.",
            added_by_user_id=1,
        )
        await session.commit()

        all_messages = await get_all_rag_messages(session, 102)
        ranked = rank_rag_messages(all_messages, query="Как оформить паковку?")

    await engine.dispose()
    return [msg.message_text for msg in ranked]


def test_rag_ranking_handles_word_forms_and_minor_typos() -> None:
    messages = asyncio.run(_prepare_parking_messages())
    assert messages[0] == "Парковка оформляется через управляющую компанию по заявлению."


async def _prepare_mixed_domain_messages() -> list[str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await add_rag_message(
            session,
            chat_id=103,
            message_text="Для оформления пропуска в паркинг обратитесь в диспетчерскую УК.",
            added_by_user_id=1,
        )
        await add_rag_message(
            session,
            chat_id=103,
            message_text="По шуму после 23:00 пишите в тему Жалобы.",
            added_by_user_id=1,
        )
        await session.commit()

        all_messages = await get_all_rag_messages(session, 103)
        ranked = rank_rag_messages(all_messages, query="Как оформить пропуск в паркинг через диспечерскую?")

    await engine.dispose()
    return [msg.message_text for msg in ranked]


def test_rag_ranking_works_for_all_rag_topics_not_only_parking_examples() -> None:
    messages = asyncio.run(_prepare_mixed_domain_messages())
    assert messages[0] == "Для оформления пропуска в паркинг обратитесь в диспетчерскую УК."


async def _prepare_admin_priority_messages() -> list[str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await add_rag_message(
            session,
            chat_id=104,
            message_text="Лифт временно отключат сегодня с 14 до 16.",
            added_by_user_id=1,
            is_admin=True,
        )
        await add_rag_message(
            session,
            chat_id=104,
            message_text="Шлагбаум снова завис, как открыть?",
            added_by_user_id=2,
        )
        await session.commit()

        all_messages = await get_all_rag_messages(session, 104)
        ranked = rank_rag_messages(all_messages, query="Как открыть шлагбаум?")

    await engine.dispose()
    return [msg.message_text for msg in ranked]


def test_rag_admin_messages_have_max_priority() -> None:
    messages = asyncio.run(_prepare_admin_priority_messages())
    assert messages[0] == "Лифт временно отключат сегодня с 14 до 16."


def test_rag_classification_supports_new_categories() -> None:
    assert classify_rag_message("На детской площадке сломалась качеля") == "детская_площадка"
    assert classify_rag_message("Квитанция за ЖКУ пришла с неверным тарифом") == "платежи"
    assert classify_rag_message("Домофон не пускает курьера в подъезд") == "безопасность_и_доступ"
