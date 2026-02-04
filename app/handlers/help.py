"""Почему: справка и подсказки — единый источник для пользователей."""

from __future__ import annotations

import logging
import random

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

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


@router.message()
async def mention_help(message: Message, bot: Bot) -> None:
    logger.info(f"HANDLER: mention_help called, text={message.text!r}")
    if message.text is None or message.text.startswith("/"):
        return
    me = await bot.get_me()
    username = me.username
    if username and f"@{username.lower()}" in message.text.lower():
        logger.info(f"HANDLER: mention_help MATCH @{username}")
        await message.reply(random.choice(MENTION_REPLIES))
        logger.info("OUT: MENTION_REPLY")
