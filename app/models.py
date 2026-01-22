"""Почему: храним состояние бота для модерации, игр и сервисных задач."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Strike(Base):
    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )


class FloodRecord(Base):
    __tablename__ = "flood_records"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_flood_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserStat(Base):
    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coins: Mapped[int] = mapped_column(Integer, default=100)
    games_played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_coin_grant_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    coins_granted_today: Mapped[int] = mapped_column(Integer, default=0)


class GameState(Base):
    __tablename__ = "game_states"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_json: Mapped[str] = mapped_column(Text)


class HealthState(Base):
    __tablename__ = "health_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notice_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TopicStat(Base):
    __tablename__ = "topic_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    topic_id: Mapped[int] = mapped_column(Integer, index=True)
    date_key: Mapped[str] = mapped_column(String(10), index=True)
    messages_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class MigrationFlag(Base):
    __tablename__ = "migration_flags"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)


class QuizSession(Base):
    __tablename__ = "quiz_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    topic_id: Mapped[int] = mapped_column(Integer)
    current_question_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_number: Mapped[int] = mapped_column(Integer, default=1)
    question_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Integer, default=True)


class QuizUserStat(Base):
    __tablename__ = "quiz_user_stats"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class QuizDailyLimit(Base):
    __tablename__ = "quiz_daily_limits"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    launches: Mapped[int] = mapped_column(Integer, default=0)
