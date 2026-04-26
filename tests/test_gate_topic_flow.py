"""Почему: в топике шлагбаума бот не должен запрашивать поля без явной просьбы оформить заявку."""

from app.handlers.moderation import _should_collect_gate_request


def test_gate_request_collection_requires_explicit_action_words() -> None:
    assert not _should_collect_gate_request(
        "Здравствуйте, кто знает номер ответственного за шлагбаум?"
    )
    assert not _should_collect_gate_request("Мне нужно увеличить лимит пропусков на завтра")


def test_gate_request_collection_starts_on_request_to_submit_ticket() -> None:
    assert _should_collect_gate_request("Помогите передать заявку диспетчеру по шлагбауму")
    assert _should_collect_gate_request("Можно оформить заявку? шлагбаум не открывается")
