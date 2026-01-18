"""Почему: логика взаимодействия с пользователем в играх должна быть изолирована."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.games import (
    apply_game_result,
    draw_card,
    end_game,
    format_hand,
    get_or_create_stats,
    load_game,
    save_game,
    start_game,
    evaluate_game,
)

router = Router()


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


@router.message(Command("bj"))
async def start_blackjack(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if message.from_user is None:
        return
    async for session in get_session():
        state = await start_game(session, message.from_user.id, settings.forum_chat_id)
        await get_or_create_stats(
            session,
            message.from_user.id,
            settings.forum_chat_id,
            display_name=_display_name(message),
        )
        await session.commit()
    await message.reply(
        "Игра 21 началась!\n"
        f"Твоя рука: {format_hand(state.player_hand)}\n"
        f"Карта дилера: {format_hand(state.dealer_hand)}\n"
        "Ответь 'взять' или 'стоп'."
    )


@router.message(Command("score"))
async def show_score(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if message.from_user is None:
        return
    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            message.from_user.id,
            settings.forum_chat_id,
            display_name=_display_name(message),
        )
        await session.commit()
    await message.reply(
        f"Монеты: {stats.coins}\nИгры: {stats.games_played}\nПобеды: {stats.wins}"
    )


@router.message()
async def blackjack_action(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if message.from_user is None:
        return
    if message.text is None:
        return
    action = message.text.lower().strip()
    if action not in {"взять", "стоп"}:
        return

    async for session in get_session():
        state = await load_game(session, message.from_user.id, settings.forum_chat_id)
        if state is None:
            return
        if action == "взять":
            state.player_hand.append(draw_card())
            await save_game(
                session, message.from_user.id, settings.forum_chat_id, state
            )
            await session.commit()
            await message.reply(
                "Карта добавлена.\n"
                f"Твоя рука: {format_hand(state.player_hand)}\n"
                "Еще 'взять' или 'стоп'?"
            )
            return

        result, blackjack = evaluate_game(state)
        stats = await apply_game_result(
            session,
            message.from_user.id,
            settings.forum_chat_id,
            result,
            blackjack,
            display_name=_display_name(message),
        )
        await end_game(session, message.from_user.id, settings.forum_chat_id)
        await session.commit()

    if result == "win":
        text = "Победа!"
    elif result == "lose":
        text = "Проигрыш."
    else:
        text = "Ничья."
    await message.reply(
        f"{text}\nМонеты: {stats.coins}\nИгры: {stats.games_played}\nПобеды: {stats.wins}"
    )
