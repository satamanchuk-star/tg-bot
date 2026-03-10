"""Точка входа: ETL-пайплайн инфраструктурного каталога."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import CENTER_LAT, CENTER_LON, SEARCH_RADIUS_KM, RAW_DIR, PROCESSED_DIR, REPORTS_DIR, SQL_DIR
from .logging_config import setup_logging
from .models import InfraObject, RawObject, ValidationIssue
from .core.geo import haversine_km
from .core.normalizers import (
    normalize_name, normalize_address, normalize_phone,
    normalize_website, normalize_work_time, normalize_text,
)
from .core.classifier import classify
from .core.validators import validate
from .core.deduplicator import deduplicate
from .exporters.csv_exporter import export_csv, export_issues_csv
from .exporters.json_exporter import export_json, export_category_dict
from .exporters.xlsx_exporter import export_xlsx
from .exporters.sql_exporter import export_sql_seed
from .providers import StaticLoaderProvider

logger = logging.getLogger(__name__)


def build_providers(
    input_dir: Path,
    provider_names: list[str] | None = None,
) -> list:
    """Создать список активных провайдеров."""
    from .providers.yandex_maps import YandexMapsProvider
    from .providers.google_maps import GoogleMapsProvider
    from .providers.gis2 import Gis2Provider
    from .providers.official_sites import OfficialSitesProvider
    from .providers.regional_portals import RegionalPortalsProvider

    all_providers = {
        "static": lambda: StaticLoaderProvider(input_dir),
        "yandex_maps": lambda: YandexMapsProvider(),
        "google_maps": lambda: GoogleMapsProvider(),
        "2gis": lambda: Gis2Provider(),
        "official": lambda: OfficialSitesProvider(),
        "regional_portal": lambda: RegionalPortalsProvider(),
    }

    if provider_names:
        return [all_providers[n]() for n in provider_names if n in all_providers]

    return [factory() for factory in all_providers.values()]


def transform_raw(
    raw: RawObject,
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> tuple[InfraObject | None, list[ValidationIssue]]:
    """Преобразовать RawObject -> InfraObject + issues."""
    issues: list[ValidationIssue] = []

    name = normalize_name(raw.raw_name)
    address = normalize_address(raw.raw_address)

    if not name:
        issues.append(ValidationIssue(
            provider=raw.source_name, raw_name=raw.raw_name,
            raw_address=raw.raw_address, reason="missing_name",
            details="Пустое название после нормализации",
        ))
        return None, issues

    # Координаты
    lat, lon = raw.raw_lat, raw.raw_lon
    if lat is None or lon is None:
        if address:
            # TODO: подключить geocoder
            issues.append(ValidationIssue(
                provider=raw.source_name, raw_name=name,
                raw_address=address, reason="no_coordinates",
                details="Координаты отсутствуют, геокодирование не реализовано",
            ))
        else:
            issues.append(ValidationIssue(
                provider=raw.source_name, raw_name=name,
                raw_address=address, reason="no_coordinates",
                details="Нет ни координат, ни адреса",
            ))
        return None, issues

    # Расстояние
    distance = haversine_km(lat, lon, center_lat, center_lon)
    if distance > radius_km:
        issues.append(ValidationIssue(
            provider=raw.source_name, raw_name=name,
            raw_address=address, reason="outside_radius",
            details=f"Расстояние {distance:.2f} км > {radius_km} км",
        ))
        return None, issues

    # Классификация
    category, subcategory = classify(
        raw.raw_name, raw.raw_type, raw.raw_category, raw.raw_subcategory,
    )
    if not category or not subcategory:
        issues.append(ValidationIssue(
            provider=raw.source_name, raw_name=name,
            raw_address=address, reason="invalid_category_mapping",
            details=f"Не удалось классифицировать: type={raw.raw_type!r}",
        ))
        return None, issues

    obj = InfraObject(
        name=name,
        category=category,
        subcategory=subcategory,
        address=address,
        phone=normalize_phone(raw.raw_phone),
        website=normalize_website(raw.raw_website),
        work_time=normalize_work_time(raw.raw_work_time),
        description=normalize_text(raw.raw_description) or None,
        lat=lat,
        lon=lon,
        distance_km=round(distance, 3),
        source=raw.source_name,
        is_active=True,
    )

    # Валидация
    obj_issues = validate(obj, provider=raw.source_name)
    issues.extend(obj_issues)

    # Если критические проблемы — не включаем
    critical = {i.reason for i in obj_issues} & {"missing_name", "invalid_category_mapping"}
    if critical:
        return None, issues

    return obj, issues


def run_pipeline(
    center_lat: float = CENTER_LAT,
    center_lon: float = CENTER_LON,
    radius_km: float = SEARCH_RADIUS_KM,
    input_dir: Path = RAW_DIR,
    output_dir: Path = PROCESSED_DIR,
    reports_dir: Path = REPORTS_DIR,
    provider_names: list[str] | None = None,
) -> list[InfraObject]:
    """Запустить полный ETL-пайплайн."""
    logger.info(
        "Старт пайплайна: центр=(%.6f, %.6f), радиус=%.1f км",
        center_lat, center_lon, radius_km,
    )

    # 1. Сбор
    providers = build_providers(input_dir, provider_names)
    all_raw: list[RawObject] = []
    for prov in providers:
        raw = prov.fetch(center_lat, center_lon, radius_km)
        logger.info("Провайдер '%s': получено %d объектов", prov.name, len(raw))
        all_raw.extend(raw)

    logger.info("Всего сырых объектов: %d", len(all_raw))

    # 2. Трансформация
    all_objects: list[InfraObject] = []
    all_issues: list[ValidationIssue] = []

    for raw in all_raw:
        obj, issues = transform_raw(raw, center_lat, center_lon, radius_km)
        all_issues.extend(issues)
        if obj:
            all_objects.append(obj)

    logger.info("После трансформации: %d объектов", len(all_objects))

    # 3. Дедупликация
    deduped, dedup_issues = deduplicate(all_objects)
    all_issues.extend(dedup_issues)

    logger.info("После дедупликации: %d объектов", len(deduped))

    # 4. Сортировка по distance_km
    deduped.sort(key=lambda o: o.distance_km)

    # 5. Экспорт
    export_csv(deduped, output_dir / "objects.csv")
    export_json(deduped, output_dir / "objects.json")
    export_xlsx(deduped, output_dir / "objects.xlsx")
    export_category_dict(output_dir / "category_dictionary.json")
    export_sql_seed(deduped, output_dir / "seed.sql")
    export_issues_csv(all_issues, reports_dir / "validation_issues.csv")

    # Статистика
    active = sum(1 for o in deduped if o.is_active)
    logger.info("=" * 50)
    logger.info("Итого: %d объектов (%d активных, %d неактивных)",
                len(deduped), active, len(deduped) - active)
    logger.info("Проблем/заметок: %d", len(all_issues))
    logger.info("Файлы сохранены в %s", output_dir)
    logger.info("=" * 50)

    return deduped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Инфраструктурный каталог — ETL-пайплайн",
    )
    parser.add_argument("--center-lat", type=float, default=CENTER_LAT)
    parser.add_argument("--center-lon", type=float, default=CENTER_LON)
    parser.add_argument("--radius-km", type=float, default=SEARCH_RADIUS_KM)
    parser.add_argument("--input-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=PROCESSED_DIR)
    parser.add_argument("--providers", nargs="*", default=None,
                        help="Список провайдеров (static, yandex_maps, google_maps, 2gis, official, regional_portal)")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(verbose=args.verbose)
    run_pipeline(
        center_lat=args.center_lat,
        center_lon=args.center_lon,
        radius_km=args.radius_km,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        provider_names=args.providers,
    )


if __name__ == "__main__":
    main()
