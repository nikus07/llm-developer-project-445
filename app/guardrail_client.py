"""Клиент guardrail-сервиса: verdikt вероятностный, решения — за кодом.

TODO(guardrail): Verdict(safe, category, available) и check(text, mode):
  POST {GUARDRAIL_URL}/check {"text", "mode": "input"|"output"}.
  Недоступность сервиса — НЕ исключение: Verdict(safe=True,
  available=False) — чтение продолжается, действия блокируются флагом
  guard_ok в политике (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Verdict:
    safe: bool
    category: str = ""
    available: bool = True


async def check(text: str, mode: str = "input") -> Verdict:
    """Вердикт guardrail. Недоступность — не исключение: safe=True, available=False."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            r = await client.post(f"{config.GUARDRAIL_URL}/check", json={"text": text, "mode": mode})
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001 — сервис упал: чтение продолжается, действия закроет политика
        return Verdict(safe=True, available=False)
    return Verdict(safe=data.get("verdict") == "safe", category=data.get("category", ""), available=True)
