"""Тесты чистой логики «21»: колода, руки, дилер, исходы, выплаты, состояние."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.services.blackjack import (
    BlackjackState,
    dealer_play,
    evaluate,
    hand_value,
    is_blackjack,
    new_deck,
    payout_for,
)


def test_new_deck_is_honest_52() -> None:
    deck = new_deck(random.Random(42))
    assert len(deck) == 52
    assert len(set(deck)) == 52  # все уникальны
    assert sum(1 for c in deck if c.endswith("♠")) == 13


def test_hand_value_aces() -> None:
    assert hand_value(["Т♠", "К♥"]) == 21
    assert hand_value(["Т♠", "Т♥", "9♦"]) == 21  # 11 + 1 + 9
    assert hand_value(["Т♠", "Т♥", "Т♦", "Т♣"]) == 14  # 11 + 1 + 1 + 1
    assert hand_value(["10♠", "В♥", "5♦"]) == 25  # перебор без тузов


def test_blackjack_is_strictly_two_cards() -> None:
    """Регресс бага старой версии: бонус давали за любую 21, не только за 2 карты."""
    assert is_blackjack(["Т♠", "Д♥"]) is True
    assert is_blackjack(["7♠", "7♥", "7♦"]) is False  # 21 тремя картами — не блэкджек
    assert is_blackjack(["9♠", "5♥"]) is False


def test_dealer_plays_to_17_and_does_not_mutate() -> None:
    """Регресс: старый evaluate_game добирал карты в переданный список in-place."""
    dealer = ["9♠", "7♥"]  # 16 — обязан добрать
    deck = ["5♦", "2♣"]
    final, rest = dealer_play(dealer, deck)
    assert hand_value(final) >= 17
    assert dealer == ["9♠", "7♥"]  # вход не тронут
    assert deck == ["5♦", "2♣"]

    dealer17 = ["10♠", "7♥"]  # 17 — не добирает
    final17, _ = dealer_play(dealer17, ["К♦"])
    assert final17 == dealer17


def test_evaluate_matrix() -> None:
    assert evaluate(["К♠", "Д♥", "5♦"], ["9♠", "8♥"]) == "lose"  # перебор игрока
    assert evaluate(["К♠", "9♥"], ["К♦", "Д♣", "5♠"]) == "win"  # перебор дилера
    assert evaluate(["К♠", "Д♥"], ["К♦", "9♣"]) == "win"  # 20 > 19
    assert evaluate(["К♠", "8♥"], ["К♦", "9♣"]) == "lose"  # 18 < 19
    assert evaluate(["К♠", "9♥"], ["Д♦", "9♣"]) == "push"  # 19 = 19


def test_payouts_match_rules() -> None:
    """Соответствие выплат тексту правил — в старой версии правила врали."""
    assert payout_for("win", 25, player_bj=False, dealer_bj=False) == 50  # ×2
    assert payout_for("win", 5, player_bj=True, dealer_bj=False) == 12  # ×2.5 floor
    assert payout_for("push", 25, player_bj=False, dealer_bj=False) == 25  # возврат
    assert payout_for("lose", 25, player_bj=False, dealer_bj=False) == 0
    # Оба блэкджека → ничья, возврат ставки
    assert payout_for("push", 10, player_bj=True, dealer_bj=True) == 10


def test_state_json_roundtrip() -> None:
    state = BlackjackState(
        phase="playing", bet=25, deck=["2♠"], player_hand=["К♥", "7♠"],
        dealer_hand=["9♦", "Т♠"], message_id=123,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    restored = BlackjackState.from_json(state.to_json())
    assert restored is not None
    assert restored.bet == 25
    assert restored.player_hand == ["К♥", "7♠"]
    assert restored.message_id == 123


def test_state_rejects_legacy_payload() -> None:
    """Старый формат (int-руки, без version) — мусор, партия удаляется."""
    assert BlackjackState.from_json('{"player_hand": [10, 11], "dealer_hand": [9]}') is None
    assert BlackjackState.from_json("не json") is None


def test_timeout_handles_naive_and_aware() -> None:
    now = datetime.now(timezone.utc)
    fresh = BlackjackState(phase="playing", started_at=now.isoformat())
    assert fresh.is_timed_out(now) is False
    old_naive = (now - timedelta(minutes=11)).replace(tzinfo=None).isoformat()
    stale = BlackjackState(phase="playing", started_at=old_naive)
    assert stale.is_timed_out(now) is True  # naive ISO не роняет
    assert BlackjackState(phase="betting", started_at="").is_timed_out(now) is True


def test_betting_phase_times_out_faster() -> None:
    """Стол без ставки снимается за 3 минуты (playing живёт 10) — анти-завис."""
    now = datetime.now(timezone.utc)
    four_min_ago = (now - timedelta(minutes=4)).isoformat()
    assert BlackjackState(phase="betting", started_at=four_min_ago).is_timed_out(now) is True
    assert BlackjackState(phase="playing", started_at=four_min_ago).is_timed_out(now) is False
    two_min_ago = (now - timedelta(minutes=2)).isoformat()
    assert BlackjackState(phase="betting", started_at=two_min_ago).is_timed_out(now) is False
