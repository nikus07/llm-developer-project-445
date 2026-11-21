"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Сервер ресурса: каждый вызов несёт Bearer-токен (подпись, издатель, аудитория
и срок проверяются). Субъект берётся из проверенного токена, аргумента sub
в контрактах нет — подменять нечего. execute_action вызывает только код
оркестратора.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from actions import service as svc
from actions.contracts import ActionError
from core.config import RESOURCES
from core.server_auth import make_mcp_server

ACTION_ERROR_CODE = -32002

actions = make_mcp_server(
    "actions",
    resource=RESOURCES["actions"],
    instructions="Действия абонента: prepare_* готовят карточку, execute_action исполняет после подтверждения.",
)


def _sub() -> str:
    """Субъект операции — из проверенного токена вызова."""
    token = get_access_token()
    assert token is not None, "вызов без аутентифицированного токена"
    return token.subject


def _fail(e: ActionError) -> MCPError:
    return MCPError(code=ACTION_ERROR_CODE, message=e.code, data=e.to_tool_result())


@actions.tool()
async def prepare_tariff_change(tariff: str) -> dict:
    """Подготовить смену тарифа: проверяет условия и возвращает карточку. Ничего не меняет."""
    try:
        return await svc.prepare_tariff_change(_sub(), tariff)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def prepare_toggle_service(service: str, enabled: bool) -> dict:
    """Подготовить включение (enabled=true) или отключение услуги. Ничего не меняет."""
    try:
        return await svc.prepare_toggle_service(_sub(), service, enabled)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def prepare_contacts_update(phone: str | None = None, email: str | None = None) -> dict:
    """Подготовить смену телефона и/или почты. Ничего не меняет."""
    try:
        return await svc.prepare_contacts_update(_sub(), phone, email)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def execute_action(action: str, details_json: str, pending_id: str, idempotency_key: str) -> dict:
    """НЕ ДЛЯ МОДЕЛИ: исполнение подтверждённого действия, вызывает код оркестратора."""
    try:
        return await svc.execute_action(_sub(), action, details_json, pending_id, idempotency_key)
    except ActionError as e:
        raise _fail(e) from None


@actions.tool()
async def create_ticket(subject: str, note: str = "") -> dict:
    """Эскалировать обращение оператору поддержки (тикет, без карточки)."""
    try:
        return await svc.create_ticket(_sub(), subject, note)
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
