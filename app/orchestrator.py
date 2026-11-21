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
import re
import uuid
from collections.abc import Awaitable, Callable

from app import audit, cards, editor, idempotency, llm, planner, sessions, workers
from core import config, db

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


ACTION_TITLES = {
    "tariff_change": "смена тарифа",
    "service_toggle": "изменение услуги",
    "contacts_update": "изменение контактов",
}


DENY_TEXTS = {
    "session_limit": "Лимит смен тарифа за сессию исчерпан (не более двух). Позовите оператора, если смена срочная.",
    "confirmation_expired": "Время подтверждения истекло — повторите запрос.",
    "card_not_prepared": "Нечего подтверждать: карточка не готовилась. Повторите запрос.",
    "pdp_unavailable": "Сервис проверки прав недоступен, поэтому изменения сейчас закрыты. Могу позвать оператора.",
    "confirmation_missing": "Подтверждение не принято. Повторите запрос.",
    "confirmation_mismatch": "Подтверждение не относится к этой карточке. Повторите запрос.",
    "no_write_scope": "У вас нет права менять тариф и услуги.",
    "role_has_no_write": "Для вашей роли изменения недоступны.",
}


def _live(card) -> bool:
    """Карточка ещё ждёт ответа: TTL и состояние держит база."""
    return db.fetchone(
        "select 1 as ok from pending_cards where pending_id = %s and state = 'pending' and expires_at > now()",
        (card.pending_id,),
    ) is not None


def _outcome_text(card, stored: idempotency.StoredResult) -> tuple[str, str]:
    """Человекочитаемый ответ по исходу исполнения → (текст, outcome)."""
    result = stored.result
    outcome = "duplicate" if stored.duplicate else result.get("outcome", "error")
    title = ACTION_TITLES.get(card.action, card.action)
    if outcome == "executed":
        return f"Готово, операция «{title}» выполнена.", outcome
    if outcome == "duplicate":
        return "Эта операция уже исполнена — повтор не выполнялся.", outcome
    if outcome == "denied":
        return DENY_TEXTS.get(result.get("reason", ""), "Операция запрещена политикой доступа."), outcome
    if outcome == "daily_limit":
        return (f"Дневной лимит смен тарифа исчерпан ({result.get('limit', 3)}). "
                "Попробуйте завтра или позовите оператора."), outcome
    return f"Операцию «{title}» выполнить не удалось ({result.get('error') or outcome}). Могу позвать оператора.", outcome


def _audit(session, card, what: str, outcome: str, **kw) -> None:
    audit.append(card.sub, what, outcome, trace_id=session.trace_id or None,
                 session_id=session.session_id, **kw)


async def execute_pending(session, card) -> str:
    """«Да»: исполнение кодом; возвращает текст ответа абоненту."""
    text, _ = await _execute(session, card)
    return text


async def _execute(session, card) -> tuple[str, str]:
    """Исполнение ТОЛЬКО кодом, ключ «{action}:{pending_id}» — одно «да», одна операция."""
    key = f"{card.action}:{card.pending_id}"
    pep = sessions.pep_of(session)

    async def call() -> dict:
        # «да» → PDP: ConfirmCard (та же карточка, TTL), затем Execute (окно + лимит)
        verdict = await pep.confirm(card)
        if verdict.allowed:
            verdict = await pep.execute(card)
        if not verdict.allowed:
            cards.settle(session, card, "expired" if verdict.reason == "confirmation_expired" else "denied")
            return {"outcome": "denied", "error": "denied", "reason": verdict.reason}
        cards.settle(session, card, "confirmed")
        session.last_confirmed = card
        try:
            return await session.pool.call("actions", "execute_action", {
                "action": card.action,
                "details_json": json.dumps(card.details, ensure_ascii=False),
                "pending_id": card.pending_id, "idempotency_key": key,
            })
        except Exception as e:  # noqa: BLE001 — сбой исполнения — исход, ключ не занимается
            data = getattr(e, "data", None)
            return {"error": (data or {}).get("error", "execute_failed") if isinstance(data, dict) else "execute_failed",
                    "outcome": "error"}

    stored = await idempotency.execute(key, card.sub, card.action, call)
    text, outcome = _outcome_text(card, stored)
    result = stored.result
    if outcome == "executed":
        _audit(session, card, card.action, "executed", args=card.details, idempotency_key=key,
               before=result.get("before"), after=result.get("after"))
    elif outcome == "duplicate":
        _audit(session, card, card.action, "duplicate_suppressed", args=card.details, idempotency_key=key,
               before=result.get("before"), after=result.get("after"))
    elif outcome == "denied":
        reason = result.get("reason", "")
        _audit(session, card, card.action, "expired" if reason == "confirmation_expired" else "denied",
               args=card.details, idempotency_key=key, detail=reason)
    elif outcome == "daily_limit":
        _audit(session, card, card.action, "server_limit", args=card.details, idempotency_key=key,
               detail=f"daily_limit={result.get('limit')}")
    else:
        _audit(session, card, card.action, "error", args=card.details, idempotency_key=key,
               detail=str(result.get("error") or outcome)[:200])
    return text, outcome


async def reject_pending(session, card) -> str:
    cards.settle(session, card, "rejected")
    _audit(session, card, card.action, "rejected_by_user", args=card.details)
    return "Хорошо, отменяю. Если понадобится — обращайтесь."


async def escalate(session, card=None) -> str:
    if card is not None:
        cards.settle(session, card, "rejected")
    res = await session.pool.call("actions", "create_ticket", {
        "subject": "Запрос абонента: связь с оператором",
        "note": "Абонент попросил позвать оператора в чате.",
    })
    audit.append(session.user.sub, "escalate", "escalated", trace_id=session.trace_id or None,
                 session_id=session.session_id, args=card.details if card else None,
                 after={"ticket_id": res.get("ticket_id")})
    return f"Передал ваш запрос оператору, номер обращения {res.get('ticket_id', '—')}. Он свяжется с вами."


async def decide(session, pending_id: str, answer: str) -> dict:
    """Кнопка карточки: «да» → исполнение, «нет» → отмена."""
    card = cards.active(session)
    if card is None or card.pending_id != pending_id:
        last = session.last_confirmed
        if answer == "yes" and last is not None and last.pending_id == pending_id:
            text, outcome = await _execute(session, last)  # повтор: идемпотентно
            return {"text": text, "outcome": outcome}
        return {"text": "Эта карточка уже не активна.", "outcome": "no_such_card"}
    if not _live(card):
        cards.settle(session, card, "expired")
        _audit(session, card, card.action, "expired", args=card.details)
        return {"text": "Время подтверждения истекло — повторите запрос.", "outcome": "expired"}
    if answer == "yes":
        text, outcome = await _execute(session, card)
        return {"text": text, "outcome": outcome}
    return {"text": await reject_pending(session, card), "outcome": "rejected"}


async def _answer_card(session, message: str, emit: Emit) -> bool:
    """Ход с живой карточкой обслуживает код. True — ход обработан."""
    card = cards.active(session)
    verdict = cards.classify_answer(message)
    if card is None:
        last = session.last_confirmed
        if verdict == "yes" and last is not None:  # повторное «да» — идемпотентно
            text = await execute_pending(session, last)
            await emit({"type": "reply", "text": text, "sources": []})
            return True
        session.last_confirmed = None
        if verdict == "yes" and len(message.split()) <= 3:  # «да» без карточки — нечего подтверждать
            await emit({"type": "reply", "sources": [], "text":
                        "Подтверждать пока нечего: у меня нет карточки действия. Что именно нужно сделать?"})
            return True
        return False
    if not _live(card):
        cards.settle(session, card, "expired")
        _audit(session, card, card.action, "expired", args=card.details)
        if verdict == "yes":
            await emit({"type": "reply", "text": "Время подтверждения истекло — повторите запрос.", "sources": []})
            return True
        return False
    if verdict == "yes":
        text = await execute_pending(session, card)
    elif verdict == "no":
        text = await reject_pending(session, card)
    elif verdict == "operator":
        text = await escalate(session, card)
    else:
        return False  # смена темы: обычный ход, карточка остаётся живой
    await emit({"type": "reply", "text": text, "sources": []})
    return True


def _strip_fake_sources(text: str, sources: list[str]) -> str:
    """Источник в ответе — только из реальных результатов search_kb."""
    def keep(m: re.Match) -> str:
        names = re.findall(r"[A-Za-z0-9_]+", m.group(1))
        return m.group(0) if names and all(n in sources for n in names) else ""
    return re.sub(r"\s*\((?:источник|источники):\s*([^)]*)\)", keep, text).strip()


async def run_turn(session, message: str, emit: Emit) -> None:
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    if await _answer_card(session, message, emit):
        return

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, history = await planner.next_plan(session.planner, session.thread, message, session.trace_id)
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

    card_before = session.pending_card.pending_id if session.pending_card else None
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

    card = session.pending_card
    if card is not None and card.pending_id != card_before:
        await emit({"type": "card", "pending_id": card.pending_id, "card": card.public_view(config.CARD_TTL_SEC)})

    final = await llm.run_agent(
        editor.build_editor(session), editor.editor_task(message, runs), trace_id=session.trace_id
    )
    sources = _sources(runs)
    await emit({"type": "reply", "text": _strip_fake_sources(final["report"], sources), "sources": sources})
