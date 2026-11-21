"""Сборка агентов на модели из .env + учёт LLM-вызовов.

TODO(агенты):
  1. build_agent(name, instructions, tools=None, output_type=None):
     Agent из agents SDK; output_type — нестрогая схема
     (AgentOutputSchema(output_type, strict_json_schema=False)):
     строгий JSON-режим слишком требователен к малым моделям;
     модель — config.MODEL_NAME или config.setup_model().
  2. run_agent(agent, task, *, trace_id="") → {"report", "tools", "seconds"}:
     отдельный Runner.run; из result.new_items собрать tool-вызовы
     (call_id → arguments/output); report — result.final_output строкой.

TODO(аудит): в run_agent — INSERT в llm_calls: trace_id, имя агента,
  модель, токены (result.context_wrapper.usage), латентность.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""
from __future__ import annotations

import time

from agents import Agent, AgentOutputSchema, Runner

from core import config, db


def build_agent(name: str, instructions: str, tools: list | None = None, output_type=None) -> Agent:
    """Агент на модели из .env; структурный выход — нестрогой схемой."""
    model = config.MODEL_NAME or config.setup_model()
    kwargs: dict = {}
    if output_type is not None:
        kwargs["output_type"] = AgentOutputSchema(output_type, strict_json_schema=False)
    return Agent(name=name, instructions=instructions, tools=tools or [], model=model, **kwargs)


def collect_tools(result) -> list[dict]:
    """Tool-вызовы прогона: call_id → имя, аргументы, результат."""
    calls: dict[str, dict] = {}
    for item in result.new_items:
        kind = getattr(item, "type", "")
        raw = item.raw_item
        get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
        cid = get("call_id")
        if kind == "tool_call_item":
            calls[cid] = {"tool": get("name"), "arguments": get("arguments"), "output": None}
        elif kind == "tool_call_output_item" and cid in calls:
            calls[cid]["output"] = item.output
    return list(calls.values())


def _log_llm_call(agent_name: str, trace_id: str, usage, started: float) -> None:
    db.execute(
        "insert into llm_calls(trace_id, agent, model, input_tokens, output_tokens, latency_ms)"
        " values (%s, %s, %s, %s, %s, %s)",
        (trace_id or None, agent_name, config.MODEL_NAME,
         getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
         int((time.perf_counter() - started) * 1000)),
    )


async def run_agent(agent: Agent, task, *, trace_id: str = "") -> dict:
    """Отдельный Runner.run; возвращает доклад, tool-вызовы и время. Каждый запуск — в llm_calls."""
    started = time.perf_counter()
    try:
        result = await Runner.run(agent, task, max_turns=8)
    except Exception:
        _log_llm_call(agent.name, trace_id, None, started)  # сбой тоже виден в журнале
        raise
    _log_llm_call(agent.name, trace_id, result.context_wrapper.usage, started)
    return {
        "report": str(result.final_output if result.final_output is not None else ""),
        "tools": collect_tools(result),
        "seconds": round(time.perf_counter() - started, 2),
        "result": result,
    }
