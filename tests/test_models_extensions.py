from app.models import ChatHistory, RagMessage


def test_rag_message_has_admin_flag_field() -> None:
    record = RagMessage(chat_id=1, message_text="x", added_by_user_id=1, is_admin=False)
    assert hasattr(record, "is_admin")
    assert record.is_admin is False


def test_chat_history_has_message_field() -> None:
    row = ChatHistory(chat_id=1, user_id=1, role="user", text="t", message="m")
    assert row.message == "m"
