"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  подтверждение карточки (с контрактов)

События SSE хода: plan / card / reply / error / done.

TODO(агенты):
  1. lifespan: подключить LLM (config.setup_model(); в бонусе failover —
     llm_pool.setup_pool(), если задан LLM_SECONDARY_BASE_URL).
  2. CORS: allow_origins=["*"] — фронтенд живёт на другом порту стенда.
  3. GET /api/health.
  4. POST /api/session: sessions.open_session(login); ответ: session_id,
     name, role, visible_tools — инструменты allowlist сессии
     (с правами — permissions.visible_tools(...), до них — весь пул).
  5. POST /api/chat: события хода через asyncio.Queue гонятся
     StreamingResponse'ом (media_type="text/event-stream", формат
     'data: {json}\\n\\n'); сбой хода — событие error, не падение потока.

TODO(контракты): POST /api/cards/{pending_id}/decision — кнопка карточки:
  «да» → orchestrator.execute_pending, «нет» → отмена.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import orchestrator, permissions, sessions
from core import config


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.setup_model()
    yield


app = FastAPI(title="operator-chat backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class OpenSession(BaseModel):
    user: str


class Decision(BaseModel):
    session_id: str
    answer: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "backend"}


@app.post("/api/session")
async def open_session(body: OpenSession):
    try:
        session = await sessions.open_session(body.user)
    except KeyError as e:
        return JSONResponse({"error": str(e).strip("'\"")}, status_code=400)
    return {
        "session_id": session.session_id,
        "name": session.user.name,
        "role": session.user.role,
        "visible_tools": permissions.visible_tools(session.allowlist, session.pool.tool_names()),
    }


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@app.post("/api/chat")
async def chat(body: ChatRequest):
    session = sessions.get(body.session_id)
    if session is None:
        return JSONResponse({"error": "unknown_session"}, status_code=404)

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def turn() -> None:
        try:
            await orchestrator.run_turn(session, body.message, emit)
        except Exception as e:  # noqa: BLE001 — сбой хода — событие error, не падение потока
            await queue.put({"type": "error", "detail": f"{type(e).__name__}: {e}"[:300]})
        finally:
            await queue.put({"type": "done"})

    async def stream():
        task = asyncio.create_task(turn())
        try:
            while True:
                event = await queue.get()
                yield _sse(event)
                if event["type"] == "done":
                    break
        finally:
            await task

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/cards/{pending_id}/decision")
async def card_decision(pending_id: str, body: Decision):
    session = sessions.get(body.session_id)
    if session is None:
        return JSONResponse({"error": "unknown_session"}, status_code=404)
    return await orchestrator.decide(session, pending_id, "yes" if body.answer == "yes" else "no")
