"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

prepare_* ничего не исполняют: проверяют предусловия и собирают view для
карточки. execute_action вызывает только код оркестратора после «да».
Субъект sub приходит аргументом до шага прав (осознанная дыра).
"""
from __future__ import annotations

import json

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from actions.contracts import (
    ActionError, normalize_email, normalize_phone, normalize_service, normalize_tariff,
)
from core import config

SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]
MOCK = f"{config.STAND_URL}/mock"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=15)


async def _get(path: str, **params) -> dict:
    async with _client() as c:
        r = await c.get(f"{MOCK}{path}", params=params)
    if r.status_code == 404:
        raise ActionError("not_found", "абонент не найден")
    r.raise_for_status()
    return r.json()


async def _post(path: str, body: dict) -> dict:
    async with _client() as c:
        r = await c.post(f"{MOCK}{path}", json=body)
    r.raise_for_status()
    return r.json()


def _prepared(action: str, details: dict, view: dict) -> dict:
    return {"ok": True, "action": action, "details": details, "view": view}


async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    subscriber = await _get("/crm/subscriber", sub=sub)
    catalog = (await _get("/crm/tariffs"))["tariffs"]
    name = normalize_tariff(tariff, catalog)
    target = next(t for t in catalog if t["name"] == name)
    if not target["available"]:
        raise ActionError("tariff_not_available", f"тариф «{name}» недоступен для подключения",
                          available=[t["name"] for t in catalog if t["available"]])
    current = subscriber["tariff"]
    if current == name:
        raise ActionError("same_tariff", f"тариф «{name}» уже подключён", current=current)
    cur_price = next((t["price_rub"] for t in catalog if t["name"] == current), None)
    view = {
        "что": "смена тарифа",
        "с": current,
        "на": name,
        "плата": f"{cur_price:g} ₽/мес → {target['price_rub']:g} ₽/мес",
        "вступает в силу": "с 1-го числа следующего расчётного периода",
    }
    return _prepared("tariff_change", {"tariff": name}, view)


async def prepare_toggle_service(sub: str, service: str, enabled: bool) -> dict:
    subscriber = await _get("/crm/subscriber", sub=sub)
    name = normalize_service(service, SERVICE_CATALOG)
    mine = next((s for s in subscriber["services"] if s["service"] == name), None)
    if mine is None:
        raise ActionError("service_not_available", f"услуга «{name}» вам недоступна",
                          available=[s["service"] for s in subscriber["services"]])
    if enabled and mine["enabled"]:
        raise ActionError("service_already_on", f"услуга «{name}» уже подключена")
    if not enabled and not mine["enabled"]:
        raise ActionError("service_already_off", f"услуга «{name}» уже отключена")
    view = {
        "что": "подключение услуги" if enabled else "отключение услуги",
        "услуга": name,
        "стоимость": f"{mine['price_rub']:g} ₽/мес",
    }
    return _prepared("service_toggle", {"service": name, "enabled": enabled}, view)


async def prepare_contacts_update(sub: str, phone: str | None = None, email: str | None = None) -> dict:
    if not phone and not email:
        raise ActionError("nothing_to_update", "не указан ни телефон, ни почта")
    subscriber = await _get("/crm/subscriber", sub=sub)
    details: dict = {}
    view: dict = {"что": "изменение контактов"}
    if phone:
        new_phone = normalize_phone(phone)
        if new_phone != subscriber["phone"]:
            details["phone"] = new_phone
            view["телефон"] = f"{subscriber['phone']} → {new_phone}"
    if email:
        new_email = normalize_email(email)
        if new_email != subscriber["email"]:
            details["email"] = new_email
            view["почта"] = f"{subscriber['email']} → {new_email}"
    if not details:
        raise ActionError("same_contacts", "указанные контакты уже записаны")
    return _prepared("contacts_update", details, view)


async def execute_action(sub: str, action: str, details_json: str, pending_id: str,
                         idempotency_key: str) -> dict:
    """ИСПОЛНЕНИЕ — вызывает только код оркестратора после явного «да»."""
    try:
        details = json.loads(details_json or "{}")
    except ValueError:
        raise ActionError("bad_details", "details_json — не JSON") from None

    if action == "tariff_change":
        # дедупликацию и дневной лимит держит сам биллинг
        result = await _post("/billing/tariff-change", {
            "sub": sub, "tariff": details.get("tariff", ""), "idempotency_key": idempotency_key,
        })
        return {**result, "action": action, "pending_id": pending_id}
    if action == "service_toggle":
        enabled = bool(details.get("enabled"))
        result = await _post("/crm/toggle-service", {
            "sub": sub, "service": details.get("service", ""), "enabled": enabled})
        outcome = "executed" if result.get("outcome") == "toggled" else result.get("outcome", "error")
        return {**result, "outcome": outcome, "action": action, "pending_id": pending_id,
                "before": {"enabled": not enabled}, "after": details}
    if action == "contacts_update":
        body = {k: v for k, v in details.items() if k in ("phone", "email")}
        old = await _get("/crm/subscriber", sub=sub)
        result = await _post("/crm/contacts", {"sub": sub, **body})
        outcome = "executed" if result.get("outcome") == "updated" else result.get("outcome", "error")
        return {**result, "outcome": outcome, "action": action, "pending_id": pending_id,
                "before": {k: old.get(k) for k in body}, "after": details}
    raise ActionError("unknown_action", f"действие «{action}» неизвестно")


async def create_ticket(sub: str, subject: str, note: str = "") -> dict:
    result = await _post("/tickets/create", {"sub": sub, "subject": subject, "note": note})
    return {"ok": True, **result}
