"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Модели видны prepare_* и create_ticket; execute_action вызывает только код
оркестратора. sub пока приходит аргументом, подставляет его обёртка
оркестратора — модель его не видит (на шаге прав аргумент уйдёт).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from actions import service as svc
from actions.contracts import ActionError

ACTION_ERROR_CODE = -32002

actions = MCPServer(
    name="actions",
    instructions="Действия абонента: prepare_* готовят карточку, execute_action исполняет после подтверждения.",
)


def _fail(e: ActionError) -> MCPError:
    return MCPError(code=ACTION_ERROR_CODE, message=e.code, data=e.to_tool_result())


@actions.tool()
async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Подготовить смену тарифа: проверяет условия и возвращает карточку. Ничего не меняет."""
    try:
        return await svc.prepare_tariff_change(sub, tariff)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def prepare_toggle_service(sub: str, service: str, enabled: bool) -> dict:
    """Подготовить включение (enabled=true) или отключение услуги. Ничего не меняет."""
    try:
        return await svc.prepare_toggle_service(sub, service, enabled)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def prepare_contacts_update(sub: str, phone: str | None = None, email: str | None = None) -> dict:
    """Подготовить смену телефона и/или почты. Ничего не меняет."""
    try:
        return await svc.prepare_contacts_update(sub, phone, email)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def execute_action(sub: str, action: str, details_json: str, pending_id: str,
                         idempotency_key: str) -> dict:
    """НЕ ДЛЯ МОДЕЛИ: исполнение подтверждённого действия, вызывает код оркестратора."""
    try:
        return await svc.execute_action(sub, action, details_json, pending_id, idempotency_key)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def create_ticket(sub: str, subject: str, note: str = "") -> dict:
    """Эскалировать обращение оператору поддержки (тикет, без карточки)."""
    try:
        return await svc.create_ticket(sub, subject, note)
    except ActionError as e:
        raise _fail(e) from None


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "actions"})


_mcp_app = actions.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    """Session manager живёт в event loop uvicorn."""
    async with actions.session_manager.run():
        yield


app = Starlette(
    routes=[Route("/health", health), Mount("/actions", app=_mcp_app)],
    lifespan=lifespan,
)
