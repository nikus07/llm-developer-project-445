"""Идемпотентность исполнения на стороне оркестратора.

TODO(контракты): StoredResult(duplicate, result) и execute(key, sub, action, fn):
  - ключ в таблице idempotency с результатом → StoredResult(duplicate=True,
    сохранённый результат) — повтор не исполняет операцию второй раз;
  - иначе исполнить fn(); неудачный исход (error в ответе) ключ не
    занимает — сбой не должен запирать повторную попытку;
  - успех → INSERT ... ON CONFLICT (key) DO NOTHING.

Ключ в оркестраторе — f"{action}:{pending_id}": одно «да» — одна операция.
Для смены тарифа дедупликация есть и на стороне биллинга — два барьера;
для услуг и контактов этот слой единственный.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core import db


@dataclass
class StoredResult:
    duplicate: bool
    result: dict


def _failed(result: dict) -> bool:
    """Неудача не занимает ключ: сбой не должен запирать повторную попытку."""
    return "error" in result or result.get("outcome") not in ("executed", "duplicate")


async def execute(key: str, sub: str, action: str, fn: Callable[[], Awaitable[dict]]) -> StoredResult:
    known = db.fetchone("select result from idempotency where key = %s", (key,))
    if known is not None:
        return StoredResult(duplicate=True, result=known["result"] or {})

    result = await fn()
    if not _failed(result):
        db.execute(
            "insert into idempotency(key, sub, action, result) values (%s, %s, %s, %s)"
            " on conflict (key) do nothing",
            (key, sub, action, json.dumps(result, ensure_ascii=False, default=str)),
        )
    return StoredResult(duplicate=result.get("outcome") == "duplicate", result=result)
