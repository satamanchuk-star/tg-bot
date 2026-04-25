"""Высокоуровневые AI-задачи: classify, generate, detect, extract.

Каждая функция:
- выбирает модель через ai_router
- вызывает OpenRouterProvider._chat_completion_with_model
- при ошибке возвращает безопасный дефолт (бот не падает)
- логирует в AiTaskLog
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db import get_session
from app.models import AiTaskLog
from app.services.ai_module import get_admin_notifier, get_ai_client
from app.services.ai_router import get_max_tokens_for_task, get_model_for_task, get_temperature_for_task
from app.services.ai_schemas import (
    GateIntentResult,
    GateRequestResult,
    ModerationResult,
    SpamResult,
    TopicResult,
)
from app.utils.time import now_tz

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Внутренние утилиты
# ---------------------------------------------------------------------------

def _parse_json_safe(text: str, default: dict) -> dict:
    """Пытается распарсить JSON из ответа модели, при ошибке возвращает default."""
    text = text.strip()
    # Модели иногда оборачивают JSON в ```json ... ```
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Ищем первую { ... } в тексте
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    logger.debug("AI: не удалось распарсить JSON: %r", text[:200])
    return default


async def _get_provider():
    """Возвращает OpenRouterProvider или None если AI недоступен."""
    client = get_ai_client()
    provider = client._provider  # noqa: SLF001
    from app.services.ai_module import OpenRouterProvider
    if not isinstance(provider, OpenRouterProvider):
        return None
    return provider


async def _log_task(
    *,
    task: str,
    model: str,
    user_id: int | None,
    chat_id: int | None,
    input_chars: int,
    output_chars: int,
    tokens: int,
    success: bool,
    error: str | None,
    cost_usd: float = 0.0,
) -> None:
    try:
        date_key = now_tz().date().isoformat()
        async for session in get_session():
            entry = AiTaskLog(
                date_key=date_key,
                task=task,
                model=model,
                user_id=user_id,
                chat_id=chat_id,
                input_chars=input_chars,
                output_chars=output_chars,
                tokens_used=tokens,
                cost_usd=cost_usd,
                success=success,
                error=error[:200] if error else None,
            )
            session.add(entry)
            await session.commit()
            break
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать AiTaskLog: %s", exc)


async def _check_daily_cost_limit(chat_id: int | None) -> bool:
    """Возвращает True если дневной лимит стоимости НЕ превышен."""
    if settings.ai_max_daily_cost_usd <= 0:
        return True
    try:
        date_key = now_tz().date().isoformat()
        async for session in get_session():
            stmt = select(func.sum(AiTaskLog.cost_usd)).where(
                AiTaskLog.date_key == date_key,
            )
            if chat_id is not None:
                stmt = stmt.where(AiTaskLog.chat_id == chat_id)
            result = await session.execute(stmt)
            total = float(result.scalar() or 0.0)
            if total >= settings.ai_max_daily_cost_usd:
                notifier = get_admin_notifier()
                if notifier:
                    try:
                        await notifier(
                            f"⚠️ AI cost limit reached\n"
                            f"Потрачено: ${total:.3f} / ${settings.ai_max_daily_cost_usd:.2f}\n"
                            f"Массовые AI-задачи приостановлены до завтра."
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка проверки cost limit: %s", exc)
        return True  # при ошибке проверки — разрешаем (fail-open)


async def _notify_admin(text: str) -> None:
    notifier = get_admin_notifier()
    if notifier:
        try:
            await notifier(text)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Классификация — дешёвые Qwen-модели
# ---------------------------------------------------------------------------

async def classify_moderation(
    text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> ModerationResult:
    """AI-классификация нарушений. При любой ошибке возвращает безопасный дефолт."""
    default = ModerationResult()
    if not settings.ai_enabled:
        return default

    provider = await _get_provider()
    if provider is None:
        return default

    if not await _check_daily_cost_limit(chat_id):
        return default

    task = "moderation"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты модератор чата жилого комплекса. Проанализируй сообщение и верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: is_violation(bool), has_profanity(bool), is_toxic(bool), is_threat(bool), "
        "category(str: none/profanity/toxicity/threat/other), confidence(float 0-1), "
        "recommended_action(str: log_only/warn/delete_and_strike), reason(str краткая причина на русском).\n"
        "Если нарушений нет — все bool=false, confidence<0.3, recommended_action=log_only."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:1000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        raw = _parse_json_safe(content, {})
        result = ModerationResult.model_validate(raw)
        if result.confidence >= 0.80 and result.recommended_action == "delete_and_strike":
            await _notify_admin(
                f"🤖 AI moderation\n"
                f"Model: {model}\n"
                f"User: {user_id}\nConfidence: {result.confidence:.2f}\n"
                f"Action: {result.recommended_action}\nText: {text[:300]}"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI classify_moderation error: %s", exc)
        await _notify_admin(f"⚠️ AI error\nTask: {task}\nModel: {model}\nError: {error_msg}")
        return default
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def classify_spam(
    text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> SpamResult:
    """AI-классификация спама и внешних ссылок."""
    default = SpamResult()
    if not settings.ai_enabled:
        return default

    provider = await _get_provider()
    if provider is None:
        return default

    if not await _check_daily_cost_limit(chat_id):
        return default

    task = "spam"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты фильтр спама для чата ЖК. Проанализируй сообщение и верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: is_spam(bool), has_external_link(bool), "
        "category(str: none/ad/phishing/offtopic/external_link), confidence(float 0-1), "
        "recommended_action(str: log_only/delete), reason(str на русском).\n"
        "Telegram-ссылки и @mention — НЕ спам. Спам — реклама, фишинг, нерелевантные внешние ссылки."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:1000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        raw = _parse_json_safe(content, {})
        return SpamResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI classify_spam error: %s", exc)
        return default
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def classify_topic(
    text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> TopicResult:
    """Определяет, в какую тему форума лучше написать это сообщение."""
    default = TopicResult()
    if not settings.ai_enabled:
        return default

    provider = await _get_provider()
    if provider is None:
        return default

    if not await _check_daily_cost_limit(chat_id):
        return default

    task = "topic"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    # Строим список доступных тем из settings
    topic_map = {
        "gate": ("Шлагбаум", settings.topic_gate),
        "repair": ("Ремонт", settings.topic_repair),
        "complaints": ("Жалобы", settings.topic_complaints),
        "pets": ("Питомцы", settings.topic_pets),
        "parents": ("Мамы и папы", settings.topic_parents),
        "realty": ("Недвижимость", settings.topic_realty),
        "uk": ("УК", settings.topic_uk),
        "market": ("Барахолка", settings.topic_market),
        "neighbors": ("Соседи", settings.topic_neighbors),
        "smoke": ("Курилка", settings.topic_smoke),
    }
    topics_str = ", ".join(f"{k}({v[0]})" for k, v in topic_map.items() if v[1] is not None)

    system_prompt = (
        f"Ты помощник форума ЖК. Доступные темы: {topics_str}.\n"
        "Проанализируй сообщение и верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: topic_key(str: одно из ключей выше или пустая строка), "
        "topic_title(str: название темы), confidence(float 0-1), "
        "suggestion_text(str: мягкая подсказка на русском, 1 предложение).\n"
        "Если сообщение не относится ни к одной теме — topic_key='', confidence<0.3."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:500]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        raw = _parse_json_safe(content, {})
        return TopicResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI classify_topic error: %s", exc)
        return default
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def detect_gate_intent(
    text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> GateIntentResult:
    """Определяет, связано ли сообщение с проблемой шлагбаума."""
    default = GateIntentResult()
    if not settings.ai_enabled:
        return default

    provider = await _get_provider()
    if provider is None:
        return default

    task = "gate_intent"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты ассистент ЖК. Определи: связано ли это сообщение с проблемой шлагбаума, "
        "въезда/выезда, пропуска, парковочного барьера. Верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: is_gate_problem(bool), confidence(float 0-1), reason(str на русском)."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:500]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        raw = _parse_json_safe(content, {})
        return GateIntentResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI detect_gate_intent error: %s", exc)
        return default
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def extract_gate_request(
    text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> GateRequestResult:
    """Извлекает поля заявки по шлагбауму: дата/время, номер авто, описание проблемы."""
    default = GateRequestResult(
        missing_fields=["дата и время", "номер автомобиля", "описание проблемы"]
    )
    if not settings.ai_enabled:
        return default

    provider = await _get_provider()
    if provider is None:
        return default

    task = "gate_extract"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты ассистент ЖК. Извлеки из сообщения данные о проблеме со шлагбаумом. "
        "Верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: date_time(str|null: дата и время проблемы), "
        "car_number(str|null: номер авто), "
        "in_pass_base(str|null: 'да'/'нет'/'неизвестно' — есть ли номер в базе пропусков), "
        "problem_description(str: краткое описание), "
        "missing_fields(list[str]: список полей которые НЕ указаны в сообщении), "
        "confidence(float 0-1).\n"
        "Обязательные поля: date_time, car_number, problem_description. "
        "Если поле не указано — добавь его в missing_fields."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:800]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        raw = _parse_json_safe(content, {})
        return GateRequestResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI extract_gate_request error: %s", exc)
        return default
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


# ---------------------------------------------------------------------------
# Генерация ответов — DeepSeek / Claude
# ---------------------------------------------------------------------------

async def generate_reply(
    question: str,
    *,
    chat_id: int,
    user_id: int | None = None,
    context: str | None = None,
) -> str:
    """Генерирует ответ на вопрос жителя через ai_reply_model."""
    if not settings.ai_enabled:
        return "AI временно недоступен."

    provider = await _get_provider()
    if provider is None:
        return "AI временно недоступен."

    if not await _check_daily_cost_limit(chat_id):
        return "Дневной лимит AI исчерпан."

    task = "reply"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты вежливый ассистент жилого комплекса. Отвечай кратко, по делу, на русском. "
        "Не выдумывай факты."
    )
    user_content = question if not context else f"Контекст:\n{context}\n\nВопрос: {question}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content[:2000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        return content
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI generate_reply error: %s", exc)
        await _notify_admin(f"⚠️ AI error\nTask: {task}\nModel: {model}\nError: {error_msg}")
        return "Не удалось получить ответ от AI. Попробуйте позже."
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(question), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def generate_premium_reply(
    question: str,
    *,
    chat_id: int,
    user_id: int | None = None,
    context: str | None = None,
) -> str:
    """Генерирует аккуратный ответ через ai_premium_model (Claude Haiku)."""
    if not settings.ai_enabled:
        return "AI временно недоступен."

    provider = await _get_provider()
    if provider is None:
        return "AI временно недоступен."

    if not await _check_daily_cost_limit(chat_id):
        return "Дневной лимит AI исчерпан."

    task = "premium_reply"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты опытный сотрудник управляющей компании жилого комплекса. "
        "Твоя задача — дать взвешенный, вежливый и конструктивный ответ от имени администрации. "
        "Тон: профессиональный, сочувствующий, без агрессии. Ответ на русском, до 300 слов."
    )
    user_content = question if not context else f"Контекст:\n{context}\n\nЗапрос: {question}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content[:2000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        return content
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI generate_premium_reply error: %s", exc)
        await _notify_admin(f"⚠️ AI error\nTask: {task}\nModel: {model}\nError: {error_msg}")
        return "Не удалось получить ответ от AI. Попробуйте позже."
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(question), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def generate_daily_digest(
    summary_context: str,
    *,
    chat_id: int,
) -> str:
    """Генерирует ежедневную сводку через ai_digest_model."""
    if not settings.ai_enabled:
        return ""

    provider = await _get_provider()
    if provider is None:
        return ""

    task = "digest"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты добрый ведущий новостей жилого комплекса. Напиши короткую ежедневную сводку "
        "на основе переданного контекста. Стиль: тёплый, с лёгким юмором, без токсичности, "
        "без персональных нападок, без личных данных. До 600 символов. На русском."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": summary_context[:3000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        return content
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI generate_daily_digest error: %s", exc)
        return ""
    finally:
        await _log_task(
            task=task, model=model, user_id=None, chat_id=chat_id,
            input_chars=len(summary_context), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )


async def explain_technical_error(
    error_text: str,
    *,
    chat_id: int,
    user_id: int | None = None,
) -> str:
    """Объясняет техническую ошибку через ai_code_model."""
    if not settings.ai_enabled:
        return "AI временно недоступен."

    provider = await _get_provider()
    if provider is None:
        return "AI временно недоступен."

    if not await _check_daily_cost_limit(chat_id):
        return "Дневной лимит AI исчерпан."

    task = "code_help"
    model = get_model_for_task(task)
    max_tokens = get_max_tokens_for_task(task)
    temperature = get_temperature_for_task(task)

    system_prompt = (
        "Ты технический ассистент DevOps. Объясни ошибку кратко и понятно:\n"
        "1. Что это за ошибка (1-2 предложения)\n"
        "2. Дай 2-3 конкретных шага для диагностики\n"
        "Не выполняй действий на сервере. Не раскрывай секреты и ключи. "
        "Ответ на русском, до 300 слов."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": error_text[:2000]},
    ]

    error_msg: str | None = None
    content = ""
    tokens = 0
    try:
        content, tokens = await provider._chat_completion_with_model(  # noqa: SLF001
            model, messages, chat_id=chat_id, max_tokens=max_tokens, temperature=temperature,
        )
        return content
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:200]
        logger.warning("AI explain_technical_error: %s", exc)
        await _notify_admin(f"⚠️ AI error\nTask: {task}\nModel: {model}\nError: {error_msg}")
        return f"Не удалось получить объяснение от AI: {exc}"
    finally:
        await _log_task(
            task=task, model=model, user_id=user_id, chat_id=chat_id,
            input_chars=len(error_text), output_chars=len(content),
            tokens=tokens, success=error_msg is None, error=error_msg,
        )
