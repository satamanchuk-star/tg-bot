"""Почему: каталог магазина и логика покупок вынесены отдельно от хендлеров."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ShopPurchase, UserStat


@dataclass(frozen=True)
class ShopItem:
    key: str           # уникальный идентификатор, напр. "poll"
    name: str          # отображаемое название
    price: int         # стоимость в монетах
    description: str   # описание для меню
    needs_input: bool  # требуется ли ввод от пользователя после покупки


# ──────────────────────────────────────────────────────────
# КАТАЛОГ ТОВАРОВ — для добавления нового товара достаточно
# дописать элемент в список ниже
# ──────────────────────────────────────────────────────────
SHOP_CATALOG: list[ShopItem] = [
    ShopItem(
        key="poll",
        name="Организовать голосование",
        price=1000,
        description="Бот проведёт голосование по любому вашему вопросу",
        needs_input=True,
    ),
]


def get_item(key: str) -> ShopItem | None:
    """Найти товар по ключу."""
    return next((item for item in SHOP_CATALOG if item.key == key), None)


async def record_purchase(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    user_name: str | None,
    item_key: str,
    coins_spent: int,
    details: dict | None = None,
) -> ShopPurchase:
    """Сохранить покупку в базу."""
    purchase = ShopPurchase(
        user_id=user_id,
        chat_id=chat_id,
        user_name=user_name,
        item_key=item_key,
        coins_spent=coins_spent,
        details_json=json.dumps(details, ensure_ascii=False) if details else None,
    )
    session.add(purchase)
    return purchase
