"""Почему: справка и подсказки — единый источник для пользователей."""

from __future__ import annotations

import logging
import random

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message, MessageEntity

logger = logging.getLogger(__name__)
router = Router()


HELP_TEXT = (
    "Я слежу за порядком, помогаю со шлагбаумом и запускаю игры/викторины. "
    "Пиши по темам топиков и соблюдай правила чата: без мата, спама и флуда, "
    "уважай соседей."
)

MENTION_REPLIES = [
    "Я тут, на посту! Проверяю, чтобы котики не получили бан по ошибке.",
    "Шлифую правила, полирую шлагбаум — всё под контролем!",
    "Считаю монеты, чтобы не убежали из банка.",
    "Охочусь на флуд. Пока что флуд прячется!",
    "Нагреваю викторину. Вопросы уже на взлёте.",
    "Тестирую шутки. Эта прошла контроль качества.",
    "Слежу, чтобы объявления не убежали в оффтоп.",
    "Делаю вид, что отдыхаю. На самом деле модерирую.",
    "Полирую игровые карты. Блэкджек ждёт!",
    "Проверяю, кто забыл сказать «доброе утро».",
    "Сканирую чат на предмет мемов. Всё стабильно.",
    "Отвечаю на упоминания. Это моя суперсила.",
    "Поднимаю щит модерации, но улыбаюсь по-дружески.",
    "Проверяю, чтобы соседям было уютно, как в тапочках.",
    "Сверяю расписание викторин. Всё по секундам!",
    "Разгоняю пыль в чате, чтобы было чисто и весело.",
    "Взвешиваю монеты на улыбках — баланс идеален.",
    "Ищу потерянные мемы. Если найду — не отдам.",
    "Дежурю у шлагбаума, но по совместительству комик.",
    "Контролирую очередность тем. Порядок — моё второе имя.",
    "Пишу заметки о хорошем настроении. Записал твоё.",
    "Разминаю алгоритмы, чтобы отвечать быстрее.",
    "Собираю вопросы в викторину, как пазл на скорость.",
    "Приглядываю за чатом, как кот за окном.",
    "Строю мосты между темами, чтобы никто не потерялся.",
    "Охраняю тишину в ночи, чтобы всем сладко спалось.",
    "Сортирую реплики по уровню улыбок. Ты в топе.",
]


@router.message(Command("start"))
@router.message(Command("help"))
async def help_command(message: Message) -> None:
    logger.info("HANDLER: help_command")
    await message.reply(HELP_TEXT)
    logger.info("OUT: HELP_TEXT")


def _get_message_text(message: Message) -> str | None:
    """Возвращает текст сообщения или подпись, если это медиа."""
    return message.text or message.caption


def _get_message_entities(message: Message) -> list[MessageEntity]:
    """Возвращает сущности сообщения или подписи."""
    return message.entities or message.caption_entities or []


def _is_bot_mentioned(message: Message, bot_user: object) -> bool:
    """Проверяет упоминание бота по сущностям и тексту."""
    text = _get_message_text(message)
    if text is None:
        return False
    username = getattr(bot_user, "username", None)
    bot_id = getattr(bot_user, "id", None)

    for entity in _get_message_entities(message):
        if entity.type == "text_mention" and getattr(entity, "user", None):
            if bot_id is not None and entity.user.id == bot_id:
                return True
        if entity.type == "mention" and username:
            mention = text[entity.offset:entity.offset + entity.length]
            if mention.lower() == f"@{username.lower()}":
                return True

    if username and f"@{username.lower()}" in text.lower():
        return True

    return False


@router.message()
async def mention_help(message: Message, bot: Bot) -> None:
    logger.info(f"HANDLER: mention_help called, text={message.text!r}")
    me = await bot.get_me()
    if _is_bot_mentioned(message, me):
        username = getattr(me, "username", None)
        if username:
            logger.info(f"HANDLER: mention_help MATCH @{username}")
        else:
            logger.info("HANDLER: mention_help MATCH by id")
        await message.reply(random.choice(MENTION_REPLIES))
        logger.info("OUT: MENTION_REPLY")
