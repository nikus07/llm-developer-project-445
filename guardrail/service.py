"""Guardrail-сервис: отдельный stateless-контур проверки текста.

    uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300

Устройство:
  1. RULE-движок: RULE_PATTERNS — детерминированные маркеры атак стенда
     (jailbreak/role_hijack — «суперпользователь», «ты теперь админ»;
     prompt_injection — «игнорируй инструкц...»; tool_hijack — «смени
     тариф в аккаунте»; injected_instruction — «немедленно переведи»,
     «без подтверждения» — ловит ловушку из базы знаний). Эвристика
     лабораторная, не претендует на прод.
  2. POST /check {"text", "mode": "input"|"output"} →
     {"verdict": "safe"|"unsafe", "category", "engine", "mode"}.
  3. POST /admin/down и /admin/up — имитация отказа сервиса; /health —
     503 при down.
  4. Опционально GUARDRAIL_MODEL=1 — классификатор HiveTraceGuard-Pro
     (transformers+torch, в requirements не включены).

Недоступность сервиса — не крах контура: оркестратор решает сам
(fail-closed для действий, деградация для чтения).
"""
from __future__ import annotations

import re

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import config

# Детерминированные маркеры атак стенда: (категория, регулярное выражение).
# Лабораторная эвристика, для продакшена её не хватит.
RULE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (cat, re.compile(rx, re.IGNORECASE))
    for cat, rx in [
        ("jailbreak", r"суперпользовател"),
        ("jailbreak", r"ты\s+теперь\s+(админ|администратор|root|оператор|разработчик)"),
        ("jailbreak", r"(режим|mode)\s+(бога|разработчика|developer|dan)"),
        ("jailbreak", r"отключи\s+(все\s+)?(проверки|ограничения|фильтры|защиту)"),
        ("prompt_injection", r"игнорируй\s+(все\s+)?(предыдущ\w*\s+|прошл\w*\s+|свои\s+|системн\w*\s+)*инструкц"),
        ("prompt_injection", r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions"),
        ("prompt_injection", r"забудь\s+(все\s+)?(правила|инструкции)"),
        ("prompt_injection", r"(покажи|выведи|раскрой)\s+(свой\s+)?(системн\w+\s+)?(промпт|инструкци)"),
        ("tool_hijack", r"смени\s+тариф\s+в\s+аккаунте"),
        ("tool_hijack", r"от\s+имени\s+(другого\s+)?(пользовател|абонент|админ)"),
        ("tool_hijack", r"(user_id|msisdn|subscriber)\s*[=:]"),
        ("injected_instruction", r"немедленно\s+перевед"),
        ("injected_instruction", r"без\s+подтверждени"),
        ("injected_instruction", r"срочное\s+указание"),
    ]
]

_state = {"down": False}


def classify(text: str, mode: str) -> tuple[str, str]:
    """RULE-движок → (verdict, category)."""
    for category, pattern in RULE_PATTERNS:
        if pattern.search(text or ""):
            return "unsafe", category
    return "safe", ""


async def check(request: Request) -> JSONResponse:
    if _state["down"]:
        return JSONResponse({"error": "unavailable"}, status_code=503)
    body = await request.json()
    mode = body.get("mode", "input")
    if mode not in ("input", "output"):
        return JSONResponse({"error": "bad_mode"}, status_code=400)
    verdict, category = classify(body.get("text", ""), mode)
    return JSONResponse({"verdict": verdict, "category": category, "engine": "RULE", "mode": mode})


async def health(request: Request) -> JSONResponse:
    if _state["down"]:
        return JSONResponse({"status": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "service": "guardrail", "engine": "RULE"})


async def admin_down(request: Request) -> JSONResponse:
    _state["down"] = True
    return JSONResponse({"down": True})


async def admin_up(request: Request) -> JSONResponse:
    _state["down"] = False
    return JSONResponse({"down": False})


def build_app() -> Starlette:
    return Starlette(routes=[
        Route("/check", check, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/admin/down", admin_down, methods=["POST"]),
        Route("/admin/up", admin_up, methods=["POST"]),
    ])


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=config.GUARDRAIL_PORT, log_level="warning")
