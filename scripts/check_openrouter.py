"""Почему: даем быстрый автономный health-check OpenRouter без запуска Telegram-бота."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass

import httpx
from dotenv import dotenv_values


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    status_code: int | None
    latency_ms: int
    details: str


def build_parser() -> argparse.ArgumentParser:
    env_values = dotenv_values(".env")
    default_api_key = os.getenv("AI_KEY") or env_values.get("AI_KEY")
    default_model = os.getenv("AI_MODEL") or env_values.get("AI_MODEL") or "qwen/qwen3-14b"
    default_api_url = os.getenv("AI_API_URL") or env_values.get("AI_API_URL") or "https://openrouter.ai/api/v1"

    parser = argparse.ArgumentParser(description="Проверка OpenRouter Chat Completions API")
    parser.add_argument("--api-key", default=default_api_key, help="OpenRouter API key")
    parser.add_argument("--model", default=default_model, help="Model name")
    parser.add_argument(
        "--api-url",
        default=default_api_url,
        help="Base API URL",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout in seconds")
    return parser


async def probe(*, api_key: str, model: str, api_url: str, timeout: float) -> ProbeResult:
    started = time.perf_counter()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient(base_url=api_url.rstrip("/"), timeout=httpx.Timeout(timeout)) as client:
            response = await client.post("/chat/completions", headers=headers, json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.is_success:
            content = response.json()
            has_choices = bool(content.get("choices")) if isinstance(content, dict) else False
            details = "ok: choices present" if has_choices else "ok: response without choices"
            return ProbeResult(True, response.status_code, latency_ms, details)

        error_map = {
            401: "401 Unauthorized: неверный API-ключ",
            403: "403 Forbidden: ключ не активен или нет доступа",
        }
        details = error_map.get(response.status_code, f"HTTP {response.status_code}: {response.text[:200]}")
        return ProbeResult(False, response.status_code, latency_ms, details)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency_ms, "timeout: проверь сеть/фаервол")
    except httpx.TransportError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency_ms, f"transport error: {exc}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: не задан API ключ. Передайте --api-key или переменную AI_KEY.")
        return 2

    result = asyncio.run(
        probe(
            api_key=args.api_key,
            model=args.model,
            api_url=args.api_url,
            timeout=args.timeout,
        )
    )

    print(f"ok={result.ok}")
    print(f"status_code={result.status_code}")
    print(f"latency_ms={result.latency_ms}")
    print(f"details={result.details}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
