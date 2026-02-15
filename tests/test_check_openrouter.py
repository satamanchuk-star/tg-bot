"""Почему: проверяем, что health-check скрипт корректно берёт настройки из окружения и .env."""

from __future__ import annotations

from pathlib import Path

from scripts import check_openrouter


def test_build_parser_reads_values_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "AI_KEY=dotenv-key\nAI_MODEL=dotenv-model\nAI_API_URL=https://example.test/v1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_KEY", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("AI_API_URL", raising=False)

    parser = check_openrouter.build_parser()
    args = parser.parse_args([])

    assert args.api_key == "dotenv-key"
    assert args.model == "dotenv-model"
    assert args.api_url == "https://example.test/v1"


def test_build_parser_prefers_process_env_over_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AI_KEY=dotenv-key\n", encoding="utf-8")
    monkeypatch.setenv("AI_KEY", "process-key")

    parser = check_openrouter.build_parser()
    args = parser.parse_args([])

    assert args.api_key == "process-key"
