"""Почему: логика игры и рейтингов вынесена отдельно от хендлеров."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameState, UserStat


@dataclass
class BlackjackState:
    player_hand: list[int]
    dealer_hand: list[int]
    finished: bool = False
    started_at: str | None = None  # ISO format datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "player_hand": self.player_hand,
                "dealer_hand": self.dealer_hand,
                "finished": self.finished,
                "started_at": self.started_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "BlackjackState":
        data = json.loads(payload)
        return cls(
            player_hand=list(data["player_hand"]),
            dealer_hand=list(data["dealer_hand"]),
            finished=bool(data.get("finished")),
            started_at=data.get("started_at"),
        )


def _draw_card() -> int:
    return random.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 11])


def _hand_value(hand: list[int]) -> int:
    total = sum(hand)
    aces = hand.count(11)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _is_exact_21(hand: list[int]) -> bool:
    return _hand_value(hand) == 21


def _dealer_play(hand: list[int]) -> list[int]:
    while _hand_value(hand) < 17:
        hand.append(_draw_card())
    return hand


def draw_card() -> int:
    """Публичный генератор карт."""

    return _draw_card()


async def get_or_create_stats(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> UserStat:
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None:
        stats = UserStat(
            user_id=user_id, chat_id=chat_id, coins=100, display_name=display_name
        )
        session.add(stats)
        await session.flush()
    if display_name:
        stats.display_name = display_name
    return stats


async def start_game(
    session: AsyncSession, user_id: int, chat_id: int
) -> BlackjackState:
    state = BlackjackState(
        player_hand=[_draw_card(), _draw_card()],
        dealer_hand=[_draw_card(), _draw_card()],  # 2 карты, вторая скрыта
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    await session.merge(
        GameState(user_id=user_id, chat_id=chat_id, state_json=state.to_json())
    )
    await session.flush()
    return state


async def load_game(
    session: AsyncSession, user_id: int, chat_id: int
) -> BlackjackState | None:
    record = await session.get(GameState, {"user_id": user_id, "chat_id": chat_id})
    if record is None:
        return None
    return BlackjackState.from_json(record.state_json)


async def save_game(
    session: AsyncSession, user_id: int, chat_id: int, state: BlackjackState
) -> None:
    await session.merge(
        GameState(user_id=user_id, chat_id=chat_id, state_json=state.to_json())
    )
    await session.flush()


async def end_game(session: AsyncSession, user_id: int, chat_id: int) -> None:
    await session.execute(
        delete(GameState).where(
            GameState.user_id == user_id, GameState.chat_id == chat_id
        )
    )


async def apply_game_result(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    result: str,
    exact_21: bool,
    display_name: str | None = None,
) -> UserStat:
    stats = await get_or_create_stats(
        session, user_id, chat_id, display_name=display_name
    )
    stats.games_played += 1
    if result == "win":
        stats.wins += 1
        stats.coins += 2 if exact_21 else 1
    elif result == "lose":
        stats.coins -= 1
    await session.flush()
    return stats


def evaluate_game(state: BlackjackState) -> tuple[str, bool]:
    player_value = _hand_value(state.player_hand)
    dealer_hand = _dealer_play(state.dealer_hand)
    dealer_value = _hand_value(dealer_hand)
    exact_21 = _is_exact_21(state.player_hand)

    if player_value > 21:
        return "lose", exact_21
    if dealer_value > 21:
        return "win", exact_21
    if player_value > dealer_value:
        return "win", exact_21
    if player_value < dealer_value:
        return "lose", exact_21
    return "push", exact_21


def format_hand(hand: list[int]) -> str:
    return " ".join(str(card) for card in hand)


async def get_weekly_leaderboard(
    session: AsyncSession, chat_id: int
) -> tuple[list[UserStat], list[UserStat]]:
    top_coins = (
        await session.scalars(
            select(UserStat)
            .where(UserStat.chat_id == chat_id)
            .order_by(UserStat.coins.desc())
            .limit(5)
        )
    ).all()
    top_games = (
        await session.scalars(
            select(UserStat)
            .where(UserStat.chat_id == chat_id)
            .order_by(UserStat.games_played.desc())
            .limit(5)
        )
    ).all()
    return top_coins, top_games


def can_grant_coins(stats: UserStat, now: datetime, amount: int) -> bool:
    if amount > 10:
        return False
    if stats.last_coin_grant_at is None:
        return True
    if now - stats.last_coin_grant_at > timedelta(days=1):
        return True
    return stats.coins_granted_today + amount <= 10


def register_coin_grant(stats: UserStat, now: datetime, amount: int) -> None:
    if stats.last_coin_grant_at is None or now - stats.last_coin_grant_at > timedelta(
        days=1
    ):
        stats.coins_granted_today = 0
    stats.coins_granted_today += amount
    stats.last_coin_grant_at = now
    stats.coins += amount


GAME_TIMEOUT_MINUTES = 1


async def get_all_active_games(
    session: AsyncSession,
) -> list[tuple[int, int, BlackjackState]]:
    """Возвращает список (user_id, chat_id, state) для всех активных игр."""
    result = await session.scalars(select(GameState))
    games = []
    for record in result:
        state = BlackjackState.from_json(record.state_json)
        games.append((record.user_id, record.chat_id, state))
    return games


def is_game_timed_out(state: BlackjackState, now: datetime) -> bool:
    """Проверяет, истёк ли таймаут игры."""
    if state.started_at is None:
        return False
    started = datetime.fromisoformat(state.started_at)
    return now - started > timedelta(minutes=GAME_TIMEOUT_MINUTES)
