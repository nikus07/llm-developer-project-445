"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

Готовая основа: MCP-инструмент превращается в FunctionTool агента;
субъект вызова в Bearer-токене соединения; кривой JSON аргументов —
машинно-читаемый ответ модели, сессия не роняется; ошибка MCP-сервера
(MCPError, код + data с подсказками) — JSON tool-результат для модели.

TODO(контракты): крючок prepare_* — успешный ответ ({"ok": true, "action",
  "details", "view"}) → cards.create(...) → вернуть модели
  {"card_created": true, "card", "next": "перескажи карточку и спроси
  подтверждение «да/нет»"}; пока параметр sub есть в схеме инструмента —
  подставляйте session.user.sub сами (модель его не видит).

TODO(аудит): каждый tool-вызов модели → таблицу tool_calls
  (trace_id, tool, args — обрезанные, outcome, latency_ms).

TODO(права): два барьера —
  wrap_tools: фильтровать имена через permissions.visible_tools
  (барьер 1: модель не видит запрещённого);
  invoke: permissions.admit(session.allowlist, name, args) перед вызовом
  (барьер 2: отказ → {"error": "denied", "reason"} + запись в аудит);
  подготовленную карточку проводить через pep.prepare — Deny → карточки
  нет, модель получает отказ с reason.
"""
from __future__ import annotations

import copy
import json

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import cards
from app.sessions import Session
from core import config

PREPARE_TOOLS = ("prepare_tariff_change", "prepare_toggle_service", "prepare_contacts_update")


def _model_schema(schema: dict) -> tuple[dict, bool]:
    """Схема для модели: параметра sub в ней нет — его подставляет обёртка."""
    schema = copy.deepcopy(schema)
    has_sub = "sub" in (schema.get("properties") or {})
    if has_sub:
        schema["properties"].pop("sub")
        schema["required"] = [r for r in schema.get("required", []) if r != "sub"]
    return schema, has_sub


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""
    schema, inject_sub = _model_schema(session.pool.tool_schema(name))

    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        try:
            args = json.loads(input_json) if input_json and input_json.strip() else {}
        except (TypeError, ValueError):
            return json.dumps(
                {"error": "bad_arguments", "detail": "аргументы — не JSON; повтори вызов по схеме"},
                ensure_ascii=False,
            )
        if inject_sub:
            args["sub"] = session.user.sub  # субъект подставляет код, модель его не видит
        session.note(name)
        try:
            result = await session.pool.call(session.pool.resource_of(name), name, args)
        except MCPError as e:
            data = e.data if isinstance(e.data, dict) else {}
            return json.dumps(
                {"error": getattr(e, "message", "tool_failed")[:60], **data},
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
            return json.dumps({"error": "tool_failed", "detail": str(e)[:160]}, ensure_ascii=False)
        if name in PREPARE_TOOLS and isinstance(result, dict) and result.get("ok"):
            card = cards.create(session, result["action"], result["details"], result["view"])
            return json.dumps({
                "card_created": True,
                "card": card.public_view(config.CARD_TTL_SEC),
                "next": "карточка готова: перескажи её абоненту и спроси подтверждение «да» или «нет»",
            }, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=session.pool.tool_description(name),
        params_json_schema=schema,
        on_invoke_tool=invoke,
    )


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Собрать обёртки для перечня имён (какие имена давать — решает код)."""
    known = set(session.pool.tool_names())
    return [wrap_mcp_tool(session, n) for n in names if n in known]
