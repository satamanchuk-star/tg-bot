from app.services.ai_module import (
    detect_profanity,
    is_assistant_topic_allowed,
    local_quiz_answer_decision,
    mask_personal_data,
    normalize_for_profanity,
    parse_quiz_answer_response,
)


def test_detects_masked_profanity() -> None:
    normalized = normalize_for_profanity("Да ты б*л_я!")
    assert detect_profanity(normalized)


def test_masks_personal_data() -> None:
    masked = mask_personal_data("Иван Иванов, +79991234567, test@example.com")
    assert "+79991234567" not in masked
    assert "test@example.com" not in masked


def test_assistant_topic_restrictions() -> None:
    assert is_assistant_topic_allowed("Как решить проблему со шлагбаумом?")
    assert not is_assistant_topic_allowed("Дай финансовый совет")


def test_parse_quiz_answer_response() -> None:
    decision = parse_quiz_answer_response({"is_correct": True, "confidence": 0.9})
    assert decision.is_correct is True
    assert decision.is_close is True


def test_local_quiz_answer_close_match() -> None:
    decision = local_quiz_answer_decision("домофон в подъезде", "домофон")
    assert decision.is_close is True
