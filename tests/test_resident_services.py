import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.handlers.admin import usluga_command
from app.models import ResidentService
from app.services.resident_services import add_service, deactivate_service


class _DummyUser:
    def __init__(self, user_id: int, full_name: str = "Житель") -> None:
        self.id = user_id
        self.full_name = full_name


class _DummyReplyMessage:
    def __init__(self, text: str, message_id: int = 77) -> None:
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.from_user = _DummyUser(200, "Иван Житель")


class _DummyMessage:
    def __init__(self) -> None:
        self.from_user = _DummyUser(100, "Админ")
        self.reply_to_message = _DummyReplyMessage("Делаю маникюр и педикюр на дому")
        self.message_thread_id = 3240
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class _DummyBot:
    pass


async def _run_add_service_with_ai_fallback() -> tuple[str, str, str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        service = await add_service(
            session,
            chat_id=1,
            message_text="Делаю маникюр и педикюр на дому",
            provider_user_id=200,
            provider_name="Иван Житель",
            source_message_id=77,
            added_by_user_id=100,
            ai_description="  ",
            ai_keywords="Маникюр, маникюр, красота, и, ногти ",
            ai_category="Несуществующая категория",
        )
        await session.commit()

    await engine.dispose()
    return service.description, service.keywords, service.category


async def _run_reactivate_existing_service() -> tuple[int, int, bool, str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        first = await add_service(
            session,
            chat_id=1,
            message_text="Ремонт стиральных машин",
            provider_user_id=200,
            provider_name="Иван Житель",
            source_message_id=88,
            added_by_user_id=100,
        )
        await session.commit()

        await deactivate_service(session, first.id)
        await session.commit()

        second = await add_service(
            session,
            chat_id=1,
            message_text="Ремонт стиральных машин и посудомоек",
            provider_user_id=200,
            provider_name="Иван Житель",
            source_message_id=88,
            added_by_user_id=101,
            ai_category="ремонт",
        )
        await session.commit()

        total = len((await session.execute(ResidentService.__table__.select())).all())

    await engine.dispose()
    return first.id, total, second.is_active, second.message_text


async def _fake_existing_session_gen():
    class _DummySession:
        pass

    yield _DummySession()


def test_add_service_normalizes_ai_metadata() -> None:
    description, keywords, category = asyncio.run(_run_add_service_with_ai_fallback())

    assert description == "Делаю маникюр и педикюр на дому"
    assert keywords == "маникюр,красота,ногти"
    assert category == "красота"


def test_usluga_command_reports_duplicate_service(monkeypatch) -> None:
    async def _allow_admin(_message, _bot) -> bool:
        return True

    async def _fake_get_existing(_session, *, chat_id: int, source_message_id: int):
        assert chat_id == 1
        assert source_message_id == 77
        return ResidentService(
            chat_id=1,
            message_text="Делаю маникюр и педикюр на дому",
            description="Маникюр и педикюр на дому",
            keywords="маникюр,педикюр",
            category="красота",
            provider_user_id=200,
            provider_name="Иван Житель",
            source_message_id=77,
            added_by_user_id=100,
            is_active=True,
        )

    async def _should_not_add(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("add_service не должен вызываться для дубля")

    monkeypatch.setattr("app.handlers.admin._ensure_admin", _allow_admin)
    monkeypatch.setattr("app.handlers.admin.get_session", lambda: _fake_existing_session_gen())
    monkeypatch.setattr("app.handlers.admin.get_service_by_source_message_id", _fake_get_existing)
    monkeypatch.setattr("app.handlers.admin.add_service", _should_not_add)

    message = _DummyMessage()
    asyncio.run(usluga_command(message, _DummyBot()))

    assert message.replies == [
        "Эта услуга уже есть в каталоге.\nКатегория: красота\nОписание: Маникюр и педикюр на дому"
    ]


def test_add_service_reactivates_existing_source_message() -> None:
    service_id, total, is_active, message_text = asyncio.run(_run_reactivate_existing_service())

    assert service_id == 1
    assert total == 1
    assert is_active is True
    assert message_text == "Ремонт стиральных машин и посудомоек"
