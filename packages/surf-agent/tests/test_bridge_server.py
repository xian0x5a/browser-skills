"""Layer 2 of plans/concurrent-bridge.md: the HTTP bridge server under concurrent clients.

A real PatchrightRuntime (so its real per-thread locks run) is served over HTTP with a fake
started browser context. The server's event loop runs in another thread, so gates are
threading.Events polled from the loop; every wait is bounded so a regression fails, not hangs.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

import surf_agent.backends.local_bridge as local_bridge
from surf_agent.errors import BridgeUnavailable
from surf_agent.backends.patchright.backend import PatchrightBridgeClient
from surf_agent.backends.patchright.bridge import PageSlot, PatchrightRuntime
from surf_agent.backends.patchright.constants import CONTEXT_RESTART_REQUIRED
from runtime_driver import running_bridge

WAIT_S = 3.0
POLL_S = 0.01
PROMPT_S = 1.0
# The gate a test never releases; long enough to outlive every bounded wait below.
NEVER_S = 30.0


async def poll_gate(gate: threading.Event | None) -> None:
    """Wait on a gate set from the test thread without blocking the server's loop."""
    deadline = time.monotonic() + NEVER_S
    while gate is not None and not gate.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("gate was never released")
        await asyncio.sleep(POLL_S)


class FakePage:
    def __init__(self, target_id: str):
        self.target_id = target_id
        self.closed = False
        self.sleep_gate: threading.Event | None = None
        self.sleep_started = threading.Event()

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def wait_for_timeout(self, milliseconds):
        self.sleep_started.set()
        deadline = time.monotonic() + NEVER_S
        while self.sleep_gate is not None and not self.sleep_gate.is_set():
            if self.closed:
                # What Patchright raises when the browser closes under an awaiting call.
                raise RuntimeError("Target page, context or browser has been closed")
            if time.monotonic() > deadline:
                raise AssertionError("gated sleep was never released or cancelled")
            await asyncio.sleep(POLL_S)


class FakeSession:
    """Answers the CDP calls window creation sends, from the context's page list."""

    def __init__(self, context, page=None):
        self.context = context
        self.page = page

    async def send(self, method, params=None):
        context = self.context
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": self.page.target_id}}
        if method == "Page.resetNavigationHistory":
            return {}
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {"targetId": page.target_id, "type": "page"}
                    for page in context.pages
                    if not page.is_closed()
                ]
            }
        if method == "Target.closeTarget":
            for page in context.pages:
                if page.target_id == params["targetId"]:
                    page.closed = True
            return {"success": True}
        if method == "Target.createTarget":
            context.create_started.set()
            await poll_gate(context.create_gate)
            context.created += 1
            page = FakePage(f"created-{context.created}")
            context.pages.append(page)
            return {"targetId": page.target_id}
        raise AssertionError(f"unexpected CDP call {method}")

    async def detach(self):
        pass


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.created = 0
        self.create_started = threading.Event()
        self.create_gate: threading.Event | None = None
        self.browser = types.SimpleNamespace(new_browser_cdp_session=lambda: FakeSession(self))

    def new_cdp_session(self, page):
        return FakeSession(self, page)

    def new_page(self):
        raise AssertionError("a new page would open a foreground window")

    def on(self, event, handler):
        pass


class FakeManager:
    """Stands in for the Patchright manager: exiting it closes the browser's pages."""

    def __init__(self, context: FakeContext):
        self.context = context
        self.exit_started = threading.Event()
        self.exit_gate: threading.Event | None = None

    async def __aexit__(self, *exc):
        self.exit_started.set()
        await poll_gate(self.exit_gate)
        for page in self.context.pages:
            page.closed = True


@pytest.fixture(autouse=True)
def forbid_real_bridge(monkeypatch):
    # The client spawns a real bridge (and browser) when health fails; never in tests.
    def popen(*args, **kwargs):
        raise AssertionError("tests must not spawn a real bridge")

    monkeypatch.setattr(local_bridge.subprocess, "Popen", popen)


@pytest.fixture
def runtime(tmp_path):
    runtime = PatchrightRuntime(profile_dir=tmp_path / "profile")
    context = FakeContext()
    runtime.browser_or_context = context
    runtime.manager = FakeManager(context)
    return runtime


def register_page(runtime: PatchrightRuntime, thread: str) -> FakePage:
    page = FakePage(f"existing-{thread}")
    runtime.browser_or_context.pages.append(page)
    runtime.pages[thread] = PageSlot(page=page, page_id=len(runtime.pages) + 100, target_id=f"{thread}-target")
    return page


def client(runtime: PatchrightRuntime, port: int, timeout_s: float = WAIT_S) -> PatchrightBridgeClient:
    return PatchrightBridgeClient(timeout_s=timeout_s, port=port, profile_dir=runtime.profile_dir)


def raw_call(port: int, name: str, args: dict) -> tuple[int, dict]:
    # Raw HTTP: the client would answer CONTEXT_RESTART_REQUIRED by relaunching a bridge.
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/call",
        data=json.dumps({"name": name, "args": args}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WAIT_S) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def health_status(port: int) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=WAIT_S) as response:
        return response.status


def wait_until(event: threading.Event, what: str) -> None:
    assert event.wait(WAIT_S), f"timed out waiting for {what}"


def test_calls_on_different_threads_overlap(runtime):
    page_a = register_page(runtime, "a")
    page_b = register_page(runtime, "b")
    page_a.sleep_gate = threading.Event()
    with running_bridge(runtime) as (_server, port, _thread), ThreadPoolExecutor(2) as pool:
        call_a = pool.submit(client(runtime, port).call_tool, "sleep", {"thread": "a", "milliseconds": 1})
        wait_until(page_a.sleep_started, "thread a's call to block")

        call_b = pool.submit(client(runtime, port).call_tool, "sleep", {"thread": "b", "milliseconds": 1})

        assert call_b.result(timeout=WAIT_S) == "waited\n"
        assert page_b.sleep_started.is_set()
        assert not call_a.done()
        page_a.sleep_gate.set()
        assert call_a.result(timeout=WAIT_S) == "waited\n"


def test_health_answers_quickly_while_a_call_is_in_flight(runtime):
    page = register_page(runtime, "a")
    page.sleep_gate = threading.Event()
    with running_bridge(runtime) as (_server, port, _thread), ThreadPoolExecutor(1) as pool:
        call = pool.submit(client(runtime, port).call_tool, "sleep", {"thread": "a", "milliseconds": 1})
        wait_until(page.sleep_started, "the call to block")

        started = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=WAIT_S) as response:
            payload = json.loads(response.read().decode())
        elapsed = time.monotonic() - started

        assert payload == runtime.health_payload()
        assert elapsed < 0.5
        page.sleep_gate.set()
        call.result(timeout=WAIT_S)


def test_abandoned_new_call_still_registers_its_window(runtime):
    context = runtime.browser_or_context
    context.create_gate = threading.Event()
    with running_bridge(runtime) as (_server, port, thread):
        with pytest.raises(BridgeUnavailable):
            client(runtime, port, timeout_s=0.5).call_tool("new", {"thread": "a"})
        assert context.create_started.is_set()

        context.create_gate.set()
        deadline = time.monotonic() + WAIT_S
        while "a" not in runtime.pages and time.monotonic() < deadline:
            time.sleep(POLL_S)
        assert "a" in runtime.pages, "the abandoned call's window was never registered"
        created = runtime.pages["a"].page
        assert created.target_id == "created-1" and not created.is_closed()

        for name in list(runtime.pages):
            assert client(runtime, port).call_tool("close", {"thread": name}) == "closed\n"

        thread.join(timeout=WAIT_S)
        assert not thread.is_alive(), "a stray window kept the bridge busy"


def test_calls_during_a_running_stop_get_restart_required_and_server_waits_for_stop(runtime):
    register_page(runtime, "a")
    runtime.manager.exit_gate = threading.Event()
    with running_bridge(runtime) as (_server, port, thread), ThreadPoolExecutor(1) as pool:
        stop = pool.submit(client(runtime, port).call_tool, "stop", {})
        wait_until(runtime.manager.exit_started, "the stop to begin")

        status, body = raw_call(port, "sleep", {"thread": "a", "milliseconds": 1})

        assert status == 500
        assert body["error"] == CONTEXT_RESTART_REQUIRED
        time.sleep(0.2)
        assert thread.is_alive()
        assert health_status(port) == 200, "the port must stay bound while the stop runs"
        assert not stop.done()

        runtime.manager.exit_gate.set()
        assert stop.result(timeout=WAIT_S) == "stopped\n"
        thread.join(timeout=WAIT_S)
        assert not thread.is_alive()


def test_stop_returns_promptly_and_server_exits_despite_in_flight_call(runtime):
    page = register_page(runtime, "a")
    page.sleep_gate = threading.Event()  # never released
    with running_bridge(runtime) as (server, port, thread), ThreadPoolExecutor(1) as pool:
        pool.submit(client(runtime, port).call_tool, "sleep", {"thread": "a", "milliseconds": 1})
        wait_until(page.sleep_started, "the call to block")

        started = time.monotonic()
        result = client(runtime, port).call_tool("stop", {})

        assert result == "stopped\n"
        assert time.monotonic() - started < PROMPT_S
        thread.join(timeout=WAIT_S)
        assert not thread.is_alive()


def test_final_close_exits_the_server_after_answering(runtime):
    register_page(runtime, "a")
    with running_bridge(runtime) as (server, port, thread):

        assert client(runtime, port).call_tool("close", {"thread": "a"}) == "closed\n"

        thread.join(timeout=WAIT_S)
        assert not thread.is_alive()
