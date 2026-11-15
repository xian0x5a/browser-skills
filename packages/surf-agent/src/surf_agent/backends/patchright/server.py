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
# Status for a call abandoned by its client; nobody reads the response.
CLIENT_CLOSED_REQUEST_STATUS = 499


def create_bridge_server(runtime: Any, port: int) -> uvicorn.Server:
    server: uvicorn.Server

    def request_exit() -> None:
        server.should_exit = True

    async def health(_request: Request) -> Response:
        return JSONResponse(runtime.health_payload())

    async def call(request: Request) -> Response:
        payload = await request.json()
        name = str(payload.get("name") or "")
        call_task = asyncio.ensure_future(runtime.call_async(name, payload.get("args") or {}))
        if not await _finished_before_disconnect(call_task, request):
            return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
        try:
            result = call_task.result()
        except Exception as exc:
            code = exc.code if isinstance(exc, BridgeCodedError) else None
            body: dict[str, Any] = {"error": str(exc), "code": code}
            return JSONResponse(body, status_code=500, background=BackgroundTask(finish, name))
        return JSONResponse({"result": result}, background=BackgroundTask(finish, name))

    async def finish(name: str) -> None:
        # Runs after the response is sent, so a final close or stop answers its caller first.
        if await runtime.finish_response(name):
            request_exit()

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


async def _finished_before_disconnect(call_task: asyncio.Future[str], request: Request) -> bool:
    """Wait for the call; cancel it if its client disconnects first.

    A client that timed out has abandoned the call. Left running, the call would hold its
    Surf thread's lock while health still answers, wedging that thread unseen.
    """
    disconnect = asyncio.ensure_future(_wait_for_disconnect(request))
    try:
        await asyncio.wait({call_task, disconnect}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # Graceful-shutdown timeout cancels this request; take the call down with it.
        call_task.cancel()
        raise
    finally:
        disconnect.cancel()
    if call_task.done():
        return True
    call_task.cancel()
    return False


async def _wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass
