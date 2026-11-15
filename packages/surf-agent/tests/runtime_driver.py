"""Drive the async Patchright bridge from synchronous tests."""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from surf_agent.backends.patchright.server import create_bridge_server

BRIDGE_START_TIMEOUT_S = 10.0
BRIDGE_EXIT_TIMEOUT_S = 15.0

# Runners opened by SyncRuntime during a test; conftest closes them at teardown.
open_runners: list[asyncio.Runner] = []


class SyncRuntime:
    """Wraps a PatchrightRuntime and runs its coroutines on one persistent event loop.

    Patchright objects and the runtime's locks belong to the loop they first ran on, so
    every call of one runtime must share a loop. Attribute reads and writes, including
    monkeypatched methods, pass through to the wrapped runtime.
    """

    def __init__(self, runtime: Any) -> None:
        runner = asyncio.Runner()
        open_runners.append(runner)
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "_runner", runner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.runtime, name, value)

    def _run(self, awaitable: Any) -> Any:
        return self._runner.run(awaitable)

    def call(self, name: str, args: dict[str, Any]) -> str:
        return self._run(self.runtime.call_async(name, args))

    def finish_response(self, name: str) -> bool:
        return self._run(self.runtime.finish_response(name))

    def stop(self) -> str:
        return self._run(self.runtime.stop_async())


class RunningBridge(NamedTuple):
    server: Any
    port: int
    thread: threading.Thread


@contextmanager
def running_bridge(runtime: Any) -> Iterator[RunningBridge]:
    """Serve ``runtime`` on an ephemeral port in a background thread."""
    server = create_bridge_server(runtime, port=0)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + BRIDGE_START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("bridge server did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield RunningBridge(server, port, thread)
    finally:
        server.should_exit = True
        thread.join(timeout=BRIDGE_EXIT_TIMEOUT_S)
