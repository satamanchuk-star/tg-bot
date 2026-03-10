"""Конфигурация проекта."""

from pathlib import Path

# Центральная точка — ЖК «Живописный»
CENTER_LAT = 55.525238
CENTER_LON = 37.616287
SEARCH_RADIUS_KM = 10.0

# Директории
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
REPORTS_DIR = DATA_DIR / "reports"
SQL_DIR = PROJECT_ROOT / "sql"

# Дедупликация
DEDUP_DISTANCE_THRESHOLD_M = 100  # метры
DEDUP_NAME_SIMILARITY_THRESHOLD = 0.80  # rapidfuzz ratio 0..1
