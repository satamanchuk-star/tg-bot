"""Справочник категорий и подкатегорий."""

CATEGORY_SUBCATEGORY: dict[str, list[str]] = {
    "medical": [
        "hospital", "clinic", "polyclinic", "dental", "diagnostic_center",
        "laboratory", "pharmacy", "private_medical_center", "veterinary",
    ],
    "food": [
        "restaurant", "cafe", "coffee_shop", "bakery", "fast_food",
        "pizzeria", "delivery_point_food",
    ],
    "government": [
        "administration", "police", "social_services", "court", "tax",
        "registry_office", "housing_services", "other_government",
    ],
    "post": [
        "russian_post", "parcel_service", "pickup_post",
    ],
    "mfc": [
        "mfc_main", "mfc_branch",
    ],
    "education": [
        "school", "kindergarten", "private_school", "private_kindergarten",
        "development_center", "additional_education",
    ],
    "mall": [
        "shopping_center", "retail_gallery", "mall_small", "mall_large",
    ],
    "grocery": [
        "supermarket", "minimarket", "hypermarket", "convenience_store", "market",
    ],
    "building_materials": [
        "hardware_store", "building_hypermarket", "finishing_materials",
        "plumbing_store", "electrical_store", "door_window_store",
        "general_building_store",
    ],
}

ALL_CATEGORIES = set(CATEGORY_SUBCATEGORY.keys())
ALL_SUBCATEGORIES = {
    sub for subs in CATEGORY_SUBCATEGORY.values() for sub in subs
}


def validate_category_pair(category: str, subcategory: str) -> bool:
    """Проверить допустимость пары category/subcategory."""
    return (
        category in CATEGORY_SUBCATEGORY
        and subcategory in CATEGORY_SUBCATEGORY[category]
    )
