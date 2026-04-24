"""Почему: emoji-реакции дают ощущение живого бота без лишних сообщений в чат."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.types import Message, ReactionTypeEmoji

from app.config import settings

logger = logging.getLogger(__name__)
router = Router()

# Cooldown: не реагируем на сообщения одного пользователя чаще раза в 10 минут
_REACTION_COOLDOWN = timedelta(minutes=10)
_LAST_REACTION: dict[int, datetime] = {}  # user_id → время последней реакции
_LAST_REACTION_MAX = 1000

# Триггерные слова по категориям
_GRATITUDE_WORDS = frozenset({
    "спасибо", "благодарю", "спс", "thanks", "thank", "сяп",
    "мерси", "благодарен", "благодарна",
})
_CONGRATS_WORDS = frozenset({
    "поздравляю", "поздравляем", "с праздником", "с днём рождения",
    "с днем рождения", "с новым годом", "с юбилеем", "ура",
})
_POSITIVE_WORDS = frozenset({
    "отлично", "супер", "классно", "здорово", "прекрасно", "замечательно",
    "огонь", "круто", "шикарно", "великолепно",
})

# Пулы реакций по триггеру
_GRATITUDE_REACTIONS = ["❤", "🙏", "👍"]
_CONGRATS_REACTIONS = ["🎉", "❤", "🥳"]
_PHOTO_REACTIONS = ["👍", "🔥", "❤"]
_POSITIVE_REACTIONS = ["👍", "🔥", "❤"]


def _is_on_cooldown(user_id: int) -> bool:
    now = datetime.now(timezone.utc)
    last = _LAST_REACTION.get(user_id)
    return last is not None and now - last < _REACTION_COOLDOWN


def _mark_reacted(user_id: int) -> None:
    if len(_LAST_REACTION) > _LAST_REACTION_MAX:
        cutoff = datetime.now(timezone.utc) - _REACTION_COOLDOWN
        expired = [uid for uid, t in _LAST_REACTION.items() if t < cutoff]
        for uid in expired:
            _LAST_REACTION.pop(uid, None)
    _LAST_REACTION[user_id] = datetime.now(timezone.utc)


def _pick_reaction(text: str, has_photo: bool) -> str | None:
    """Возвращает emoji для реакции или None если триггер не найден."""
    lower = text.lower() if text else ""

    if any(w in lower for w in _GRATITUDE_WORDS):
        return random.choice(_GRATITUDE_REACTIONS)
    if any(w in lower for w in _CONGRATS_WORDS):
        return random.choice(_CONGRATS_REACTIONS)
    if any(w in lower for w in _POSITIVE_WORDS):
        return random.choice(_POSITIVE_REACTIONS)
    if has_photo:
        return random.choice(_PHOTO_REACTIONS)
    return None


@router.message()
async def maybe_react(message: Message, bot: Bot) -> None:
    """Ставит emoji-реакцию на сообщения с триггерными словами или фото."""
    if message.chat.id != settings.forum_chat_id:
        return
    if message.from_user is None or message.from_user.is_bot:
        return
    if _is_on_cooldown(message.from_user.id):
        return

    text = message.text or message.caption or ""
    has_photo = bool(message.photo or message.video or message.document)

    emoji = _pick_reaction(text, has_photo)
    if emoji is None:
        return

    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        _mark_reacted(message.from_user.id)
        logger.info(
            "REACTION: %s user_id=%s msg_id=%s",
            emoji, message.from_user.id, message.message_id,
        )
    except Exception as exc:
        logger.debug("Не удалось поставить реакцию: %s", exc)
