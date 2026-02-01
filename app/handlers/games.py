"""Почему: логика взаимодействия с пользователем в играх должна быть изолирована."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import get_session
from app.utils.time import is_game_time_allowed
from app.services.games import (
    apply_game_result,
    draw_card,
    end_game,
    format_hand,
    get_or_create_stats,
    get_weekly_leaderboard,
    load_game,
    register_game_command_message,
    save_game,
    start_game,
    evaluate_game,
)

router = Router()


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


def _display_name_from_callback(callback: CallbackQuery) -> str | None:
    if callback.from_user is None:
        return None
    return callback.from_user.username or callback.from_user.full_name


async def _track_game_command(message: Message) -> None:
    if not is_game_time_allowed(22, 24):
        return
    async for session in get_session():
        await register_game_command_message(
            session,
            message.chat.id,
            message.message_id,
        )
        await session.commit()


def game_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для игры."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Взять карту", callback_data="bj_hit"),
                InlineKeyboardButton(text="Остановиться", callback_data="bj_stand"),
            ]
        ]
    )


def _hand_value(hand: list[int]) -> int:
    """Вычисляет сумму руки с учётом тузов."""
    total = sum(hand)
    aces = hand.count(11)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def format_game_state(
    player_hand: list[int],
    dealer_hand: list[int],
    username: str,
    hide_dealer: bool = True,
) -> str:
    """Форматирует состояние игры для вывода."""
    player_value = _hand_value(player_hand)
    player_str = f"Рука @{username}: {format_hand(player_hand)} ({player_value})"

    if hide_dealer:
        # Показываем только первую карту дилера
        dealer_str = f"Карты дилера: {dealer_hand[0]} [?]"
    else:
        dealer_value = _hand_value(dealer_hand)
        dealer_str = f"Карты дилера: {format_hand(dealer_hand)} ({dealer_value})"

    return f"{player_str}\n{dealer_str}"


@router.message(Command("21"))
async def start_blackjack(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if message.from_user is None:
        return

    # Проверка времени: игра доступна с 22:00 до 23:00 МСК
    if not is_game_time_allowed(22, 24):
        await message.reply("Игра '21' доступна с 22:00 до 00:00 по Москве.")
        return
    await _track_game_command(message)

    async for session in get_session():
        # Проверяем, нет ли уже активной игры
        existing = await load_game(session, message.from_user.id, settings.forum_chat_id)
        if existing is not None:
            await message.reply("У тебя уже есть активная игра!")
            return

        state = await start_game(session, message.from_user.id, settings.forum_chat_id)
        await get_or_create_stats(
            session,
            message.from_user.id,
            settings.forum_chat_id,
            display_name=_display_name(message),
        )
        await session.commit()

    username = _display_name(message) or str(message.from_user.id)
    text = "Игра 21 началась!\n" + format_game_state(
        state.player_hand, state.dealer_hand, username, hide_dealer=True
    )
    await message.reply(text, reply_markup=game_keyboard())


@router.message(Command("score"))
async def show_score(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if message.from_user is None:
        return
    await _track_game_command(message)
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


@router.message(Command("21top"))
async def show_leaderboard(message: Message) -> None:
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    await _track_game_command(message)

    async for session in get_session():
        top_coins, top_games = await get_weekly_leaderboard(session, settings.forum_chat_id)

    lines = ["🏆 Топ-5 по монетам:"]
    for i, stat in enumerate(top_coins, 1):
        name = stat.display_name or str(stat.user_id)
        lines.append(f"{i}. @{name} — {stat.coins}")

    lines.append("")
    lines.append("🎮 Топ-5 по играм:")
    for i, stat in enumerate(top_games, 1):
        name = stat.display_name or str(stat.user_id)
        lines.append(f"{i}. @{name} — {stat.games_played}")

    await message.reply("\n".join(lines))


@router.callback_query(F.data == "bj_hit")
async def on_hit(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Взять карту'."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    async for session in get_session():
        state = await load_game(session, callback.from_user.id, settings.forum_chat_id)
        if state is None:
            await callback.answer("Игра не найдена. Начни новую с /21")
            return

        # Добавляем карту
        state.player_hand.append(draw_card())
        player_value = _hand_value(state.player_hand)

        # Проверяем перебор
        if player_value > 21:
            # Игрок проиграл
            stats = await apply_game_result(
                session,
                callback.from_user.id,
                settings.forum_chat_id,
                "lose",
                exact_21=False,
                display_name=_display_name_from_callback(callback),
            )
            await end_game(session, callback.from_user.id, settings.forum_chat_id)
            await session.commit()

            username = _display_name_from_callback(callback) or str(callback.from_user.id)
            text = format_game_state(
                state.player_hand, state.dealer_hand, username, hide_dealer=False
            )
            text += f"\n\nПеребор! Проигрыш.\nТвой баланс: {stats.coins} монет"
            await callback.message.edit_text(text, reply_markup=None)
            await callback.answer("Перебор!")
            return

        # Проверяем ровно 21
        if player_value == 21:
            # Автоматически заканчиваем игру
            result, blackjack = evaluate_game(state)
            stats = await apply_game_result(
                session,
                callback.from_user.id,
                settings.forum_chat_id,
                result,
                blackjack,
                display_name=_display_name_from_callback(callback),
            )
            await end_game(session, callback.from_user.id, settings.forum_chat_id)
            await session.commit()

            if result == "win":
                result_text = "победа!" + (" +2 за ровно 21!" if blackjack else "")
            elif result == "lose":
                result_text = "проигрыш."
            else:
                result_text = "ничья."

            username = _display_name_from_callback(callback) or str(callback.from_user.id)
            text = format_game_state(
                state.player_hand, state.dealer_hand, username, hide_dealer=False
            )
            text += f"\n\n{result_text.capitalize()}\nТвой баланс: {stats.coins} монет"
            await callback.message.edit_text(text, reply_markup=None)
            await callback.answer("21!")
            return

        # Продолжаем игру
        await save_game(session, callback.from_user.id, settings.forum_chat_id, state)
        await session.commit()

    username = _display_name_from_callback(callback) or str(callback.from_user.id)
    text = format_game_state(state.player_hand, state.dealer_hand, username, hide_dealer=True)
    await callback.message.edit_text(text, reply_markup=game_keyboard())
    await callback.answer()


@router.callback_query(F.data == "bj_stand")
async def on_stand(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Остановиться'."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    async for session in get_session():
        state = await load_game(session, callback.from_user.id, settings.forum_chat_id)
        if state is None:
            await callback.answer("Игра не найдена. Начни новую с /21")
            return

        result, blackjack = evaluate_game(state)
        stats = await apply_game_result(
            session,
            callback.from_user.id,
            settings.forum_chat_id,
            result,
            blackjack,
            display_name=_display_name_from_callback(callback),
        )
        await end_game(session, callback.from_user.id, settings.forum_chat_id)
        await session.commit()

    if result == "win":
        result_text = "победа!" + (" +2 за ровно 21!" if blackjack else "")
    elif result == "lose":
        result_text = "проигрыш."
    else:
        result_text = "ничья."

    username = _display_name_from_callback(callback) or str(callback.from_user.id)
    text = format_game_state(state.player_hand, state.dealer_hand, username, hide_dealer=False)
    text += f"\n\n{result_text.capitalize()}\nТвой баланс: {stats.coins} монет"
    await callback.message.edit_text(text, reply_markup=None)
    await callback.answer()
