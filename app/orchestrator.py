"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента; оркестратор управляет запуском агентов и
собирает события хода для фронтенда.

TODO(агенты) — read-контур:
  run_turn(session, message, emit):
    1. session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    2. план: planner.build_planner() лениво → planner.next_plan →
       planner.gate; отброшенные шаги — в session.trace;
       событие {"type": "plan", "agents": [...], "dropped": [...]}.
    3. plan.question без шагов → {"type": "reply", "text": question}.
    4. запуск агентов по шагам (llm.run_agent с trace_id); после агентов —
       редактор; событие {"type": "reply", "text", "sources"}
       (sources — источники kb из tool-результатов).
  Emit = Callable[[dict], Awaitable[None]].

TODO(контракты) — карточка действия:
  - в начале хода: cards.active(session) → cards.classify_answer(message):
    «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
    «оператор» → escalate, смена темы → обычный ход с живой карточкой;
  - после агентов: свежая карточка → событие {"type": "card"} и ход
    завершается (ждём подтверждение);
  - execute_pending(session, card): settle("confirmed"), ключ
    f"{action}:{pending_id}", idempotency.execute → вызов MCP execute_action
    → человекочитаемый ответ (executed / duplicate / daily_limit / отказ).

TODO(аудит) — аудит: audit.append на все исходы пути действия
  (executed с before/after, duplicate_suppressed, rejected_by_user,
  server_limit, escalated).

TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
  человекочитаемый отказ по reason (session_limit, confirmation_expired,
  card_not_prepared, pdp_unavailable).

TODO(guardrail) — guardrail: input-проверка реплики до всего хода (unsafe →
  отказ, запись в аудит); output-проверка ответа и карточки до показа;
  недоступный сервис → session.guard_state["ok"] = False (fail-closed
  для действий), чтение продолжается.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from app import editor, llm, planner, workers

Emit = Callable[[dict], Awaitable[None]]


def _sources(runs: list[dict]) -> list[str]:
    """Источники базы знаний из tool-результатов search_kb."""
    found: list[str] = []
    for run in runs:
        for call in run["tools"]:
            if call["tool"] != "search_kb":
                continue
            try:
                out = call["output"]
                data = json.loads(out) if isinstance(out, str) else out
                for hit in data.get("results", []):
                    if hit["source"] not in found:
                        found.append(hit["source"])
            except (TypeError, ValueError, AttributeError, KeyError):
                continue
    return found


async def run_turn(session, message: str, emit: Emit) -> None:
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, history = await planner.next_plan(session.planner, session.thread, message)
    plan, dropped = planner.gate(plan)
    session.thread = history
    for reason in dropped:
        session.trace.append(f"gate: {reason}")
    await emit({
        "type": "plan",
        "agents": [s.agent for s in plan.steps],
        "steps": [{"agent": s.agent, "task": s.task, "action": s.action} for s in plan.steps],
        "dropped": dropped,
    })

    if not plan.steps:
        if plan.question:
            await emit({"type": "reply", "text": plan.question, "sources": []})
            return
        # планировщик не нашёл шагов — редактор отвечает сам
        reply = await llm.run_agent(
            editor.build_editor(session), editor.editor_task(message, []), trace_id=session.trace_id
        )
        await emit({"type": "reply", "text": reply["report"], "sources": []})
        return

    team = workers.build_workers(session, tuple(dict.fromkeys(s.agent for s in plan.steps)))
    runs: list[dict] = []
    for step in plan.steps:
        task = step.task or message
        if runs:  # последующие агенты видят доклады предыдущих как данные
            prior = "\n".join(f"- [{r['agent']}] {r['report'].strip()}" for r in runs)
            task = f"{task}\n\nУже известно от других агентов:\n{prior}"
        run = await llm.run_agent(team[step.agent], task, trace_id=session.trace_id)
        run["agent"] = step.agent
        runs.append(run)

    final = await llm.run_agent(
        editor.build_editor(session), editor.editor_task(message, runs), trace_id=session.trace_id
    )
    await emit({"type": "reply", "text": final["report"], "sources": _sources(runs)})
