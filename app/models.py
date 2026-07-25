"""Почему: храним состояние бота для модерации, игр и сервисных задач."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Strike(Base):
    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
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
    # Дефолт держим синхронным с coins.DEFAULT_COINS (единая константа).
    coins: Mapped[int] = mapped_column(Integer, default=200)
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


class GameRound(Base):
    """Почему: аудит экономики блэкджека — каждая развязка партии хранится навсегда
    (переживает /reset_stats и рестарты)."""

    __tablename__ = "game_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    bet: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(16))  # win | blackjack | lose | push
    payout: Mapped[int] = mapped_column(Integer)  # сколько вернулось на баланс (0 при lose)
    player_hand: Mapped[str] = mapped_column(Text)  # «К♥ 7♠ 5♦»
    dealer_hand: Mapped[str] = mapped_column(Text)
    # Кто закрыл партию: player | timeout | midnight | admin
    closed_by: Mapped[str] = mapped_column(String(16), default="player")
    finished_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class QuizQuestion(Base):
    """Пул вопросов викторины (грузится из data/quiz_questions.json)."""

    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    # Пояснение после развязки («Кинотеатры. 5 центов стоил билет…» → хвост).
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Помечаем заданные — вопрос НИКОГДА не повторяется; когда свежие кончились,
    # викторина закрывается с уведомлением владельцу (без recycle).
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class QuizSession(Base):
    """Активная сессия викторины одной темы. Всё состояние тура — в state_json
    (dataclass QuizState), поэтому переживает рестарт бота посреди игры."""

    __tablename__ = "quiz_sessions"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_json: Mapped[str] = mapped_column(Text)


class QuizRound(Base):
    """История туров викторины — вечный аудит и all-time лидерборд
    (переживает /reset_stats, как GameRound)."""

    __tablename__ = "quiz_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    correct_answers: Mapped[int] = mapped_column(Integer, default=0)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    coins_awarded: Mapped[int] = mapped_column(Integer, default=0)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


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
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class GameCommandMessage(Base):
    __tablename__ = "game_command_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class MessageLog(Base):
    __tablename__ = "message_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, default=0)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class ModerationEvent(Base):
    __tablename__ = "moderation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(20), index=True)
    severity: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class RagMessage(Base):
    __tablename__ = "rag_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    message_text: Mapped[str] = mapped_column(Text)
    added_by_user_id: Mapped[int] = mapped_column(Integer)
    source_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    rag_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    rag_semantic_key: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )
    rag_canonical_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class ChatHistory(Base):
    """Почему: персистентная история диалогов с ИИ — бот помнит контекст после рестарта."""
    __tablename__ = "chat_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(20))  # user / assistant / summary
    text: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class AiFeedback(Base):
    """Почему: обратная связь от пользователей позволяет улучшать качество ответов."""
    __tablename__ = "ai_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    bot_message_id: Mapped[int] = mapped_column(Integer)
    prompt_text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str] = mapped_column(Text)
    rating: Mapped[int] = mapped_column(Integer)  # +1 / -1
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class FrequentQuestion(Base):
    """Почему: трекинг частых вопросов ускоряет ответы и экономит токены."""
    __tablename__ = "frequent_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    question_key: Mapped[str] = mapped_column(String(500), index=True)
    best_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    ask_count: Mapped[int] = mapped_column(Integer, default=1)
    positive_ratings: Mapped[int] = mapped_column(Integer, default=0)
    negative_ratings: Mapped[int] = mapped_column(Integer, default=0)
    last_asked_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class AiUsage(Base):
    __tablename__ = "ai_usage"

    date_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ModerationTraining(Base):
    """Почему: тихое обучение — сбор обратной связи от участников лог-чата."""
    __tablename__ = "moderation_training"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    message_text: Mapped[str] = mapped_column(Text)
    ai_severity: Mapped[int] = mapped_column(Integer, default=0)
    ai_violation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vote_yes: Mapped[int] = mapped_column(Integer, default=0)
    vote_no: Mapped[int] = mapped_column(Integer, default=0)
    voted_user_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class ResidentProfile(Base):
    """Почему: бот запоминает факты о жителях для персонализированных ответов."""
    __tablename__ = "resident_profiles"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_json: Mapped[str] = mapped_column(Text, default="{}")
    # Когда боту последний раз отправили еженедельный персональный нажъм. NULL —
    # ни разу не отправляли. Используется для rate-limit выборки кандидатов.
    last_nudge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )


class Place(Base):
    """Почему: справочник инфраструктуры нужен для быстрых ответов бота без внешних API."""

    __tablename__ = "places"
    __table_args__ = (
        UniqueConstraint("name", "address", "category", name="uq_places_name_address_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    category: Mapped[str] = mapped_column(String(120), index=True)
    subcategory: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(120), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    work_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Паспорт достоверности: когда и кем данные проверялись в последний раз
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class BotImprovement(Base):
    """Почему: жители тратят монеты на реальные доработки бота через коллективное голосование."""
    __tablename__ = "bot_improvements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    author_id: Mapped[int] = mapped_column(Integer, index=True)
    author_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    coins_total: Mapped[int] = mapped_column(Integer, default=0)
    # Порог монет для принятия доработки в работу
    threshold: Mapped[int] = mapped_column(Integer, default=500)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Когда истекает срок голосования (1 неделя с момента создания)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class ImprovementVote(Base):
    """Почему: трекинг голосов за доработки бота (один голос на пользователя)."""
    __tablename__ = "improvement_votes"
    __table_args__ = (
        UniqueConstraint("improvement_id", "user_id", name="uq_improvement_vote_per_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    improvement_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    user_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ShopPurchase(Base):
    """Почему: история покупок в магазине монет."""
    __tablename__ = "shop_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    user_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_key: Mapped[str] = mapped_column(Text)
    coins_spent: Mapped[int] = mapped_column(Integer)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AiTaskLog(Base):
    """Детальный лог каждого AI-запроса: задача, модель, стоимость, результат."""

    __tablename__ = "ai_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
    date_key: Mapped[str] = mapped_column(String(10), index=True)  # "2026-04-25"
    task: Mapped[str] = mapped_column(String(40))                   # "moderation", "reply", ...
    model: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    output_chars: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UnansweredQuestion(Base):
    """Вопросы, на которые бот честно ответил «не знаю» — сырьё для пополнения базы.

    Петля роста: гейт точности логирует вопрос → еженедельный дайджест в админ-чат →
    админ отвечает реплаем → ответ уходит в RAG, статус становится answered.
    """

    __tablename__ = "unanswered_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(Text)
    # Нормализованный ключ для группировки повторов одного и того же вопроса
    norm_key: Mapped[str] = mapped_column(String(200), index=True)
    hits: Mapped[int] = mapped_column(Integer, default=1)
    # open | answered | dismissed
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # message_id сообщений дайджеста в админ-чате (через запятую): реплай на
    # любое из них — ответ на этот вопрос. Персистентно: раньше связка жила
    # в памяти и рестарт между дайджестом и ответом админа рвал петлю роста.
    digest_message_ids: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_asked_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
