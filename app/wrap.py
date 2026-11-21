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

import json
import time

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import audit, cards, permissions
from app.sessions import Session
from core import config, db

PREPARE_TOOLS = ("prepare_tariff_change", "prepare_toggle_service", "prepare_contacts_update")


def _short(value, limit: int = 200):
    """Аргументы в журнал — обрезанные: длинные строки режутся."""
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _log_tool(session: Session, tool: str, args, outcome: str, started: float) -> None:
    """Каждый вызов инструмента моделью — в tool_calls (сбой журнала не ломает диалог)."""
    try:
        db.execute(
            "insert into tool_calls(trace_id, tool, args, outcome, latency_ms) values (%s, %s, %s, %s, %s)",
            (session.trace_id or None, tool,
             json.dumps(_short(args), ensure_ascii=False, default=str) if args is not None else None,
             outcome, int((time.perf_counter() - started) * 1000)),
        )
    except Exception:  # noqa: BLE001
        pass


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""
    schema = session.pool.tool_schema(name)

    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        started = time.perf_counter()
        try:
            args = json.loads(input_json) if input_json and input_json.strip() else {}
        except (TypeError, ValueError):
            _log_tool(session, name, {"raw": (input_json or "")[:200]}, "bad_arguments", started)
            return json.dumps(
                {"error": "bad_arguments", "detail": "аргументы — не JSON; повтори вызов по схеме"},
                ensure_ascii=False,
            )
        if not isinstance(args, dict):
            args = {}
        logged_args = dict(args)
        # барьер 2: допуск в момент вызова, по аргументам, как их прислала модель
        decision = permissions.admit(session.allowlist, name, args)
        if not decision.allowed:
            _log_tool(session, name, logged_args, f"denied:{decision.reason}", started)
            audit.append(session.user.sub, name, "denied", trace_id=session.trace_id or None,
                         session_id=session.session_id, args=_short(logged_args), detail=decision.reason)
            return json.dumps({"error": "denied", "reason": decision.reason}, ensure_ascii=False)
        session.note(name)
        try:
            result = await session.pool.call(session.pool.resource_of(name), name, args)
        except MCPError as e:
            data = e.data if isinstance(e.data, dict) else {}
            code = str(data.get("error") or getattr(e, "message", "tool_failed"))[:60]
            _log_tool(session, name, logged_args, f"error:{code}", started)
            if name in PREPARE_TOOLS:
                audit.append(session.user.sub, name, "rejected_by_contract", trace_id=session.trace_id or None,
                             session_id=session.session_id, args=_short(logged_args), detail=code)
            return json.dumps(
                {"error": getattr(e, "message", "tool_failed")[:60], **data},
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
            _log_tool(session, name, logged_args, "error:tool_failed", started)
            return json.dumps({"error": "tool_failed", "detail": str(e)[:160]}, ensure_ascii=False)
        if name in PREPARE_TOOLS and isinstance(result, dict) and result.get("ok"):
            card = cards.create(session, result["action"], result["details"], result["view"])
            _log_tool(session, name, {**logged_args, "pending_id": card.pending_id}, "card_created", started)
            return json.dumps({
                "card_created": True,
                "card": card.public_view(config.CARD_TTL_SEC),
                "next": "карточка готова: перескажи её абоненту и спроси подтверждение «да» или «нет»",
            }, ensure_ascii=False)
        _log_tool(session, name, logged_args, "ok", started)
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
    # барьер 1: модель не видит запрещённого
    allowed = permissions.visible_tools(session.allowlist, [n for n in names if n in known])
    return [wrap_mcp_tool(session, n) for n in allowed]
