"""Тесты еженедельных персональных нажъмов в DM."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ResidentProfile
from app.services.personalization import (
    _first_actionable_fact,
    _is_opted_out,
    build_nudge_message,
    select_nudge_candidates,
)


def _facts(**kw) -> dict:
    return kw


def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _setup_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- _first_actionable_fact ---

def test_first_actionable_fact_picks_interests_first():
    facts = _facts(interests=["школа"], pets="собака", car="ниссан")
    assert _first_actionable_fact(facts) == ("interests", "школа")


def test_first_actionable_fact_skips_empty_interests():
    facts = _facts(interests=[], pets="собака")
    assert _first_actionable_fact(facts) == ("pets", "собака")


def test_first_actionable_fact_family_only_with_kid_keywords():
    # семья без упоминания детей — не actionable
    assert _first_actionable_fact(_facts(family="живём вдвоём")) is None
    # семья с ребёнком — годится
    assert _first_actionable_fact(_facts(family="с ребёнком")) == ("family", "с ребёнком")


def test_first_actionable_fact_returns_none_for_empty_profile():
    assert _first_actionable_fact({}) is None
    assert _first_actionable_fact({"name": "Иван", "building": "2"}) is None


# --- _is_opted_out ---

def test_opted_out_blocks_nudge():
    assert _is_opted_out({"nudge_opt_out": True}) is True
    assert _is_opted_out({"nudge_unreachable": True}) is True
    assert _is_opted_out({"interests": ["школа"]}) is False


# --- build_nudge_message ---

def test_build_nudge_message_returns_text_for_actionable_profile():
    text = build_nudge_message(_facts(interests=["школа"]), display_name="Иван Петров")
    assert text is not None
    assert "Иван" in text
    assert "школа" in text
    assert "/off_nudges" in text
    # Не должно протекать второе слово имени (фамилия).
    assert "Петров" not in text


def test_build_nudge_message_returns_none_when_no_facts():
    assert build_nudge_message({}, display_name="Кто-то") is None


def test_build_nudge_message_returns_none_when_opted_out():
    facts = _facts(interests=["школа"], nudge_opt_out=True)
    assert build_nudge_message(facts, display_name="Иван") is None


def test_build_nudge_message_handles_no_display_name():
    text = build_nudge_message(_facts(pets="собака"))
    assert text is not None
    # Без имени фраза остаётся грамматически корректной (без хвоста ", None").
    assert "None" not in text
    assert "собака" in text


# --- select_nudge_candidates ---

@pytest.mark.asyncio
async def test_select_skips_opted_out_and_unactionable():
    engine, factory = _make_session_factory()
    await _setup_db(engine)
    async with factory() as session:
        session.add_all([
            # actionable
            ResidentProfile(
                user_id=1, chat_id=100, display_name="A",
                facts_json=json.dumps({"interests": ["школа"]}),
            ),
            # opted out
            ResidentProfile(
                user_id=2, chat_id=100, display_name="B",
                facts_json=json.dumps({
                    "interests": ["школа"], "nudge_opt_out": True,
                }),
            ),
            # unreachable
            ResidentProfile(
                user_id=3, chat_id=100, display_name="C",
                facts_json=json.dumps({
                    "interests": ["школа"], "nudge_unreachable": True,
                }),
            ),
            # no actionable fact
            ResidentProfile(
                user_id=4, chat_id=100, display_name="D",
                facts_json=json.dumps({"name": "Дмитрий"}),
            ),
        ])
        await session.commit()
        result = await select_nudge_candidates(
            session, chat_id=100, limit=10, min_days_between=30,
        )
    assert [c.user_id for c in result] == [1]


@pytest.mark.asyncio
async def test_select_respects_min_days_between():
    engine, factory = _make_session_factory()
    await _setup_db(engine)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add_all([
            # 10 дней назад — слишком недавно
            ResidentProfile(
                user_id=1, chat_id=100, display_name="A",
                facts_json=json.dumps({"interests": ["школа"]}),
                last_nudge_at=now - timedelta(days=10),
            ),
            # 60 дней назад — годится
            ResidentProfile(
                user_id=2, chat_id=100, display_name="B",
                facts_json=json.dumps({"interests": ["школа"]}),
                last_nudge_at=now - timedelta(days=60),
            ),
            # никогда — годится
            ResidentProfile(
                user_id=3, chat_id=100, display_name="C",
                facts_json=json.dumps({"interests": ["школа"]}),
            ),
        ])
        await session.commit()
        result = await select_nudge_candidates(
            session, chat_id=100, limit=10, min_days_between=30,
        )
    # NULL last_nudge_at идёт первым (round-robin), затем 60-дневный.
    assert [c.user_id for c in result] == [3, 2]


@pytest.mark.asyncio
async def test_select_respects_limit():
    engine, factory = _make_session_factory()
    await _setup_db(engine)
    async with factory() as session:
        for i in range(5):
            session.add(ResidentProfile(
                user_id=i + 1, chat_id=100, display_name=f"U{i}",
                facts_json=json.dumps({"interests": ["школа"]}),
            ))
        await session.commit()
        result = await select_nudge_candidates(
            session, chat_id=100, limit=2, min_days_between=30,
        )
    assert len(result) == 2
