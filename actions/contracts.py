"""Контракты действий: схемы, нормализация, таксономия ошибок.

Три класса исходов:
  * ошибка валидации (unknown_tariff, bad_phone…) — модель читает подсказки
    в ответе и исправляет вызов;
  * бизнес-правило (BUSINESS_RULE_CODES) — повторять бессмысленно, модель
    пересказывает отказ абоненту;
  * сбой инфраструктуры — до модели не доходит (повтор прячет клиент MCP).
"""
from __future__ import annotations

import difflib
import os.path
import re

from pydantic import BaseModel

# Отказы по бизнес-правилам: повторять вызов бессмысленно.
BUSINESS_RULE_CODES = frozenset({
    "same_tariff",
    "tariff_not_available",
    "service_already_on",
    "service_already_off",
    "service_not_available",
    "same_contacts",
    "nothing_to_update",
})


class ActionError(Exception):
    """Отказ действия: код, сообщение и подсказки для модели."""

    def __init__(self, code: str, message: str = "", **hints) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.hints = hints

    @property
    def is_business_rule(self) -> bool:
        return self.code in BUSINESS_RULE_CODES

    def to_tool_result(self) -> dict:
        return {"error": self.code, "detail": self.message, **self.hints}


class TariffChange(BaseModel):
    tariff: str


class ServiceToggle(BaseModel):
    service: str
    enabled: bool


class ContactsUpdate(BaseModel):
    phone: str | None = None
    email: str | None = None


def _clean(raw: str) -> str:
    text = (raw or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[\"'«»“”„]", "", text)
    text = re.sub(r"^(тариф|услугу|услуга|услуги)\s+", "", text)
    return text.strip()


def _best_match(raw: str, names: list[str]) -> str | None:
    """Название «как расслышала модель» → точное название из списка."""
    want = _clean(raw)
    if not want:
        return None
    index = {_clean(n): n for n in names}
    if want in index:
        return index[want]
    for key, name in index.items():  # падежные формы: «семейного», «музыку»
        common = len(os.path.commonprefix([key, want]))
        if common >= max(4, min(len(key), len(want)) - 3):
            return name
    close = difflib.get_close_matches(want, list(index), n=1, cutoff=0.75)
    return index[close[0]] if close else None


def normalize_tariff(raw: str, catalog: list[dict]) -> str:
    """Точное имя тарифа из каталога; иначе unknown_tariff с перечнем доступных."""
    found = _best_match(raw, [t["name"] for t in catalog])
    if found is None:
        raise ActionError(
            "unknown_tariff",
            f"тарифа «{raw}» нет в каталоге",
            available=[t["name"] for t in catalog if t.get("available", True)],
        )
    return found


def normalize_service(raw: str, catalog: list[str]) -> str:
    found = _best_match(raw, catalog)
    if found is None:
        raise ActionError("unknown_service", f"услуги «{raw}» нет", available=list(catalog))
    return found


def normalize_phone(raw: str) -> str:
    """11 цифр → «+7 900 111-22-33»; иначе bad_phone с примером."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if len(digits) != 11 or digits[0] not in "78":
        raise ActionError("bad_phone", "номер должен содержать 11 цифр", example="+7 900 111-22-33")
    d = digits[1:]
    return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not _EMAIL.match(email):
        raise ActionError("bad_email", "адрес почты некорректен", example="name@example.ru")
    return email
