"""Почему: игра «21» живёт только в теме игр (topic_games) — бот там молчит и не
модерирует, а весь игровой UX (команды, кнопки, джобы) изолирован в этом модуле.

Гонки: все мутации денег/партий — под per-user asyncio.Lock (двойные клики,
параллельные /21, таймаут-джоба vs клик игрока). Списание/выплата — один commit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.services import blackjack as bj
from app.services.blackjack import (
    BANKRUPT_TOP_UP,
    BET_OPTIONS,
    BLACKJACK_MULTIPLIER,
    GAME_TIMEOUT_MINUTES,
    MIN_BET,
    BlackjackState,
)
from app.services.coins import (
    DAILY_BONUS,
    get_or_create_stats,
    rescue_if_bankrupt,
    transfer_coins,
    try_grant_daily_bonus,
)
from app.utils.admin import extract_target_user
from app.utils.time import is_game_time_allowed

logger = logging.getLogger(__name__)

router = Router()

GAME_START_HOUR = 22
GAME_END_HOUR = 24  # [22, 24) = 22:00–23:59 МСК

# Per-user блокировки: сериализуют двойные клики и джобы. Словарь не чистим —
# рост O(число игравших за аптайм), десятки записей, приемлемо.
_user_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


RULES_TEXT = (
    "🃏 Блэкджек «21» — здесь, с 22:00 до 00:00 МСК\n\n"
    "• /21 — начать партию и выбрать ставку (5/10/25/50 монет), ставка списывается сразу\n"
    "• Цель: набрать больше очков, чем дилер, но не больше 21\n"
    "• В/Д/К = 10, Туз = 11 или 1 (сам подстраивается), колода — честные 52 карты\n"
    "• Дилер добирает до 17\n"
    "• Выплаты: победа ×2 • блэкджек (21 двумя картами) ×2.5 • ничья — возврат • проигрыш — ставка сгорает\n"
    f"• Нет хода {GAME_TIMEOUT_MINUTES} минут — авто-«хватит», рука доигрывается как есть\n"
    "• В полночь партии закрываются, сообщения с командами подчищаются\n\n"
    f"💰 /бонус — +{DAILY_BONUS} монет раз в сутки • баланс меньше {MIN_BET} — при /21 пополним до {BANKRUPT_TOP_UP}\n"
    "📊 /score — баланс и последние партии • /21top — лидеры • /подарить — перевод монет (реплай + сумма)"
)


def _in_games_topic(message: Message) -> bool:
    return (
        settings.topic_games is not None
        and message.chat.id == settings.forum_chat_id
        and message.message_thread_id == settings.topic_games
    )


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


def _bet_keyboard(user_id: int, balance: int) -> InlineKeyboardMarkup:
    """Кнопки ставок; ставки больше баланса не показываем (реальная проверка
    всё равно в place_bet_and_deal — баланс мог измениться между /21 и кликом)."""
    bets = [
        InlineKeyboardButton(text=f"{b} 🪙", callback_data=f"bj:bet:{user_id}:{b}")
        for b in BET_OPTIONS
        if b <= balance
    ]
    rows = [bets] if bets else []
    rows.append(
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"bj:cancel:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _play_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🃏 Ещё карту", callback_data=f"bj:hit:{user_id}"),
                InlineKeyboardButton(text="✋ Хватит", callback_data=f"bj:stand:{user_id}"),
            ]
        ]
    )


def _playing_text(state: BlackjackState, name: str) -> str:
    return (
        f"🎰 Партия {name} — ставка {state.bet} 🪙\n\n"
        f"Твои карты: {bj.format_hand(state.player_hand)} = {bj.hand_value(state.player_hand)}\n"
        f"Дилер: {state.dealer_hand[0]} [?]"
    )


def _outcome_text(
    state: BlackjackState, result: str, payout: int, balance: int, name: str
) -> str:
    player = f"{bj.format_hand(state.player_hand)} = {bj.hand_value(state.player_hand)}"
    dealer = f"{bj.format_hand(state.dealer_hand)} = {bj.hand_value(state.dealer_hand)}"
    if result == "blackjack":
        verdict = f"🃏 Блэкджек! Выплата {payout} 🪙"
    elif result == "win":
        verdict = f"🎉 Победа! Выплата {payout} 🪙"
    elif result == "push":
        verdict = f"🤝 Ничья — ставка {state.bet} 🪙 возвращена"
    elif bj.hand_value(state.player_hand) > 21:
        verdict = f"💥 Перебор! Ставка {state.bet} 🪙 сгорела"
    else:
        verdict = f"😔 Проигрыш — ставка {state.bet} 🪙 сгорела"
    return (
        f"🎰 Партия {name} — ставка {state.bet} 🪙\n\n"
        f"Твои карты: {player}\nДилер: {dealer}\n\n{verdict}\nБаланс: {balance} 🪙"
    )


async def _register_cleanup(session: AsyncSession, message: Message) -> None:
    await bj.register_game_command_message(session, message.chat.id, message.message_id)


async def _settle(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    state: BlackjackState,
    *,
    closed_by: str = "player",
) -> tuple[str, int, int]:
    """Единая развязка: дилер → исход → выплата → история → удаление партии.

    Возвращает (result, payout, новый баланс). Commit — на вызывающем.
    """
    player_bj = bj.is_blackjack(state.player_hand)
    if bj.hand_value(state.player_hand) > 21:
        # Перебор игрока — дилер не добирает.
        outcome = "lose"
    else:
        state.dealer_hand, state.deck = bj.dealer_play(state.dealer_hand, state.deck)
        outcome = bj.evaluate(state.player_hand, state.dealer_hand)
    dealer_bj = bj.is_blackjack(state.dealer_hand)
    payout = bj.payout_for(outcome, state.bet, player_bj, dealer_bj)
    result = "blackjack" if (outcome == "win" and player_bj) else outcome

    stats = await get_or_create_stats(session, user_id, chat_id)
    stats.coins += payout
    if outcome == "win":
        stats.wins += 1  # games_played уже учтён при ставке
    await bj.record_round(
        session,
        user_id=user_id,
        chat_id=chat_id,
        bet=state.bet,
        result=result,
        payout=payout,
        player_hand=state.player_hand,
        dealer_hand=state.dealer_hand,
        closed_by=closed_by,
    )
    await bj.delete_game(session, user_id, chat_id)
    return result, payout, stats.coins


async def _safe_edit(bot: Bot, chat_id: int, message_id: int | None, text: str,
                     reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """edit_text устаревших сообщений часто невозможен — молча пропускаем."""
    if message_id is None:
        return
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
    except TelegramBadRequest:
        pass


# --- Команды ---


@router.message(Command("21"))
async def cmd_blackjack(message: Message, bot: Bot) -> None:
    if not _in_games_topic(message) or message.from_user is None:
        return
    user_id = message.from_user.id
    async with _lock_for(user_id):
        async for session in get_session():
            await _register_cleanup(session, message)
            if not is_game_time_allowed(GAME_START_HOUR, GAME_END_HOUR):
                await session.commit()
                reply = await message.reply(
                    "🕙 Игра «21» открыта с 22:00 до 00:00 по Москве. Приходи вечером!"
                )
                async for s2 in get_session():
                    await bj.register_game_command_message(s2, reply.chat.id, reply.message_id)
                    await s2.commit()
                    break
                return

            existing = await bj.load_game(session, user_id, message.chat.id)
            if existing is not None:
                await session.commit()
                await message.reply("У тебя уже есть активная партия — доиграй её.")
                return

            stats = await get_or_create_stats(
                session, user_id, message.chat.id, display_name=_display_name(message)
            )
            rescued = rescue_if_bankrupt(stats, MIN_BET, BANKRUPT_TOP_UP)
            balance = stats.coins
            state = bj.new_betting_state()
            await bj.save_game(session, user_id, message.chat.id, state)
            await session.commit()

            prefix = (
                f"🆘 Банкрот! Держи {BANKRUPT_TOP_UP} 🪙 на реванш.\n\n" if rescued else ""
            )
            reply = await message.reply(
                f"{prefix}💰 Баланс: {balance} 🪙\nВыбирай ставку:",
                reply_markup=_bet_keyboard(user_id, balance),
            )
            # message_id ответа — в состояние (edit из джобов) + в реестр чистки.
            async for s2 in get_session():
                state.message_id = reply.message_id
                await bj.save_game(s2, user_id, message.chat.id, state)
                await bj.register_game_command_message(s2, reply.chat.id, reply.message_id)
                await s2.commit()
                break
            return


@router.message(Command("бонус", "bonus"))
async def cmd_bonus(message: Message) -> None:
    if not _in_games_topic(message) or message.from_user is None:
        return
    async with _lock_for(message.from_user.id):
        async for session in get_session():
            await _register_cleanup(session, message)
            stats = await get_or_create_stats(
                session,
                message.from_user.id,
                message.chat.id,
                display_name=_display_name(message),
            )
            granted = try_grant_daily_bonus(stats, datetime.now(timezone.utc))
            await session.commit()
            if granted:
                await message.reply(
                    f"💰 +{DAILY_BONUS} монет! Баланс: {stats.coins} 🪙"
                )
            else:
                await message.reply("Бонус уже получен сегодня — приходи завтра.")
            return


@router.message(Command("score"))
async def cmd_score(message: Message) -> None:
    if not _in_games_topic(message) or message.from_user is None:
        return
    async for session in get_session():
        await _register_cleanup(session, message)
        stats = await get_or_create_stats(
            session, message.from_user.id, message.chat.id,
            display_name=_display_name(message),
        )
        rounds = await bj.get_recent_rounds(session, message.from_user.id, message.chat.id)
        total_bet, total_paid = await bj.get_round_totals(
            session, message.from_user.id, message.chat.id
        )
        await session.commit()

        lines = [
            f"📊 {_display_name(message) or 'Игрок'}",
            f"Монеты: {stats.coins} 🪙 | Партий: {stats.games_played} | Побед: {stats.wins}",
        ]
        if rounds:
            lines.append(f"Всего поставлено: {total_bet} | выплачено: {total_paid}")
            lines.append("\nПоследние партии:")
            marks = {"win": "🎉", "blackjack": "🃏", "lose": "💥", "push": "🤝"}
            for r in rounds:
                delta = r.payout - r.bet
                lines.append(
                    f"{marks.get(r.result, '•')} ставка {r.bet} → {r.result} ({delta:+d})"
                )
        await message.reply("\n".join(lines))
        return


@router.message(Command("21top"))
async def cmd_leaderboard(message: Message) -> None:
    if not _in_games_topic(message):
        return
    async for session in get_session():
        await _register_cleanup(session, message)
        await session.commit()
        text = await _leaderboard_text(session, message.chat.id)
        await message.reply(text)
        return


async def _leaderboard_text(session: AsyncSession, chat_id: int) -> str:
    by_coins, by_games = await bj.get_leaderboard(session, chat_id)
    lines = ["🏆 Лидеры «21»", "", "💰 По монетам:"]
    for i, s in enumerate(by_coins, 1):
        lines.append(f"{i}. {s.display_name or s.user_id} — {s.coins} 🪙")
    lines += ["", "🎰 По партиям:"]
    for i, s in enumerate(by_games, 1):
        lines.append(f"{i}. {s.display_name or s.user_id} — {s.games_played} (побед {s.wins})")
    return "\n".join(lines)


@router.message(Command("подарить", "gift"))
async def cmd_gift(message: Message) -> None:
    if not _in_games_topic(message) or message.from_user is None:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество монет: /подарить 50 (реплаем на получателя)")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply("Количество монет должно быть числом.")
        return
    target_id, target_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение получателя.")
        return
    if target_id == message.from_user.id:
        await message.reply("Нельзя подарить монеты самому себе.")
        return
    async with _lock_for(message.from_user.id):
        async for session in get_session():
            sender = await get_or_create_stats(
                session, message.from_user.id, message.chat.id,
                display_name=_display_name(message),
            )
            receiver = await get_or_create_stats(
                session, target_id, message.chat.id, display_name=target_name
            )
            error = transfer_coins(sender, receiver, amount)
            if error:
                await message.reply(error)
                return
            await session.commit()
            sender_name = _display_name(message) or str(message.from_user.id)
            await message.reply(
                f"🎁 {sender_name} подарил(а) {amount} 🪙 — {target_name or target_id}!\n"
                f"Балансы: {sender.coins} | {receiver.coins}"
            )
            return


@router.message(Command("21rules", "правила21"))
async def cmd_rules(message: Message) -> None:
    if not _in_games_topic(message):
        return
    await message.reply(RULES_TEXT)


# --- Callback-кнопки: bj:bet|cancel|hit|stand:{owner_id}[:amount] ---


@router.callback_query(F.data.startswith("bj:"))
async def on_game_callback(callback: CallbackQuery, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or callback.from_user is None:
        await callback.answer()
        return
    action, owner = parts[1], parts[2]
    if owner != str(callback.from_user.id):
        await callback.answer("Это не твоя игра 😉")
        return
    if callback.message is None:
        await callback.answer()
        return
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with _lock_for(user_id):
        async for session in get_session():
            state = await bj.load_game(session, user_id, chat_id)
            if state is None:
                await session.commit()
                await callback.answer("Игра не найдена. Начни заново: /21")
                await _safe_edit(bot, chat_id, callback.message.message_id,
                                 "Партия завершена. Новая — /21")
                return
            # Клик по устаревшему сообщению (после таймаута была новая партия).
            if state.message_id and state.message_id != callback.message.message_id:
                await session.commit()
                await callback.answer("Эта партия уже неактуальна.")
                return

            name = callback.from_user.username or callback.from_user.full_name

            if action == "cancel":
                if state.phase != "betting":
                    await session.commit()
                    await callback.answer("Ставка уже в игре — доиграй партию.")
                    return
                await bj.delete_game(session, user_id, chat_id)
                await session.commit()
                await callback.answer("Партия отменена")
                await _safe_edit(bot, chat_id, callback.message.message_id,
                                 "✖️ Партия отменена — деньги не тронуты. Новая — /21")
                return

            if action == "bet":
                try:
                    amount = int(parts[3])
                except (IndexError, ValueError):
                    await callback.answer("Некорректная ставка.")
                    return
                state, reason = await bj.place_bet_and_deal(
                    session, user_id, chat_id, amount, display_name=name
                )
                if state is None:
                    await session.commit()
                    await callback.answer(reason or "Не получилось.")
                    return
                state.message_id = callback.message.message_id
                await bj.save_game(session, user_id, chat_id, state)

                if bj.is_blackjack(state.player_hand):
                    # Блэкджек с раздачи — немедленная развязка.
                    result, payout, balance = await _settle(session, user_id, chat_id, state)
                    await session.commit()
                    await callback.answer("Блэкджек!")
                    await _safe_edit(bot, chat_id, callback.message.message_id,
                                     _outcome_text(state, result, payout, balance, name))
                    return
                await session.commit()
                await callback.answer(f"Ставка {amount} принята")
                await _safe_edit(bot, chat_id, callback.message.message_id,
                                 _playing_text(state, name), _play_keyboard(user_id))
                return

            if action == "hit":
                if state.phase != "playing" or not state.deck:
                    await session.commit()
                    await callback.answer("Сейчас нельзя взять карту.")
                    return
                state.player_hand.append(state.deck.pop())
                value = bj.hand_value(state.player_hand)
                if value >= 21:
                    # Перебор → lose; ровно 21 → авто-stand.
                    result, payout, balance = await _settle(session, user_id, chat_id, state)
                    await session.commit()
                    await callback.answer()
                    await _safe_edit(bot, chat_id, callback.message.message_id,
                                     _outcome_text(state, result, payout, balance, name))
                    return
                await bj.save_game(session, user_id, chat_id, state)
                await session.commit()
                await callback.answer()
                await _safe_edit(bot, chat_id, callback.message.message_id,
                                 _playing_text(state, name), _play_keyboard(user_id))
                return

            if action == "stand":
                if state.phase != "playing":
                    await session.commit()
                    await callback.answer("Сначала сделай ставку.")
                    return
                result, payout, balance = await _settle(session, user_id, chat_id, state)
                await session.commit()
                await callback.answer()
                await _safe_edit(bot, chat_id, callback.message.message_id,
                                 _outcome_text(state, result, payout, balance, name))
                return

            await session.commit()
            await callback.answer()
            return


# --- Scheduler-джобы (регистрируются в main.schedule_jobs) ---


async def check_game_timeouts(bot: Bot) -> None:
    """Каждую минуту: просроченные партии. betting — снять без потерь;
    playing — авто-«хватит» (возврат ставки абьюзился бы ожиданием таймаута)."""
    if settings.topic_games is None:
        return
    try:
        now = datetime.now(timezone.utc)
        async for session in get_session():
            games = await bj.get_all_active_games(session)
            break
        else:
            return
        for user_id, chat_id, state in games:
            if not state.is_timed_out(now):
                continue
            async with _lock_for(user_id):
                async for session in get_session():
                    # Перечитываем под lock'ом — игрок мог доиграть.
                    fresh = await bj.load_game(session, user_id, chat_id)
                    if fresh is None or not fresh.is_timed_out(now):
                        await session.commit()
                        break
                    if fresh.phase == "betting":
                        await bj.delete_game(session, user_id, chat_id)
                        await session.commit()
                        await _safe_edit(bot, chat_id, fresh.message_id,
                                         "⏰ Партия отменена по таймауту — деньги не тронуты. Новая — /21")
                        break
                    result, payout, balance = await _settle(
                        session, user_id, chat_id, fresh, closed_by="timeout"
                    )
                    await session.commit()
                    name = str(user_id)
                    await _safe_edit(bot, chat_id, fresh.message_id,
                                     "⏰ Время вышло — авто-«хватит».\n\n"
                                     + _outcome_text(fresh, result, payout, balance, name))
                    break
    except Exception:
        logger.warning("BLACKJACK: job таймаутов не отработал.", exc_info=True)


async def close_games_and_cleanup(bot: Bot) -> None:
    """00:05: закрыть все партии (авто-«хватит») и подчистить сообщения-команды."""
    if settings.topic_games is None:
        return
    try:
        async for session in get_session():
            games = await bj.get_all_active_games(session)
            break
        else:
            return
        for user_id, chat_id, state in games:
            async with _lock_for(user_id):
                async for session in get_session():
                    fresh = await bj.load_game(session, user_id, chat_id)
                    if fresh is None:
                        await session.commit()
                        break
                    if fresh.phase == "betting":
                        await bj.delete_game(session, user_id, chat_id)
                        await session.commit()
                    else:
                        await _settle(session, user_id, chat_id, fresh, closed_by="midnight")
                        await session.commit()
                    await _safe_edit(bot, chat_id, fresh.message_id,
                                     "🌙 Полночь — казино закрыто, партия завершена. До завтра!")
                    break

        # Чистка сообщений-команд за вечер.
        async for session in get_session():
            records = await bj.get_game_command_messages(session, settings.forum_chat_id)
            await bj.clear_game_command_messages(session, settings.forum_chat_id)
            await session.commit()
            break
        else:
            return
        for record in records:
            try:
                await bot.delete_message(record.chat_id, record.message_id)
            except TelegramBadRequest:
                pass
        logger.info("BLACKJACK: полуночная чистка — %d сообщений.", len(records))
    except Exception:
        logger.warning("BLACKJACK: полуночная чистка не отработала.", exc_info=True)


async def send_weekly_game_leaderboard(bot: Bot) -> None:
    """Суббота 21:00 (за час до окна): лидеры + статистика недели."""
    if settings.topic_games is None:
        return
    try:
        async for session in get_session():
            text = await _leaderboard_text(session, settings.forum_chat_id)
            rounds, week_bet, week_paid = await bj.get_week_stats(
                session, settings.forum_chat_id
            )
            break
        else:
            return
        if rounds:
            text += (
                f"\n\n📅 За неделю: партий {rounds}, "
                f"поставлено {week_bet} 🪙, выплачено {week_paid} 🪙"
            )
        text += "\n\n🕙 Казино открывается в 22:00 — /21"
        await bot.send_message(
            settings.forum_chat_id, text, message_thread_id=settings.topic_games
        )
    except Exception:
        logger.warning("BLACKJACK: лидерборд не отправился.", exc_info=True)


async def announce_blackjack_rules(bot: Bot) -> None:
    """21:55 ежедневно: анонс правил перед открытием окна."""
    if settings.topic_games is None:
        return
    try:
        msg = await bot.send_message(
            settings.forum_chat_id, RULES_TEXT, message_thread_id=settings.topic_games
        )
        # Анонс тоже подчищаем в полночь, чтобы тема не зарастала.
        async for session in get_session():
            await bj.register_game_command_message(session, msg.chat.id, msg.message_id)
            await session.commit()
            break
    except Exception:
        logger.warning("BLACKJACK: анонс правил не отправился.", exc_info=True)
