"""Команды генерации картинок через AI. Только для администраторов."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.config import settings
from app.services.ai_image import generate_image, is_daily_limit_reached, get_today_count
from app.utils.admin import is_admin

router = Router()
logger = logging.getLogger(__name__)

_IMAGE_COMMANDS = ("meme", "poster", "digest_image", "rules_image", "welcome_card", "warning_image")

_USAGE_EXAMPLES: dict[str, str] = {
    "meme": "/meme шлагбаум опять завис, соседи ждут",
    "poster": "/poster собрание жителей в воскресенье в 19:00",
    "digest_image": "/digest_image утренние новости ЖК",
    "rules_image": "/rules_image не шумите после 23:00",
    "welcome_card": "/welcome_card добро пожаловать в наш ЖК",
    "warning_image": "/warning_image предупреждение о парковке",
}


async def _check_admin(message: Message, bot: Bot) -> bool:
    if message.from_user is None:
        return False
    for chat_id in (settings.forum_chat_id, settings.admin_log_chat_id):
        try:
            if await is_admin(bot, chat_id, message.from_user.id):
                return True
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка проверки прав в чате %s", chat_id)
    return False


@router.message(Command(*_IMAGE_COMMANDS))
async def image_command(message: Message, bot: Bot) -> None:
    """Обрабатывает все команды генерации картинок."""
    if message.text is None:
        return

    # Определяем команду и текст
    parts = message.text.split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0]
    user_text = parts[1].strip() if len(parts) > 1 else ""

    # 1. Проверяем что генерация включена
    if not settings.ai_image_enabled:
        await message.reply("Генерация картинок сейчас отключена.")
        return

    # 2. Проверяем права администратора
    if settings.ai_image_admin_only and not await _check_admin(message, bot):
        await message.reply("Генерация картинок доступна только администраторам.")
        return

    # 3. Проверяем наличие текста
    if not user_text:
        example = _USAGE_EXAMPLES.get(cmd, f"/{cmd} <описание>")
        await message.reply(f"Укажи описание для картинки.\nПример: {example}")
        return

    # 4. Проверяем длину промпта
    if len(user_text) > settings.ai_image_max_prompt_chars:
        await message.reply(
            f"Слишком длинный запрос. Максимум {settings.ai_image_max_prompt_chars} символов, "
            f"у тебя {len(user_text)}."
        )
        return

    # 5. Проверяем дневной лимит
    if is_daily_limit_reached():
        await message.reply(
            f"Лимит генерации картинок на сегодня исчерпан ({settings.ai_image_daily_limit} шт.)."
        )
        return

    # 6. Генерируем
    status_msg = await message.reply("Генерирую картинку...")

    user_id = message.from_user.id if message.from_user else None
    try:
        image_bytes = await generate_image(cmd, user_text, chat_id=message.chat.id, user_id=user_id)
    except RuntimeError as exc:
        await status_msg.delete()
        await message.reply(f"Не удалось сгенерировать картинку: {exc}")
        return

    # 7. Отправляем как фото, fallback на документ
    await status_msg.delete()
    filename = f"{cmd}.png"
    try:
        await message.answer_photo(
            photo=BufferedInputFile(image_bytes, filename=filename),
            caption=f"/{cmd}: {user_text[:100]}",
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отправить как photo, пробуем document")
        try:
            await message.answer_document(
                document=BufferedInputFile(image_bytes, filename=filename),
                caption=f"/{cmd}: {user_text[:100]}",
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error("Не удалось отправить image document: %s", exc2)
            await message.reply("Картинка сгенерирована, но не удалось отправить в чат.")
