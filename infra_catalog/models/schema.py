"""Pydantic-модели данных."""

from __future__ import annotations
from pydantic import BaseModel, Field


class RawObject(BaseModel):
    """Сырой объект от провайдера."""
    source_name: str
    raw_name: str = ""
    raw_type: str = ""
    raw_address: str = ""
    raw_phone: str = ""
    raw_website: str = ""
    raw_work_time: str = ""
    raw_description: str = ""
    raw_lat: float | None = None
    raw_lon: float | None = None
    raw_category: str = ""
    raw_subcategory: str = ""
    raw_payload: dict = Field(default_factory=dict)


class InfraObject(BaseModel):
    """Нормализованный инфраструктурный объект."""
    name: str
    category: str
    subcategory: str
    address: str = ""
    phone: str | None = None
    website: str | None = None
    work_time: str | None = None
    description: str | None = None
    lat: float
    lon: float
    distance_km: float = 0.0
    source: str = ""
    is_active: bool = True


class ValidationIssue(BaseModel):
    """Запись о проблеме при обработке объекта."""
    provider: str = ""
    raw_name: str = ""
    raw_address: str = ""
    reason: str = ""
    details: str = ""
