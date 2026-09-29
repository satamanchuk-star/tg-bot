"""Почему: викторина живёт в теме игр (topic_games) — бот там молчит и не
модерирует. Тур ведёт единственный driver-таск (один писатель переходов
вопроса), приём ответов — «первый верный забирает вопрос» атомарно под
chat-lock. Это убирает гонки, на которых ломалась старая версия.

Надёжность: всё состояние тура персистентно (QuizSession.state_json), а
watchdog-джоба возобновляет driver после рестарта бота или закрывает зависшую
сессию.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services import quiz as q
from app.services.coins import get_or_create_stats
from app.services.game_common import (
    LockRegistry,
    display_name as _gc_display_name,
    in_games_topic,
    safe_react,
    safe_send,
)
from app.services.game_common import safe_edit as _gc_safe_edit

logger = logging.getLogger(__name__)

router = Router()

QUIZ_HOUR = 20  # старт в 20:00 МСК

# chat-lock: сериализует приём ответов и переходы вопроса (first-wins атомарен).
_chat_locks = LockRegistry()
# Событие «на текущий вопрос ответили верно» — driver ждёт его вместо таймаута.
_answer_events: dict[int, asyncio.Event] = {}
# Активные driver-таски: chat_id → Task (для watchdog: возобновить после рестарта).
_running: dict[int, asyncio.Task] = {}

_lock_for = _chat_locks.for_key


def _event_for(chat_id: int) -> asyncio.Event:
    return _answer_events.setdefault(chat_id, asyncio.Event())


_INVITATIONS = (
    "🧠 Соседи, через 5 минут — викторина!\nВ 20:00 стартуем. Разминаем эрудицию, на кону монеты. Кто сегодня умнее всех?",
    "❓ Вечерний квиз на подходе!\nВ 20:00 бот засыпет вопросами. За каждый верный ответ — монеты, победителю тура — джекпот. Готовь пальцы!",
    "🎓 Тс-с… через 5 минут проверка на эрудицию\nВ 20:00 викторина. Отвечай первым — забирай монеты. Соседи, кто в игре?",
    "🔥 Пять минут до викторины!\nВ 20:00 15 вопросов, на середине каждого — подсказка. Первый верный ответ — монеты твои. Врывайся!",
    "🧩 Знатоки, по местам!\nВ 20:00 стартует квиз. Быстрее всех и без ошибок — вот рецепт победы. Ждём в 20:00!",
    "📚 Викторина через 5 минут!\nВ 20:00 узнаем, кто в доме самый эрудированный. Монеты за ответы, большой бонус победителю. /викторина_правила",
    "⚡ Внимание, вечерний квиз!\nВ 20:00 бот начинает. Кто первым даст верный ответ — тот и молодец (и при монетах). Соседи, готовы?",
    "🏆 Место чемпиона свободно!\nВ 20:00 викторина — приходи побороться за титул знатока и монеты. Через 5 минут старт!",
)


def _pick_invitation() -> str:
    return random.choice(_INVITATIONS)


RULES_TEXT = (
    "🧠 Викторина — каждый вечер в 20:00 МСК, здесь\n"
    "━━━━━━━━━━━━\n"
    f"• {q.QUESTIONS_PER_ROUND} вопросов, от {q.SECONDS_PER_QUESTION} до "
    f"{q.SECONDS_PER_QUESTION + q.EXTRA_SECONDS_MAX} секунд: чем длиннее вопрос, тем больше времени\n"
    "• На середине вопроса — подсказка: первая буква и длина ответа 🔤\n"
    "• Отвечай прямо в чат — «первый верно ответивший» забирает вопрос\n"
    f"• За верный ответ: +{q.COINS_PER_CORRECT} 🪙 • победителю тура: +{q.WINNER_BONUS} 🪙\n"
    "• Опечатки прощаются, лишние слова в ответе — не страшны\n"
    "• Числа и даты нужно назвать точно\n"
    "• Неверный ответ ничем не грозит — пробуй ещё, пока идёт вопрос\n\n"
    "📊 /викторина_топ — знатоки за всё время"
)


# Общие примитивы игр — app/services/game_common.py
_in_games_topic = in_games_topic


def _is_games_topic_answer(message: Message) -> bool:
    """Фильтр приёма ответов: срабатывает ТОЛЬКО на не-командный текст в теме
    игр. Иначе catch-all перехватил бы все сообщения форума и лишил бы
    модерацию входящих (роутер викторины идёт до модерации)."""
    return (
        message.text is not None
        and not message.text.startswith("/")
        and _in_games_topic(message)
    )


_display_name = _gc_display_name
_safe_send = safe_send
_safe_edit = _gc_safe_edit
_safe_react = safe_react

# Реакции-анимации на ответы игроков: верный — праздник, «почти» — подсказка
# уточнить формулировку, неверный — раздумье.
_CORRECT_REACTIONS = ("🎉", "🏆", "⚡", "🔥", "👏")
_NEAR_REACTION = "👀"
_WRONG_REACTION = "🤔"

# Реплики на почти-верный ответ: игрок должен понять, что мысль верная и надо
# лишь уточнить формулировку. Разные каждый раз — иначе бот выглядит роботом.
# Содержание ответа не выдаём: только «ты рядом».
_NEAR_HINTS = (
    "🔥 Горячо! Мысль верная — уточни формулировку.",
    "🎯 Рядом! Не хватает слова — попробуй полнее.",
    "💡 Тепло-тепло! Скажи чуть точнее.",
    "🤏 Совсем чуть-чуть! Дожимай.",
    "⚡ Почти в точку! Уточни ответ.",
    "🧭 Верное направление — добавь недостающее.",
    "🌡 Горячо! Ещё разок, поточнее.",
    "😮 Ой, почти! Переформулируй.",
    "🪄 Почти угадал — не хватает детали.",
    "📎 Близко! Ответ рядом с твоей версией.",
    "🚀 Почти долетел! Уточни формулировку.",
    "🔍 Тепло! Присмотрись к формулировке.",
    "🎣 Клюёт! Но нужно точнее.",
    "🫡 Почти! Соберись и добей.",
    "🧩 Кусочек на месте — не хватает остального.",
)

# chat_id → индекс вопроса, по которому подсказку уже дали: одна реплика на
# вопрос. Иначе при нескольких «почти» тема превращается в спам бота.
_near_hint_at: dict[int, int] = {}
# chat_id → прошлая реплика: не повторяем подряд одну и ту же.
_last_near_hint: dict[int, str] = {}


def _pick_near_hint(chat_id: int) -> str:
    options = [h for h in _NEAR_HINTS if h != _last_near_hint.get(chat_id)]
    hint = random.choice(options or list(_NEAR_HINTS))
    _last_near_hint[chat_id] = hint
    return hint


async def _safe_reply(message: Message, text: str) -> None:
    """Реплай игроку; транзиентные ошибки Telegram игру ронять не должны."""
    try:
        await message.reply(text)
    except (TelegramBadRequest, TelegramRetryAfter):
        pass


async def _send_start_animation(bot: Bot) -> None:
    """Анимированный дротик 🎯 перед стартом тура — «прицелились, поехали»."""
    try:
        await bot.send_dice(
            settings.forum_chat_id, message_thread_id=settings.topic_games, emoji="🎯"
        )
    except Exception:  # noqa: BLE001
        pass


# --- Тексты тура ---


def _question_text(
    state: q.QuizState, *, warn: bool = False, hint: bool = False
) -> str:
    num = state.index + 1
    # Знаменатель — фактический размер тура (вопрос мог быть снят при
    # синхронизации базы; константа «/15» давала «неверный подсчёт» номеров).
    total = len(state.question_ids)
    length_hint = q.answer_length_hint(state.current_answer)
    seconds = q.question_seconds(state.question_text)
    text = (
        f"❓ Вопрос {num}/{total}\n"
        f"━━━━━━━━━━━━\n"
        f"{state.question_text}\n\n"
        f"💡 Ответ: {length_hint} • {seconds} сек"
    )
    if hint:
        mask = q.hint_mask(state.current_answer)
        if mask:
            text += f"\n🔤 Подсказка: {mask}"
    if warn:
        text += "\n\n⚡ Осталось 10 секунд!"
    return text


def _reveal_text(state: q.QuizState, winner_name: str | None) -> str:
    if winner_name:
        head = f"✅ {winner_name} угадал(а)! +{q.COINS_PER_CORRECT} 🪙"
    else:
        head = "⏰ Никто не успел"
    text = f"{head}\nПравильный ответ: {state.current_answer}"
    if state.current_comment:
        text += f"\n💬 {state.current_comment}"
    return text


def _final_text(scores: dict) -> str:
    winners, best = q.winners_from_scores(scores)
    lines = ["🏁 Викторина окончена!", "━━━━━━━━━━━━"]
    if not winners:
        lines.append("Сегодня никто не набрал очков. В следующий раз повезёт! 🍀")
        return "\n".join(lines)
    # Итоговая таблица (топ по правильным).
    ranked = sorted(scores.items(), key=lambda kv: int(kv[1].get("correct", 0)), reverse=True)
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    for i, (uid, entry) in enumerate(ranked[:5]):
        mark = medals.get(i, f"{i + 1}.")
        lines.append(f"{mark} {entry.get('name') or uid} — {int(entry.get('correct', 0))} верных")
    names = ", ".join(w[1] for w in winners)
    lines.append(f"\n🏆 Победитель тура: {names} (+{q.WINNER_BONUS} 🪙)")
    lines.append("Монеты начислены. До завтра, в 20:00! 🧠")
    return "\n".join(lines)


# --- Driver тура: единственный писатель переходов ---


async def _run_quiz(bot: Bot, chat_id: int) -> None:
    """Ведёт тур от текущего вопроса до конца. Возобновляемый после рестарта:
    берёт состояние из БД, доигрывает остаток времени по question_started_at."""
    try:
        while True:
            async for session in get_session():
                state = await q.load_session(session, chat_id)
                await session.commit()
                break
            else:
                return
            if state is None:
                return
            # Финиш обрабатывает ТОЛЬКО driver и ТОЛЬКО вне лока — иначе
            # реентрантный дедлок (finish берёт тот же _lock_for).
            if state.phase == "finished":
                await _finish_quiz(bot, chat_id)
                return

            if state.phase == "asking":
                # Вопрос уже забрали (гонка или возобновление после ответа) —
                # закрываем сразу, не досиживая таймер.
                if state.winner_user_id is not None:
                    await _close_question(bot, chat_id)
                    continue
                remaining = _remaining_seconds(state)
                if remaining <= 0:
                    await _close_question(bot, chat_id)
                    continue
                # Событие чистится при ПОДГОТОВКЕ вопроса (_advance/_launch), а не
                # здесь — иначе set() от быстрого ответа, пришедший до этого места,
                # терялся бы, и driver зря ждал полный таймер (потерянное пробуждение).
                await _wait_with_stages(bot, chat_id, state, remaining)
                await _close_question(bot, chat_id)
                continue

            if state.phase == "break":
                await asyncio.sleep(q.BREAK_SECONDS)
                await _advance_question(bot, chat_id)
                continue
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("QUIZ: driver тура упал (chat=%s).", chat_id, exc_info=True)
    finally:
        _running.pop(chat_id, None)


def _remaining_seconds(state: q.QuizState) -> float:
    total = q.question_seconds(state.question_text)
    if not state.question_started_at:
        return float(total)
    try:
        started = datetime.fromisoformat(state.question_started_at)
    except ValueError:
        return float(total)
    from app.utils.time import ensure_aware
    elapsed = (datetime.now(timezone.utc) - ensure_aware(started)).total_seconds()
    return max(0.0, total - elapsed)


# Сколько секунд должно ОСТАВАТЬСЯ до конца вопроса, чтобы сработало
# предупреждение «⚡ Осталось 10 секунд!».
_WARN_LEFT = 10.0


def _stages(total: int) -> list[tuple[float, str]]:
    """Вехи вопроса по убыванию «осталось секунд»: сначала подсказка, потом
    предупреждение. Веха включается, только если умещается в вопрос с запасом
    (тесты гоняют вопросы по 1-3 секунды — там вех нет вовсе)."""
    stages = [
        (float(total - q.hint_at_seconds(total)), "hint"),
        (_WARN_LEFT, "warn"),
    ]
    return sorted(
        [(left, kind) for left, kind in stages if total > left + 1],
        key=lambda s: -s[0],
    )


async def _wait_with_stages(
    bot: Bot, chat_id: int, state: q.QuizState, remaining: float
) -> bool:
    """Ждёт ответ до конца вопроса, по пути открывая подсказку и предупреждение.

    Возвращает True, если вопрос забрали. Вехи, которые уже прошли (driver
    возобновлён после рестарта посреди вопроса), догоняются одной правкой —
    последней из пройденных: предупреждение показывает и подсказку тоже.
    """
    stages = _stages(q.question_seconds(state.question_text))
    passed = [kind for left, kind in stages if remaining <= left + 0.5]
    if passed:
        await _reveal_stage(bot, chat_id, passed[-1])
    for left, kind in stages:
        if remaining <= left + 0.5:
            continue
        try:
            await asyncio.wait_for(_event_for(chat_id).wait(), timeout=remaining - left)
            return True
        except asyncio.TimeoutError:
            remaining = left
            await _reveal_stage(bot, chat_id, kind)
    try:
        await asyncio.wait_for(_event_for(chat_id).wait(), timeout=max(remaining, 0.0))
        return True
    except asyncio.TimeoutError:
        return False


async def _reveal_stage(bot: Bot, chat_id: int, kind: str) -> None:
    """Правит сообщение вопроса на вехе (если его ещё никто не взял):
    «hint» — открывает первую букву и длину ответа, «warn» — дописывает
    «⚡ Осталось 10 секунд!» (подсказка к этому моменту уже открыта).
    Живая динамика без лишних сообщений в теме."""
    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            await session.commit()
            break
        else:
            return
    if (
        state is not None
        and state.phase == "asking"
        and state.winner_user_id is None
        and state.board_message_id
    ):
        await _safe_edit(
            bot,
            state.board_message_id,
            _question_text(state, hint=True, warn=(kind == "warn")),
        )


async def _close_question(bot: Bot, chat_id: int) -> None:
    """Показывает верный ответ и переводит тур в паузу или к финишу."""
    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            if state is None or state.phase != "asking":
                await session.commit()
                return
            winner_name = None
            if state.winner_user_id is not None:
                entry = state.scores.get(str(state.winner_user_id))
                winner_name = entry.get("name") if entry else None
            reveal = _reveal_text(state, winner_name)
            is_last = state.index >= len(state.question_ids) - 1
            # Только помечаем фазу; сам финиш зовёт driver вне лока (анти-дедлок).
            state.phase = "finished" if is_last else "break"
            await q.save_session(session, chat_id, settings.topic_games, state)
            await session.commit()
            break
        else:
            return

    await _safe_send(bot, reveal)


async def _advance_question(bot: Bot, chat_id: int) -> None:
    """Готовит следующий вопрос и публикует его. Финиш НЕ зовёт — только ставит
    phase=finished, а driver его подхватит вне лока (иначе реентрантный дедлок)."""
    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            if state is None or state.phase != "break":
                await session.commit()
                return
            state.index += 1
            if state.index >= len(state.question_ids):
                state.phase = "finished"
                await q.save_session(session, chat_id, settings.topic_games, state)
                await session.commit()
                return
            question = await q.get_question(session, state.question_ids[state.index])
            if question is None:
                # Вопрос пропал из БД (синхронизация базы во время тура) —
                # вычёркиваем из списка, НЕ съедая номер: нумерация остаётся
                # сплошной («Вопрос 3/14», а не скачок 3→5 из 15).
                state.question_ids.pop(state.index)
                state.index -= 1  # следующий заход возьмёт этот же индекс
                await q.save_session(session, chat_id, settings.topic_games, state)
                await session.commit()
                return
            state.phase = "asking"
            state.current_answer = question.answer
            state.current_comment = question.comment or ""
            state.question_text = question.question
            state.winner_user_id = None
            state.question_started_at = datetime.now(timezone.utc).isoformat()
            _event_for(chat_id).clear()  # свежее событие под новый вопрос
            await q.save_session(session, chat_id, settings.topic_games, state)
            await session.commit()
            text = _question_text(state)
            break
        else:
            return
    msg = await _safe_send(bot, text)
    if msg is not None:
        await _remember_question_message(chat_id, msg.message_id)


async def _remember_question_message(chat_id: int, message_id: int) -> None:
    """Сохраняет message_id вопроса в состоянии — для предупреждения за 10 сек."""
    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            if state is not None and state.phase == "asking":
                state.board_message_id = message_id
                await q.save_session(session, chat_id, settings.topic_games, state)
            await session.commit()
            return


async def _finish_quiz(bot: Bot, chat_id: int) -> None:
    """Начисляет монеты победителям, пишет историю, публикует итоги."""
    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            if state is None:
                await session.commit()
                return
            winners, best = q.winners_from_scores(state.scores)
            winner_ids = {w[0] for w in winners}
            # Бонус победителям (монеты за верные ответы уже начислены по ходу тура).
            for uid in winner_ids:
                entry = state.scores.get(str(uid))
                stats = await get_or_create_stats(
                    session, uid, chat_id, display_name=entry.get("name") if entry else None
                )
                stats.coins += q.WINNER_BONUS
            await q.record_round(
                session, chat_id=chat_id, scores=state.scores,
                winner_ids=winner_ids, winner_bonus=q.WINNER_BONUS,
            )
            final = _final_text(state.scores)
            await q.delete_session(session, chat_id)
            await session.commit()
            break
        else:
            return
    await _safe_send(bot, final)


# --- Старт тура ---


async def _launch_quiz(bot: Bot, chat_id: int) -> str | None:
    """Создаёт сессию и публикует первый вопрос. Возврат — причина отказа или None."""
    # Новый тур — счётчик подсказок с нуля (номера вопросов начинаются заново).
    _near_hint_at.pop(chat_id, None)
    async with _lock_for(chat_id):
        async for session in get_session():
            existing = await q.load_session(session, chat_id)
            if existing is not None and existing.phase != "finished":
                await session.commit()
                return "Викторина уже идёт."
            questions = await q.pick_questions(session, q.QUESTIONS_PER_ROUND)
            if len(questions) < q.QUESTIONS_PER_ROUND:
                # НЕ коммитим: pick_questions уже пометил used_at, но тур не
                # стартовал — откатываем, чтобы не «сжигать» свежесть вопросов зря.
                await session.rollback()
                return "Недостаточно вопросов в базе для тура."
            first = questions[0]
            state = q.QuizState(
                phase="asking",
                question_ids=[qq.id for qq in questions],
                index=0,
                current_answer=first.answer,
                current_comment=first.comment or "",
                question_text=first.question,
                question_started_at=datetime.now(timezone.utc).isoformat(),
            )
            _event_for(chat_id).clear()  # свежее событие под первый вопрос
            await q.save_session(session, chat_id, settings.topic_games, state)
            await session.commit()
            text = _question_text(state)
            break
        else:
            return "Не удалось открыть сессию."

    intro = (
        "🧠 Викторина начинается!\n"
        f"{q.QUESTIONS_PER_ROUND} вопросов, на середине каждого — подсказка "
        "с первой буквой. Первый верный ответ забирает вопрос. Поехали!"
    )
    await _send_start_animation(bot)  # 🎯 анимированный дротик — «прицелились»
    await _safe_send(bot, intro)
    msg = await _safe_send(bot, text)
    if msg is not None:
        await _remember_question_message(chat_id, msg.message_id)
    _start_driver(bot, chat_id)
    return None


def _start_driver(bot: Bot, chat_id: int) -> None:
    """Запускает driver-таск, если он ещё не бежит для этого чата."""
    task = _running.get(chat_id)
    if task is not None and not task.done():
        return
    _running[chat_id] = asyncio.create_task(_run_quiz(bot, chat_id))


# --- Приём ответов (обычные сообщения в теме игр) ---


@router.message(_is_games_topic_answer)
async def on_answer(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return
    text = message.text or ""
    user_id = message.from_user.id
    chat_id = message.chat.id
    outcome: str | None = None  # correct | near | wrong (для реакции вне лока)
    question_index = -1  # номер вопроса на момент ответа (для «одна подсказка на вопрос»)

    async with _lock_for(chat_id):
        async for session in get_session():
            state = await q.load_session(session, chat_id)
            if state is None or state.phase != "asking":
                await session.commit()
                return
            if state.winner_user_id is not None:
                await session.commit()  # вопрос уже забрали — молча игнор
                return
            if not q.check_answer(state.current_answer, text):
                await session.commit()  # неверно — попытку НЕ жжём (фикс старой версии)
                question_index = state.index
                # «Почти» (совпало значимое слово многословного эталона) —
                # отдельная реакция: игрок видит, что надо уточнить, а не гадать
                # заново. Пара «эталон → ответ» ложится в БД (не только в лог):
                # по ней в /аналитика видно, справедливо ли бот отказывает.
                outcome = "near" if q.is_near_miss(state.current_answer, text) else "wrong"
                await q.record_answer_miss(
                    session,
                    chat_id=chat_id,
                    question_id=state.current_question_id(),
                    correct_answer=state.current_answer,
                    given_text=text,
                    verdict=outcome,
                )
                await session.commit()
                logger.info(
                    "QUIZ_%s: эталон=%r ответ=%r",
                    outcome.upper(), state.current_answer[:80], text[:80],
                )
                break
            # Первый верный: фиксируем победителя, начисляем монеты, будим driver.
            name = _display_name(message)
            state.winner_user_id = user_id
            key = str(user_id)
            entry = state.scores.get(key, {"name": name, "correct": 0})
            entry["name"] = name or entry.get("name")
            entry["correct"] = int(entry.get("correct", 0)) + 1
            state.scores[key] = entry
            stats = await get_or_create_stats(session, user_id, chat_id, display_name=name)
            stats.coins += q.COINS_PER_CORRECT
            await q.save_session(session, chat_id, settings.topic_games, state)
            await session.commit()
            outcome = "correct"
            break
        else:
            return

    # Реакции и пробуждение driver'а — вне лока (Telegram-вызовы под локом
    # тормозили бы приём других ответов; см. вечер флуд-контроля блэкджека).
    if outcome == "correct":
        _event_for(chat_id).set()  # driver прекращает ждать и закрывает вопрос
        await _safe_react(bot, message, random.choice(_CORRECT_REACTIONS))
    elif outcome == "near":
        await _safe_react(bot, message, _NEAR_REACTION)
        # Реплика-подсказка — одна на вопрос: первому, кто оказался рядом.
        # Остальным в этом же вопросе достаётся только реакция, иначе при
        # нескольких «почти» бот засоряет тему.
        if _near_hint_at.get(chat_id) != question_index:
            _near_hint_at[chat_id] = question_index
            await _safe_reply(message, _pick_near_hint(chat_id))
    elif outcome == "wrong":
        await _safe_react(bot, message, _WRONG_REACTION)


# --- Команды ---


@router.message(Command("викторина_правила", "quiz_rules"))
async def cmd_rules(message: Message) -> None:
    if not _in_games_topic(message):
        return
    await message.reply(RULES_TEXT)


@router.message(Command("викторина_топ", "quiz_top"))
async def cmd_top(message: Message) -> None:
    if not _in_games_topic(message):
        return
    async for session in get_session():
        rows = await q.get_alltime_leaderboard(session, message.chat.id)
        await session.commit()
        break
    else:
        return
    if not rows:
        await message.reply("Пока нет сыгранных викторин. Первая — сегодня в 20:00!")
        return
    lines = ["🧠 Знатоки викторины (за всё время)", "━━━━━━━━━━━━"]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    for i, (name, correct, wins) in enumerate(rows):
        mark = medals.get(i, f"{i + 1}.")
        lines.append(f"{mark} {name} — {correct} верных, побед {wins}")
    await message.reply("\n".join(lines))


@router.message(Command("quiz_start"))
async def cmd_quiz_start(message: Message, bot: Bot) -> None:
    """Ручной старт (админ, в теме игр) — на случай теста или пропуска автозапуска."""
    from app.utils.admin import is_admin
    if not _in_games_topic(message) or message.from_user is None:
        return
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    reason = await _launch_quiz(bot, message.chat.id)
    if reason:
        await message.reply(reason)


# --- Закрытие викторины при исчерпании базы (решение владельца) ---

_EXHAUSTED_FLAG = "quiz_bank_exhausted"


async def _bank_is_exhausted() -> bool:
    """True, если свежих вопросов меньше, чем нужно на тур."""
    async for session in get_session():
        fresh = await q.count_fresh_questions(session)
        await session.commit()
        return fresh < q.QUESTIONS_PER_ROUND
    return True


async def _exhausted_already_announced() -> bool:
    from app.models import MigrationFlag
    async for session in get_session():
        flag = await session.get(MigrationFlag, _EXHAUSTED_FLAG)
        await session.commit()
        return flag is not None
    return True


async def _mark_exhausted_announced() -> None:
    from app.models import MigrationFlag
    async for session in get_session():
        session.add(MigrationFlag(key=_EXHAUSTED_FLAG))
        await session.commit()
        return


# --- Scheduler-джобы ---


async def announce_quiz_soon(bot: Bot) -> None:
    """19:55 ежедневно: случайное приглашение. При исчерпанной базе молчит —
    иначе будет «анонс был, игры не было»."""
    if settings.topic_games is None:
        return
    try:
        if await _bank_is_exhausted():
            return
        await _safe_send(bot, _pick_invitation())
    except Exception:
        logger.warning("QUIZ: анонс не отправился.", exc_info=True)


async def start_quiz_auto(bot: Bot) -> None:
    """20:00 ежедневно: автозапуск тура.

    База кончилась → ОДИН раз сообщаем владельцу и жителям, ставим флаг и
    закрываем викторину (решение владельца: вопросы не повторяются). Прочие
    отказы — громко в админ-чат (молчаливый лог уже стоил пропущенного вечера).
    """
    if settings.topic_games is None:
        return
    try:
        if await _bank_is_exhausted():
            if not await _exhausted_already_announced():
                await _mark_exhausted_announced()
                try:
                    await bot.send_message(
                        settings.admin_log_chat_id,
                        "🏁 База вопросов викторины полностью исчерпана — все вопросы "
                        "заданы, викторина закрыта.\nЧтобы возобновить: добавь вопросы "
                        "(xlsx → scripts/import_quiz_xlsx) и задеплой.",
                    )
                except (TelegramBadRequest, TelegramRetryAfter):
                    pass
                await _safe_send(
                    bot,
                    "🏁 Викторина сыграла все свои вопросы — спасибо, знатоки!\n"
                    "Вернёмся с новой базой. Следите за анонсами 🧠",
                )
            return
        reason = await _launch_quiz(bot, settings.forum_chat_id)
        if reason:
            logger.warning("QUIZ: автозапуск пропущен — %s", reason)
            try:
                await bot.send_message(
                    settings.admin_log_chat_id,
                    f"⚠️ Викторина в 20:00 не запустилась: {reason}",
                )
            except (TelegramBadRequest, TelegramRetryAfter):
                pass
    except Exception:
        logger.warning("QUIZ: автозапуск упал.", exc_info=True)


async def quiz_watchdog(bot: Bot) -> None:
    """Каждую минуту: возобновляет driver после рестарта бота и закрывает
    зависшие сессии (единственная страховка от потери тура в памяти)."""
    if settings.topic_games is None:
        return
    try:
        async for session in get_session():
            active = await q.get_active_chat_ids(session)
            await session.commit()
            break
        else:
            return
        now = datetime.now(timezone.utc)
        for chat_id, _topic in active:
            task = _running.get(chat_id)
            if task is not None and not task.done():
                continue  # driver жив
            # Проверяем свежесть; зависшую (>10 мин без прогресса) — закрываем.
            async for session in get_session():
                state = await q.load_session(session, chat_id)
                await session.commit()
                break
            else:
                continue
            if state is None:
                continue
            if state.phase == "finished" or state.is_stale(now):
                logger.info("QUIZ: watchdog закрывает сессию chat=%s (phase=%s).",
                            chat_id, state.phase)
                await _finish_quiz(bot, chat_id)
            else:
                logger.info("QUIZ: watchdog возобновляет driver chat=%s.", chat_id)
                _start_driver(bot, chat_id)
    except Exception:
        logger.warning("QUIZ: watchdog упал.", exc_info=True)
