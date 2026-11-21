"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

TODO(аудит):
  1. OUTCOMES — фиксированная таксономия исходов: executed,
     duplicate_suppressed, rejected_by_user, rejected_by_contract, expired,
     escalated, denied, guardrail_unsafe, guardrail_degraded,
     token_refreshed, server_limit, budget_exhausted, error.
  2. append(who, what, outcome, *, trace_id=None, session_id=None, args=None,
     before=None, after=None, idempotency_key=None, detail=None) — INSERT в
     audit_log. Журнал не редактируется: только append и чтение.
  3. tail(n=20, who=None) и trace(trace_id) — чтение для разбора инцидентов.

В журнал попадают и неуспешные попытки: по нему видно «кто пытался и что».
Не логируйте текст реплик целиком — журнал про действия, не про переписку.
"""
from __future__ import annotations

import json

from core import db

OUTCOMES = frozenset({
    "executed",
    "duplicate_suppressed",
    "rejected_by_user",
    "rejected_by_contract",
    "expired",
    "escalated",
    "denied",
    "guardrail_unsafe",
    "guardrail_degraded",
    "token_refreshed",
    "server_limit",
    "budget_exhausted",
    "error",
})


def _json(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def append(who: str, what: str, outcome: str, *, trace_id: str | None = None,
           session_id: str | None = None, args=None, before=None, after=None,
           idempotency_key: str | None = None, detail: str | None = None) -> None:
    """Дописать запись в журнал. Обновления и удаления у модуля нет намеренно."""
    if outcome not in OUTCOMES:
        raise ValueError(f"неизвестный исход аудита: {outcome}")
    db.execute(
        "insert into audit_log(trace_id, session_id, who, what, outcome, args, before, after,"
        " idempotency_key, detail) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (trace_id, session_id, who, what, outcome, _json(args), _json(before), _json(after),
         idempotency_key, detail),
    )


def tail(n: int = 20, who: str | None = None) -> list[dict]:
    """Последние записи (новые первыми), при необходимости — только одного субъекта."""
    if who:
        return db.fetchall("select * from audit_log where who = %s order by id desc limit %s", (who, n))
    return db.fetchall("select * from audit_log order by id desc limit %s", (n,))


def trace(trace_id: str) -> list[dict]:
    """Хронология хода: аудит + вызовы моделей + вызовы инструментов по trace_id."""
    events: list[dict] = []
    for row in db.fetchall("select * from audit_log where trace_id = %s", (trace_id,)):
        events.append({"kind": "audit", "ts": row["ts"], "id": row["id"], **row})
    for row in db.fetchall("select * from llm_calls where trace_id = %s", (trace_id,)):
        events.append({"kind": "llm", **row})
    for row in db.fetchall("select * from tool_calls where trace_id = %s", (trace_id,)):
        events.append({"kind": "tool", **row})
    return sorted(events, key=lambda e: (e["ts"], e["kind"], e.get("id", 0)))
