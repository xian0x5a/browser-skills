"""HTTP adapter that serves one PatchrightRuntime to many agent processes concurrently.

Every request runs as its own task on uvicorn's event loop, which is also the loop that owns
the Patchright objects, so calls on different Surf threads overlap without crossing threads.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..bridge_common import BridgeCodedError

BRIDGE_HOST = "127.0.0.1"
# A wedged call must not keep a stopping bridge alive: uvicorn cancels it after this.
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5


def create_bridge_server(runtime: Any, port: int) -> uvicorn.Server:
    server: uvicorn.Server

    def request_exit() -> None:
        server.should_exit = True

    async def health(_request: Request) -> Response:
        return JSONResponse(runtime.health_payload())

    async def call(request: Request) -> Response:
        payload = await request.json()
        name = str(payload.get("name") or "")
        # A call runs to completion even if its client gives up: cancelling mid-launch,
        # mid-window-creation or mid-stop would strand half-made browser state. Each call
        # is bounded by its own Patchright timeouts; stop_bridge() recovers a wedged one.
        try:
            result = await runtime.call_async(name, payload.get("args") or {})
        except Exception as exc:
            code = exc.code if isinstance(exc, BridgeCodedError) else None
            body: dict[str, Any] = {"error": str(exc), "code": code}
            return JSONResponse(body, status_code=500, background=BackgroundTask(finish, name))
        return JSONResponse({"result": result}, background=BackgroundTask(finish, name))

    async def finish(name: str) -> None:
        # Runs after the response is sent, so a final close or stop answers its caller first.
        if await runtime.finish_response(name):
            request_exit()

    idle_checks: set[asyncio.Future[None]] = set()

    def check_idle() -> None:
        # No response follows a page closed outside a call, so nothing else would run finish.
        task = asyncio.ensure_future(finish("idle"))
        idle_checks.add(task)
        task.add_done_callback(idle_checks.discard)

    runtime.on_idle = check_idle

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        await runtime.stop_async()

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/call", call, methods=["POST"])],
        lifespan=lifespan,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=BRIDGE_HOST,
            port=port,
            loop="asyncio",
            lifespan="on",
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
    )
    return server

