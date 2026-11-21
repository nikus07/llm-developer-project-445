"""Права сессии: allowlist из claims, два барьера оркестратора.

TODO(права) — ядро шага:
  1. Константы операций:
     READ_OPS = get_my_profile, get_my_tariff, list_tariffs,
                list_my_charges, search_kb
     WRITE_OPS = prepare_tariff_change, prepare_toggle_service,
                 prepare_contacts_update (карточные — исполняет политика)
     OPERATOR_OPS = get_subscriber_overview (только operator; чужой
                    идентификатор легитимен — роль проверит сервер)
     ESCALATION_OPS = create_ticket (позвать человека можно всегда)
     IDENTITY_ARGS = user_id, account, subscriber, msisdn, sub
                    (phone не ключ: это новое значение в prepare_contacts_update)
  2. allowlist_from_claims(claims) → Allowlist(sub, role, scopes,
     read_ops, write_ops): роль даёт базовый набор, scope сужает —
     карточные действия только клиенту с tariffs:write; оператору —
     operator-инструменты и никаких write.
  3. visible_tools(allowlist, names) — барьер 1: модель видит только
     разрешённое; execute_action не виден никому.
  4. admit(allowlist, operation, args) → Decision(allowed, reason) —
     барьер 2: операция вне allowlist — отказ operation_not_allowed:<op>;
     identity-ключи в аргументах — отказ object_from_arguments:<ключ>; лимиты считает политика.
"""
from __future__ import annotations

from dataclasses import dataclass, field


READ_OPS = ("get_my_profile", "get_my_tariff", "list_tariffs", "list_my_charges", "search_kb")
WRITE_OPS = ("prepare_tariff_change", "prepare_toggle_service", "prepare_contacts_update")
OPERATOR_OPS = ("get_subscriber_overview",)
ESCALATION_OPS = ("create_ticket",)
# phone — не ключ идентичности: это новое значение контакта в prepare_contacts_update
IDENTITY_ARGS = ("user_id", "account", "subscriber", "msisdn", "sub")

# оператору: справочные чтения без данных «своего» абонента
_OPERATOR_READ = ("list_tariffs", "search_kb")


@dataclass
class Allowlist:
    sub: str
    role: str
    scopes: list[str] = field(default_factory=list)
    read_ops: list[str] = field(default_factory=list)
    write_ops: list[str] = field(default_factory=list)

    @property
    def operations(self) -> set[str]:
        return set(self.read_ops) | set(self.write_ops)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def allowlist_from_claims(claims: dict) -> Allowlist:
    """Роль даёт базовый набор операций, scope его сужает."""
    role = claims.get("role", "")
    scopes = str(claims.get("scope", "")).split()
    read: list[str] = []
    write: list[str] = []
    if role == "client":
        read = list(READ_OPS)
        if "tariffs:write" in scopes:  # карточные действия — только с правом записи
            write = list(WRITE_OPS)
    elif role == "operator":
        read = [*OPERATOR_OPS, *_OPERATOR_READ]  # обзор абонентов, записи нет
    read += [op for op in ESCALATION_OPS]  # позвать человека можно всегда
    return Allowlist(sub=claims.get("sub", ""), role=role, scopes=scopes, read_ops=read, write_ops=write)


def visible_tools(allowlist: Allowlist, names: list[str]) -> list[str]:
    """Барьер 1: модель видит только разрешённое; execute_action не виден никому."""
    allowed = allowlist.operations
    return [n for n in names if n in allowed]


def admit(allowlist: Allowlist, operation: str, args: dict | None) -> Decision:
    """Барьер 2: допуск тройки «субъект, операция, объект» в момент вызова."""
    if operation not in allowlist.operations:
        return Decision(False, f"operation_not_allowed:{operation}")
    if operation in OPERATOR_OPS:  # чужой идентификатор легитимен: роль проверит сервер
        return Decision(True)
    for key in (args or {}):
        if str(key).lower() in IDENTITY_ARGS:
            return Decision(False, f"object_from_arguments:{key}")
    return Decision(True)
