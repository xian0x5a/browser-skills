"""Live contract: the bridge stops itself once a human has closed its last page (issue #77).

Local pages only; never loads a remote site. The runtime sits behind the real HTTP server,
so the page is closed on the server's own event loop, which owns the Patchright objects.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from surf_agent.backends.patchright.bridge import PatchrightRuntime
from runtime_driver import BRIDGE_EXIT_TIMEOUT_S, running_bridge

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

PAGE_URL = "data:text/html,<title>Local</title>"
CALL_TIMEOUT_S = 30.0
LOOP_HOP_TIMEOUT_S = 10.0
# Long enough that the wait-for is still running when the human closes the window.
WAIT_FOR_TIMEOUT_MS = 20_000
WAIT_FOR_STARTED_S = 1.0


class ServedRuntime:
    """A live headless runtime behind running_bridge, with the loop it runs on captured."""

    def __init__(self, runtime: PatchrightRuntime, port: int) -> None:
        self.runtime = runtime
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None

    def call(self, name: str, args: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/call",
            data=json.dumps({"name": name, "args": args}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def close_page_like_a_human(self, thread: str) -> None:
        # No Surf command: the page just goes away, as when its window is closed by hand.
        page = self.runtime.pages[thread].page
        asyncio.run_coroutine_threadsafe(page.close(), self.loop).result(LOOP_HOP_TIMEOUT_S)


@pytest.fixture
def served(tmp_path):
    runtime = PatchrightRuntime(profile_dir=tmp_path / "profile", headless=True)
    served = ServedRuntime(runtime, port=0)
    original_call_async = runtime.call_async

    async def capture_loop(name, args):
        served.loop = asyncio.get_running_loop()
        return await original_call_async(name, args)

    runtime.call_async = capture_loop
    with running_bridge(runtime) as (server, port, thread):
        served.port = port
        served.server_thread = thread
        try:
            yield served
        finally:
            # Ends the browser on every path, so a failing test leaves no Chrome behind.
            if thread.is_alive():
                served.call("stop", {})


def assert_bridge_stopped(served: ServedRuntime) -> None:
    served.server_thread.join(timeout=BRIDGE_EXIT_TIMEOUT_S)
    assert not served.server_thread.is_alive(), "bridge server still up after its last page was closed by hand"
    assert served.runtime.browser_or_context is None, "browser still running after its last page was closed by hand"


def test_bridge_stops_after_a_human_closes_every_thread_window(served):
    for thread in ("first", "second"):
        status, _ = served.call("open", {"thread": thread, "url": PAGE_URL})
        assert status == 200

    served.close_page_like_a_human("first")
    served.close_page_like_a_human("second")

    assert_bridge_stopped(served)


def test_bridge_stops_after_a_human_closes_the_window_of_a_running_call(served):
    status, _ = served.call("open", {"thread": "waiting", "url": PAGE_URL})
    assert status == 200

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(
            served.call, "wait-for", {"thread": "waiting", "text": "never shown", "timeoutMs": WAIT_FOR_TIMEOUT_MS}
        )
        threading.Event().wait(WAIT_FOR_STARTED_S)
        served.close_page_like_a_human("waiting")
        status, body = waiting.result(timeout=CALL_TIMEOUT_S)

    assert (status, body["code"]) == (500, "page_closed")
    assert_bridge_stopped(served)
