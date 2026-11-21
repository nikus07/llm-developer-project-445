"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

TODO(права):
  1. Decision(allowed, reason) и Pep(session_id, claims, guard_state):
     субъектные факты из проверенного токена — sub, role,
     scopes = claims["scope"].split().
  2. _open(): POST {PDP_URL}/session {"session_id", "subject"} — один раз.
  3. prepare(card) → POST /authorize {action: "Prepare"} — Allow фиксирует
     карточку в журнале PDP; Deny — карточки не существует.
  4. confirm(card) → POST /confirm {action: "ConfirmCard"} — «да»;
     TTL карточки проверяет PDP по wall clock.
  5. execute(card) → POST /authorize {action: "Execute"} — окно
     formerly ConfirmCard + forbid-лимит.
  6. input для политики: {"action": card.action, "pending_id",
     "details": json.dumps(card.details), "guard_ok": bool(guard_state),
     **subject}. Недоступный PDP — fail-closed: Decision(False,
     "pdp_unavailable").
"""
from __future__ import annotations

import json
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


class Pep:
    """Клиент PDP одной сессии: субъектные факты — из проверенного токена."""

    def __init__(self, session_id: str, claims: dict, guard_state: dict) -> None:
        self.session_id = session_id
        self.guard_state = guard_state
        self.subject = {
            "sub": claims.get("sub", ""),
            "role": claims.get("role", ""),
            "scopes": str(claims.get("scope", "")).split(),
        }
        self._opened = False

    async def _post(self, path: str, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            return await client.post(f"{config.PDP_URL}{path}", json=body)

    async def _open(self) -> None:
        r = await self._post("/session", {"session_id": self.session_id, "subject": self.subject})
        r.raise_for_status()
        self._opened = True

    def _input(self, card) -> dict:
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            "details": json.dumps(card.details, ensure_ascii=False),
            "guard_ok": bool(self.guard_state.get("ok", False)),
            **self.subject,
        }

    async def _ask(self, path: str, body: dict) -> Decision:
        """Запрос к PDP; недоступный сервис — fail-closed."""
        try:
            if not self._opened:
                await self._open()
            r = await self._post(path, {"session_id": self.session_id, **body})
            if r.status_code == 404:  # PDP перезапускали: сессия потеряна — открыть заново
                await self._open()
                r = await self._post(path, {"session_id": self.session_id, **body})
            r.raise_for_status()
            data = r.json()
        except Exception:  # noqa: BLE001 — любой сбой PDP закрывает действие
            return Decision(False, "pdp_unavailable")
        return Decision(data.get("decision") == "Allow", data.get("reason", ""))

    async def prepare(self, card) -> Decision:
        return await self._ask("/authorize", {"action": "Prepare", "input": self._input(card)})

    async def confirm(self, card) -> Decision:
        return await self._ask("/confirm", {"input": self._input(card)})

    async def execute(self, card) -> Decision:
        return await self._ask("/authorize", {"action": "Execute", "input": self._input(card)})
