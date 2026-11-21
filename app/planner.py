"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов. Действия
(смена тарифа, услуги, контакты, тикет) идут через карточку подтверждения
и политику прав, а не напрямую.

TODO(агенты):
  1. Схемы Step и Plan (pydantic):
     - Step: agent (алиас agentName), task (алиас job), why, action: bool
       — алиасы нужны: модели отвечают своим диалектом имён;
     - Plan: steps: list[Step], question: str | None (уточняющий вопрос,
       если данных для плана мало).
  2. PLANNER_PROMPT — каталог агентов и правила планирования:
     вопрос о данных абонента — один шаг data; правила и последствия —
     шаг kb; «почему списание» — оба; изменения (смена тарифа, услуги,
     контакты) — шаг tariff с action=true; вне тем оператора — tickets
     с action=true; названия тарифов — строчными без кавычек.
  3. build_planner() — агент со структурированным выходом Plan
     (app.llm.build_agent(..., output_type=Plan)).
  4. gate(plan) — гейт: action-шаги только tariff/tickets, у read-агентов
     action снимается, неизвестные агенты отбрасываются (причина — в трейс).
  5. next_plan(planner, history, message) — один вызов Runner.run
     (через пул llm_pool, если он включён) → (Plan, история хода).
"""
from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

READ_AGENTS = ("data", "kb")
ACTION_AGENTS = ("tariff", "tickets")


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent: str = Field(validation_alias=AliasChoices("agent", "agentName", "agent_name"))
    task: str = Field(default="", validation_alias=AliasChoices("task", "job"))
    why: str = ""
    action: bool = False


class Plan(BaseModel):
    steps: list[Step] = []
    question: str | None = None


def _known_agents() -> dict:
    from app.workers import CATALOG

    return CATALOG


def planner_prompt() -> str:
    lines = "\n".join(f"- {aid}: {meta['title']}" for aid, meta in _known_agents().items())
    return f"""Ты планировщик ассистента мобильного оператора. Ты НЕ отвечаешь абоненту:
ты только составляешь план — какие агенты и с какой задачей запустить.

Агенты:
{lines}

Правила:
- вопрос о данных абонента (тариф, профиль, списания, баланс) — один шаг data;
- вопрос о правилах, условиях, последствиях — шаг kb;
- «почему списание» и подобное — оба шага: data (найти списание) и kb (правила);
- изменения (смена тарифа, услуги, контакты) — шаг tariff с action=true;
- просьба вне тем оператора — шаг tickets с action=true;
- читающие агенты (data, kb) всегда с action=false;
- названия тарифов пиши строчными буквами без кавычек;
- если данных для плана не хватает — верни пустой steps и вопрос в question;
- в task пиши конкретную задачу агенту по-русски, одной фразой.

Ответ — только JSON вида
{{"steps": [{{"agent": "data", "task": "...", "why": "...", "action": false}}], "question": null}}"""


def build_planner():
    from app.llm import build_agent

    return build_agent("planner", planner_prompt(), output_type=Plan)


def gate(plan: Plan) -> tuple[Plan, list[str]]:
    """Код решает, что исполнять: только известные агенты, action — лишь у tariff/tickets."""
    known = _known_agents()
    kept: list[Step] = []
    dropped: list[str] = []
    for step in plan.steps:
        agent = step.agent.strip().lower()
        if agent not in known:
            dropped.append(f"{step.agent}: неизвестный агент")
            continue
        action = bool(step.action) and agent in ACTION_AGENTS
        if step.action and not action:
            dropped.append(f"{agent}: action снят (агент чтения)")
        kept.append(step.model_copy(update={"agent": agent, "action": action}))
    return Plan(steps=kept, question=plan.question), dropped


async def next_plan(planner, history: list, message: str):
    """Один вызов планировщика по истории диалога → (Plan, история хода)."""
    from agents import Runner

    inp = list(history) + [{"role": "user", "content": message}]
    result = await Runner.run(planner, inp, max_turns=3)
    plan = result.final_output
    if not isinstance(plan, Plan):
        plan = Plan.model_validate(plan)
    return plan, result.to_input_list()
