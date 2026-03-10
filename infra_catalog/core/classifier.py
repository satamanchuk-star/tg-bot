"""Классификация объектов по category / subcategory."""

from __future__ import annotations

import re
from ..constants import CATEGORY_SUBCATEGORY, validate_category_pair

# Правила: (pattern_in_lower, category, subcategory)
_RULES: list[tuple[str, str, str]] = [
    # medical
    (r"\bбольниц", "medical", "hospital"),
    (r"\bгоспиталь", "medical", "hospital"),
    (r"\bполиклиник", "medical", "polyclinic"),
    (r"\bстоматолог", "medical", "dental"),
    (r"\bзубн", "medical", "dental"),
    (r"\bдиагностическ", "medical", "diagnostic_center"),
    (r"\bлаборатор", "medical", "laboratory"),
    (r"\bинвитро", "medical", "laboratory"),
    (r"\bгемотест", "medical", "laboratory"),
    (r"\bаптек", "medical", "pharmacy"),
    (r"\bфармац", "medical", "pharmacy"),
    (r"\bветеринар", "medical", "veterinary"),
    (r"\bветклиник", "medical", "veterinary"),
    (r"\bмедицинск", "medical", "private_medical_center"),
    (r"\bмедцентр", "medical", "private_medical_center"),
    (r"\bклиник", "medical", "clinic"),

    # food
    (r"\bпиццер", "food", "pizzeria"),
    (r"\bкофейн", "food", "coffee_shop"),
    (r"\bcoffee", "food", "coffee_shop"),
    (r"\bпекарн", "food", "bakery"),
    (r"\bхлебопекарн", "food", "bakery"),
    (r"\bфастфуд", "food", "fast_food"),
    (r"\bфаст.?фуд", "food", "fast_food"),
    (r"\bмакдональдс", "food", "fast_food"),
    (r"\bбургер.?кинг", "food", "fast_food"),
    (r"\bkfc", "food", "fast_food"),
    (r"\bсубвей", "food", "fast_food"),
    (r"\bресторан", "food", "restaurant"),
    (r"\bкафе\b", "food", "cafe"),
    (r"\bстоловая", "food", "cafe"),
    (r"\bбар\b", "food", "cafe"),

    # government
    (r"\bадминистрац", "government", "administration"),
    (r"\bполиц", "government", "police"),
    (r"\bовд\b", "government", "police"),
    (r"\bумвд", "government", "police"),
    (r"\bсоцзащит", "government", "social_services"),
    (r"\bсоциальн", "government", "social_services"),
    (r"\bсуд\b", "government", "court"),
    (r"\bсудебн", "government", "court"),
    (r"\bналогов", "government", "tax"),
    (r"\bифнс", "government", "tax"),
    (r"\bзагс", "government", "registry_office"),
    (r"\bжилищн", "government", "housing_services"),
    (r"\bуправляющ.*компан", "government", "housing_services"),

    # post
    (r"\bпочта\s*росси", "post", "russian_post"),
    (r"\bпочтов.*отделен", "post", "russian_post"),
    (r"\bcdek|сдэк", "post", "parcel_service"),
    (r"\bboxberry|боксберри", "post", "parcel_service"),
    (r"\bdpd\b", "post", "parcel_service"),
    (r"\bпункт\s*выдачи", "post", "pickup_post"),
    (r"\bпвз\b", "post", "pickup_post"),
    (r"\bwildberries|вайлдберри", "post", "pickup_post"),
    (r"\bozon\b", "post", "pickup_post"),

    # mfc
    (r"\bмфц", "mfc", "mfc_main"),
    (r"\bмногофункциональн", "mfc", "mfc_main"),

    # education
    (r"\bдетский\s*сад", "education", "kindergarten"),
    (r"\bдетсад", "education", "kindergarten"),
    (r"\bдоу\b", "education", "kindergarten"),
    (r"\bразвивающ.*центр", "education", "development_center"),
    (r"\bцентр\s*развит", "education", "development_center"),
    (r"\bшкола\b", "education", "school"),
    (r"\bгимназ", "education", "school"),
    (r"\bлицей", "education", "school"),
    (r"\bдополнительн.*образован", "education", "additional_education"),
    (r"\bкружок", "education", "additional_education"),
    (r"\bсекци[яи]", "education", "additional_education"),

    # mall
    (r"\bторгов.*центр", "mall", "shopping_center"),
    (r"\bтц\b", "mall", "shopping_center"),
    (r"\bтрц\b", "mall", "mall_large"),
    (r"\bмолл\b", "mall", "mall_large"),

    # grocery
    (r"\bгипермаркет", "grocery", "hypermarket"),
    (r"\bашан", "grocery", "hypermarket"),
    (r"\bглобус", "grocery", "hypermarket"),
    (r"\bлента\b", "grocery", "hypermarket"),
    (r"\bсупермаркет", "grocery", "supermarket"),
    (r"\bпятёрочка|\bпятерочка", "grocery", "supermarket"),
    (r"\bперекрёсток|\bперекресток", "grocery", "supermarket"),
    (r"\bмагнит\b", "grocery", "supermarket"),
    (r"\bверный\b", "grocery", "supermarket"),
    (r"\bдикси", "grocery", "supermarket"),
    (r"\bвкусвилл", "grocery", "supermarket"),
    (r"\bмини.?маркет", "grocery", "minimarket"),
    (r"\bпродукт", "grocery", "convenience_store"),
    (r"\bмагазин", "grocery", "convenience_store"),
    (r"\bрынок", "grocery", "market"),
    (r"\bярмарк", "grocery", "market"),

    # building_materials
    (r"\bлемана\s*про|леруа\s*мерлен|leroy", "building_materials", "building_hypermarket"),
    (r"\bпетрович", "building_materials", "building_hypermarket"),
    (r"\bоби\b|obi\b", "building_materials", "building_hypermarket"),
    (r"\bстройматериал", "building_materials", "general_building_store"),
    (r"\bстроительн.*магаз", "building_materials", "general_building_store"),
    (r"\bстроймаркет", "building_materials", "general_building_store"),
    (r"\bсантехник", "building_materials", "plumbing_store"),
    (r"\bэлектротовар", "building_materials", "electrical_store"),
    (r"\bэлектрик", "building_materials", "electrical_store"),
    (r"\bдвери\b.*магаз|магаз.*двер", "building_materials", "door_window_store"),
    (r"\bокна\b.*магаз|магаз.*окн", "building_materials", "door_window_store"),
    (r"\bотделочн", "building_materials", "finishing_materials"),
    (r"\bобои\b", "building_materials", "finishing_materials"),
    (r"\bплитк", "building_materials", "finishing_materials"),
]


def classify(
    raw_name: str,
    raw_type: str = "",
    raw_category: str = "",
    raw_subcategory: str = "",
) -> tuple[str, str]:
    """Определить (category, subcategory) по имени / типу / raw-значениям.

    Возвращает ("", "") если классификация не удалась.
    """
    # Если провайдер уже дал валидные значения — доверяем
    if raw_category and raw_subcategory:
        if validate_category_pair(raw_category, raw_subcategory):
            return raw_category, raw_subcategory

    text = f"{raw_name} {raw_type} {raw_category} {raw_subcategory}".lower()
    text = text.replace("ё", "е")

    for pattern, cat, subcat in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return cat, subcat

    return "", ""
