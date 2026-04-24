"""Почему: централизуем работу с Google Sheets — запись предложений и импорт мест."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_SUGGESTIONS_WORKSHEET = "Предложения"

# Заголовки листа предложений
_SUGGESTION_HEADERS = [
    "Дата", "Название", "Категория", "Адрес", "Описание",
    "Телефон", "Сайт", "Пользователь", "User ID", "Статус",
]


def _get_client() -> gspread.Client:
    """Создаёт аутентифицированный клиент gspread."""
    creds = Credentials.from_service_account_file(
        settings.google_service_account_file,
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _ensure_suggestions_sheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Возвращает лист «Предложения», создаёт его если нет."""
    try:
        ws = spreadsheet.worksheet(_SUGGESTIONS_WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=_SUGGESTIONS_WORKSHEET,
            rows=1000,
            cols=len(_SUGGESTION_HEADERS),
        )
        ws.append_row(_SUGGESTION_HEADERS, value_input_option="USER_ENTERED")
        logger.info("SHEETS: создан лист «%s»", _SUGGESTIONS_WORKSHEET)
    return ws


def _write_suggestion_sync(
    name: str,
    category: str,
    address: str,
    description: str,
    phone: str,
    website: str,
    user_name: str,
    user_id: int,
) -> None:
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheets_spreadsheet_id)
    ws = _ensure_suggestions_sheet(spreadsheet)

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
    row: list[Any] = [
        now, name, category, address, description,
        phone, website, user_name, str(user_id), "Новое",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info("SHEETS: добавлено предложение '%s' от user_id=%s", name, user_id)


async def write_suggestion(
    *,
    name: str,
    category: str,
    address: str,
    description: str = "",
    phone: str = "",
    website: str = "",
    user_name: str = "",
    user_id: int = 0,
) -> None:
    """Записывает предложение жителя в лист «Предложения» Google Sheets."""
    if not settings.google_service_account_file:
        logger.info("SHEETS: write_suggestion пропущен — GOOGLE_SERVICE_ACCOUNT_FILE не задан")
        return
    await asyncio.to_thread(
        _write_suggestion_sync,
        name, category, address, description, phone, website, user_name, user_id,
    )


async def sync_places_from_sheet(*, dry_run: bool = False) -> dict[str, int]:
    """Импортирует места из Google Sheets в БД. Возвращает статистику."""
    if not settings.google_service_account_file:
        logger.info("SHEETS: sync пропущен — GOOGLE_SERVICE_ACCOUNT_FILE не задан")
        return {}
    try:
        from scripts.import_places_from_google_sheets import run_import
        stats = await run_import(dry_run=dry_run)
        return {
            "created": stats.created,
            "updated": stats.updated,
            "skipped": stats.skipped,
            "errors": stats.errors,
        }
    except Exception:
        logger.exception("SHEETS: ошибка импорта мест из Google Sheets")
        return {}
