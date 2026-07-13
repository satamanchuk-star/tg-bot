"""Почему: логика игры «21» отделена от aiogram-хендлеров — чистые функции
тестируются без Telegram, персистентность (GameState/GameRound) живёт рядом.

Экономика ставок:
- ставка списывается атомарно при раздаче (place_bet_and_deal, один commit);
- выплата, запись в историю и удаление партии — тоже один commit (_settle
  в хендлере), краш не расщепит деньги и состояние;
- games_played инкрементируется при ставке, wins — при развязке (не задваивать!).
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameCommandMessage, GameRound, GameState, UserStat
from app.services.coins import get_or_create_stats
from app.utils.time import ensure_aware

logger = logging.getLogger(__name__)

# --- Константы игры (текст правил в хендлере обязан им соответствовать) ---

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "В", "Д", "К", "Т")
BET_OPTIONS = (5, 10, 25, 50)
MIN_BET = 5
BANKRUPT_TOP_UP = 10
GAME_TIMEOUT_MINUTES = 10
BLACKJACK_MULTIPLIER = 2.5  # выплата за блэкджек, округление вниз
DEALER_STANDS_AT = 17

_STATE_VERSION = 2  # payload без этой версии — legacy/мусор, партия удаляется


# --- Чистая логика (без БД и aiogram) ---


def new_deck(rng: random.Random | None = None) -> list[str]:
    """Честная колода 52 карты, перетасованная. rng — для детерминизма в тестах."""
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    (rng or random).shuffle(deck)
    return deck


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ("В", "Д", "К"):
        return 10
    if rank == "Т":
        return 11
    return int(rank)


def hand_value(hand: list[str]) -> int:
    """Сумма руки: тузы считаются 11, пока нет перебора, дальше — по 1."""
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[:-1] == "Т")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand: list[str]) -> bool:
    """Блэкджек — строго 21 первыми ДВУМЯ картами (в старой версии был баг:
    бонус давали за любую 21)."""
    return len(hand) == 2 and hand_value(hand) == 21


def dealer_play(dealer_hand: list[str], deck: list[str]) -> tuple[list[str], list[str]]:
    """Дилер добирает до DEALER_STANDS_AT. Возвращает НОВЫЕ списки (рука, остаток
    колоды) — вход не мутируется (в старой версии evaluate менял руку in-place)."""
    hand = list(dealer_hand)
    rest = list(deck)
    while hand_value(hand) < DEALER_STANDS_AT and rest:
        hand.append(rest.pop())
    return hand, rest


def evaluate(player_hand: list[str], dealer_hand: list[str]) -> str:
    """Исход партии: win | lose | push. Перебор игрока — lose до хода дилера."""
    player = hand_value(player_hand)
    if player > 21:
        return "lose"
    dealer = hand_value(dealer_hand)
    if dealer > 21 or player > dealer:
        return "win"
    if player < dealer:
        return "lose"
    return "push"


def payout_for(outcome: str, bet: int, player_bj: bool, dealer_bj: bool) -> int:
    """Сколько монет вернуть НА баланс (ставка уже списана при раздаче).

    Оба блэкджека → ничья (возврат ставки); блэкджек игрока → ×2.5 (floor);
    победа → ×2; ничья → возврат; проигрыш → 0.
    """
    if player_bj and dealer_bj:
        return bet
    if outcome == "win":
        return int(bet * BLACKJACK_MULTIPLIER) if player_bj else bet * 2
    if outcome == "push":
        return bet
    return 0


def format_hand(hand: list[str]) -> str:
    return " ".join(hand)


# --- Состояние партии ---


@dataclass
class BlackjackState:
    """Состояние партии, сериализуется в GameState.state_json."""

    phase: str  # "betting" | "playing"
    bet: int = 0
    deck: list[str] = field(default_factory=list)
    player_hand: list[str] = field(default_factory=list)
    dealer_hand: list[str] = field(default_factory=list)
    message_id: int | None = None  # сообщение бота с кнопками (для edit из джобов)
    started_at: str = ""  # ISO 8601 с таймзоной (UTC)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": _STATE_VERSION,
                "phase": self.phase,
                "bet": self.bet,
                "deck": self.deck,
                "player_hand": self.player_hand,
                "dealer_hand": self.dealer_hand,
                "message_id": self.message_id,
                "started_at": self.started_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "BlackjackState | None":
        """None для битого/legacy payload (старый формат с int-руками) — партия
        считается мусором и удаляется вызывающим."""
        try:
            data = json.loads(payload)
            if data.get("version") != _STATE_VERSION:
                return None
            return cls(
                phase=str(data["phase"]),
                bet=int(data["bet"]),
                deck=[str(c) for c in data["deck"]],
                player_hand=[str(c) for c in data["player_hand"]],
                dealer_hand=[str(c) for c in data["dealer_hand"]],
                message_id=data.get("message_id"),
                started_at=str(data.get("started_at") or ""),
            )
        except (ValueError, KeyError, TypeError):
            return None

    def is_timed_out(self, now: datetime) -> bool:
        if not self.started_at:
            return True
        try:
            started = ensure_aware(datetime.fromisoformat(self.started_at))
        except ValueError:
            return True
        return now - started > timedelta(minutes=GAME_TIMEOUT_MINUTES)


def new_betting_state(message_id: int | None = None) -> BlackjackState:
    return BlackjackState(
        phase="betting",
        message_id=message_id,
        started_at=datetime.now(timezone.utc).isoformat(),
    )


# --- Персистентность ---


async def load_game(
    session: AsyncSession, user_id: int, chat_id: int
) -> BlackjackState | None:
    row = await session.get(GameState, {"user_id": user_id, "chat_id": chat_id})
    if row is None:
        return None
    state = BlackjackState.from_json(row.state_json)
    if state is None:
        # Legacy/битый payload — молча подчищаем.
        await session.delete(row)
        await session.flush()
    return state


async def save_game(
    session: AsyncSession, user_id: int, chat_id: int, state: BlackjackState
) -> None:
    await session.merge(
        GameState(user_id=user_id, chat_id=chat_id, state_json=state.to_json())
    )
    await session.flush()


async def delete_game(session: AsyncSession, user_id: int, chat_id: int) -> None:
    await session.execute(
        delete(GameState).where(
            GameState.user_id == user_id, GameState.chat_id == chat_id
        )
    )


async def get_all_active_games(
    session: AsyncSession,
) -> list[tuple[int, int, BlackjackState]]:
    rows = (await session.execute(select(GameState))).scalars().all()
    games: list[tuple[int, int, BlackjackState]] = []
    for row in rows:
        state = BlackjackState.from_json(row.state_json)
        if state is not None:
            games.append((row.user_id, row.chat_id, state))
    return games


async def place_bet_and_deal(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    bet: int,
    display_name: str | None = None,
) -> tuple[BlackjackState | None, str | None]:
    """Атомарно: проверка фазы и баланса → списание ставки → раздача 2+2.

    Возвращает (state, None) или (None, причина для callback.answer).
    Commit — на вызывающем (одна транзакция «списание+раздача»).
    """
    if bet not in BET_OPTIONS:
        return None, "Такой ставки нет."
    state = await load_game(session, user_id, chat_id)
    if state is None:
        return None, "Игра не найдена. Начни заново: /21"
    if state.phase != "betting":
        return None, "Ставка уже сделана."
    stats = await get_or_create_stats(session, user_id, chat_id, display_name)
    if stats.coins < bet:
        return None, f"Не хватает монет: у тебя {stats.coins}, ставка {bet}."
    stats.coins -= bet
    stats.games_played += 1  # wins инкрементируется при развязке — не задваивать

    deck = new_deck()
    state.phase = "playing"
    state.bet = bet
    state.player_hand = [deck.pop(), deck.pop()]
    state.dealer_hand = [deck.pop(), deck.pop()]
    state.deck = deck
    state.started_at = datetime.now(timezone.utc).isoformat()
    await save_game(session, user_id, chat_id, state)
    return state, None


async def record_round(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    bet: int,
    result: str,
    payout: int,
    player_hand: list[str],
    dealer_hand: list[str],
    closed_by: str = "player",
) -> None:
    """История партий: пишется при каждой развязке, хранится навсегда."""
    session.add(
        GameRound(
            user_id=user_id,
            chat_id=chat_id,
            bet=bet,
            result=result,
            payout=payout,
            player_hand=format_hand(player_hand),
            dealer_hand=format_hand(dealer_hand),
            closed_by=closed_by,
        )
    )
    await session.flush()


async def get_recent_rounds(
    session: AsyncSession, user_id: int, chat_id: int, limit: int = 5
) -> list[GameRound]:
    return list(
        (
            await session.execute(
                select(GameRound)
                .where(GameRound.user_id == user_id, GameRound.chat_id == chat_id)
                .order_by(desc(GameRound.finished_at), desc(GameRound.id))
                .limit(limit)
            )
        ).scalars().all()
    )


async def get_round_totals(
    session: AsyncSession, user_id: int, chat_id: int
) -> tuple[int, int]:
    """(всего поставлено, всего выплачено) за всю историю."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(GameRound.bet), 0),
                func.coalesce(func.sum(GameRound.payout), 0),
            ).where(GameRound.user_id == user_id, GameRound.chat_id == chat_id)
        )
    ).one()
    return int(row[0]), int(row[1])


async def get_leaderboard(
    session: AsyncSession, chat_id: int
) -> tuple[list[UserStat], list[UserStat]]:
    """Два топ-5: по монетам и по сыгранным партиям."""
    by_coins = (
        await session.execute(
            select(UserStat)
            .where(UserStat.chat_id == chat_id)
            .order_by(desc(UserStat.coins))
            .limit(5)
        )
    ).scalars().all()
    by_games = (
        await session.execute(
            select(UserStat)
            .where(UserStat.chat_id == chat_id, UserStat.games_played > 0)
            .order_by(desc(UserStat.games_played))
            .limit(5)
        )
    ).scalars().all()
    return list(by_coins), list(by_games)


async def get_week_stats(session: AsyncSession, chat_id: int) -> tuple[int, int, int]:
    """(партий, поставлено, выплачено) за последние 7 дней — для лидерборда."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    row = (
        await session.execute(
            select(
                func.count(GameRound.id),
                func.coalesce(func.sum(GameRound.bet), 0),
                func.coalesce(func.sum(GameRound.payout), 0),
            ).where(GameRound.chat_id == chat_id, GameRound.finished_at >= cutoff)
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


# --- Реестр сообщений-команд (для полуночной чистки темы) ---


async def register_game_command_message(
    session: AsyncSession, chat_id: int, message_id: int
) -> None:
    session.add(GameCommandMessage(chat_id=chat_id, message_id=message_id))
    await session.flush()


async def get_game_command_messages(
    session: AsyncSession, chat_id: int
) -> list[GameCommandMessage]:
    return list(
        (
            await session.execute(
                select(GameCommandMessage).where(GameCommandMessage.chat_id == chat_id)
            )
        ).scalars().all()
    )


async def clear_game_command_messages(session: AsyncSession, chat_id: int) -> None:
    await session.execute(
        delete(GameCommandMessage).where(GameCommandMessage.chat_id == chat_id)
    )


async def refund_active_bets(session: AsyncSession) -> int:
    """Возврат ставок всех активных партий перед принудительной чисткой
    (/restart_jobs, /reset_stats): деньги игроков не сгорают.

    Возвращает число партий с рефандом. Партии удаляет вызывающий.
    """
    refunded = 0
    for user_id, chat_id, state in await get_all_active_games(session):
        if state.phase != "playing" or state.bet <= 0:
            continue
        stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
        if stats is None:
            continue
        stats.coins += state.bet
        await record_round(
            session,
            user_id=user_id,
            chat_id=chat_id,
            bet=state.bet,
            result="push",
            payout=state.bet,
            player_hand=state.player_hand,
            dealer_hand=state.dealer_hand,
            closed_by="admin",
        )
        refunded += 1
    return refunded
