"""Layer 1 of plans/concurrent-bridge.md: the Patchright runtime under interleaved calls.

Each test runs several `PatchrightRuntime.call_async` tasks on one event loop against
async fakes. Fakes yield (`asyncio.sleep`) or wait on `asyncio.Event`s at chosen awaits
so the calls interleave there; every wait is bounded so a regression fails, not hangs.
"""

import asyncio
import types

import pytest

import surf_agent.backends.patchright.bridge as bridge
from surf_agent.backends.patchright.bridge import PageSlot, PatchrightRuntime
from surf_agent.backends.patchright.constants import CONTEXT_RESTART_REQUIRED

WAIT_S = 3.0
# Long enough for a concurrent call to reach the same await, short enough to keep tests fast.
INTERLEAVE_S = 0.05


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, timeout=WAIT_S)


class FakePage:
    def __init__(self, target_id: str):
        self.target_id = target_id
        self.url = "about:blank"
        self.closed = False
        self.sleep_gate: asyncio.Event | None = None
        self.events: list[str] = []

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, wait_until=None):
        self.url = url

    async def wait_for_timeout(self, milliseconds):
        self.events.append("sleep-start")
        if self.sleep_gate is not None:
            await self.sleep_gate.wait()
        self.events.append("sleep-end")

    async def evaluate(self, code):
        self.events.append("eval")
        return 42


class FakeSession:
    """Answers the CDP calls the runtime sends, from the context's page list."""

    def __init__(self, context, page=None):
        self.context = context
        self.page = page

    async def send(self, method, params=None):
        context = self.context
        if context.closed:
            await asyncio.sleep(INTERLEAVE_S)
            raise RuntimeError(bridge.CLOSED_TARGET_MESSAGE)
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
            context.create_entered.set()
            if context.create_gate is not None:
                await context.create_gate.wait()
            context.created += 1
            page = FakePage(f"target-{context.created}")
            # Chrome lists the new target before answering createTarget.
            context.pages.append(page)
            await asyncio.sleep(INTERLEAVE_S)
            return {"targetId": page.target_id}
        raise AssertionError(f"unexpected CDP call {method}")

    async def detach(self):
        pass


class FakeContext:
    def __init__(self, *, closed: bool = False):
        self.pages: list[FakePage] = []
        self.closed = closed
        self.created = 0
        self.create_entered = asyncio.Event()
        self.create_gate: asyncio.Event | None = None
        self.browser = types.SimpleNamespace(new_browser_cdp_session=lambda: FakeSession(self))

    def new_page(self):
        raise AssertionError("a new page would open a foreground window")

    def new_cdp_session(self, page):
        return FakeSession(self, page)

    def on(self, event, handler):
        pass


def started_runtime(tmp_path, context: FakeContext) -> PatchrightRuntime:
    runtime = PatchrightRuntime(profile_dir=tmp_path / "profile")
    runtime.browser_or_context = context
    return runtime


def register_page(runtime: PatchrightRuntime, context: FakeContext, thread: str) -> FakePage:
    page = FakePage(f"existing-{thread}")
    context.pages.append(page)
    runtime.pages[thread] = PageSlot(page=page, page_id=len(runtime.pages) + 100)
    return page


def forbid_launch(monkeypatch, launches: list):
    def async_playwright():
        launches.append(True)
        raise AssertionError("a stopping bridge must not relaunch the browser")

    monkeypatch.setattr(bridge, "async_playwright", async_playwright)


def test_concurrent_new_windows_both_survive_registered_to_their_threads(tmp_path, monkeypatch):
    # Without serialized window creation, one window's sweep closes the other's
    # window, and the other then waits the full window timeout; keep it short.
    monkeypatch.setattr(bridge, "CDP_NEW_WINDOW_TIMEOUT_S", 0.5)

    async def scenario():
        context = FakeContext()
        runtime = started_runtime(tmp_path, context)
        results = await bounded(
            asyncio.gather(
                runtime.call_async("new", {"thread": "a"}),
                runtime.call_async("new", {"thread": "b"}),
                return_exceptions=True,
            )
        )
        return context, runtime, results

    context, runtime, results = asyncio.run(scenario())

    assert [r for r in results if isinstance(r, BaseException)] == []
    page_a, page_b = runtime.pages["a"].page, runtime.pages["b"].page
    assert page_a is not page_b
    assert not page_a.is_closed() and not page_b.is_closed()
    assert {page_a, page_b} <= set(context.pages)


def test_two_first_calls_launch_the_browser_once(tmp_path, monkeypatch):
    launches = []

    class FakeChromium:
        async def launch_persistent_context(self, **options):
            launches.append(options)
            await asyncio.sleep(INTERLEAVE_S)
            return FakeContext()

    class FakeManager:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *exc):
            pass

    async def capture_default_args(chromium, **options):
        return []

    monkeypatch.setattr(bridge, "async_playwright", FakeManager)
    monkeypatch.setattr(bridge, "capture_default_args", capture_default_args)

    async def scenario():
        runtime = PatchrightRuntime(profile_dir=tmp_path / "profile")
        return await bounded(
            asyncio.gather(
                runtime.call_async("new", {"thread": "a"}),
                runtime.call_async("new", {"thread": "b"}),
                return_exceptions=True,
            )
        )

    results = asyncio.run(scenario())

    assert len(launches) == 1
    assert [r for r in results if isinstance(r, BaseException)] == []


def test_closing_last_page_while_another_window_is_mid_creation_does_not_idle_stop(tmp_path):
    async def scenario():
        context = FakeContext()
        context.create_gate = asyncio.Event()
        runtime = started_runtime(tmp_path, context)
        register_page(runtime, context, "a")

        creating_b = asyncio.create_task(runtime.call_async("new", {"thread": "b"}))
        await bounded(context.create_entered.wait())

        await bounded(runtime.call_async("close", {"thread": "a"}))
        runtime.after_response("close")
        shutdown_while_b_creates = runtime.shutdown_requested

        context.create_gate.set()
        b_result = await bounded(creating_b)
        return runtime, shutdown_while_b_creates, b_result

    runtime, shutdown_while_b_creates, b_result = asyncio.run(scenario())

    assert shutdown_while_b_creates is False
    assert b_result.startswith("opened")
    assert not runtime.pages["b"].page.is_closed()


def test_long_call_on_one_thread_does_not_delay_another_thread(tmp_path):
    async def scenario():
        context = FakeContext()
        runtime = started_runtime(tmp_path, context)
        page_a = register_page(runtime, context, "a")
        register_page(runtime, context, "b")
        page_a.sleep_gate = asyncio.Event()

        sleeping_a = asyncio.create_task(runtime.call_async("sleep", {"thread": "a", "milliseconds": 60000}))
        b_result = await bounded(runtime.call_async("eval", {"thread": "b", "code": "1"}))
        a_still_blocked = not sleeping_a.done()

        page_a.sleep_gate.set()
        await bounded(sleeping_a)
        return b_result, a_still_blocked

    b_result, a_still_blocked = asyncio.run(scenario())

    assert b_result == "42\n"
    assert a_still_blocked


def test_calls_on_the_same_thread_run_in_order(tmp_path):
    async def scenario():
        context = FakeContext()
        runtime = started_runtime(tmp_path, context)
        page_a = register_page(runtime, context, "a")
        page_a.sleep_gate = asyncio.Event()

        first = asyncio.create_task(runtime.call_async("sleep", {"thread": "a", "milliseconds": 60000}))
        await asyncio.sleep(INTERLEAVE_S)
        second = asyncio.create_task(runtime.call_async("eval", {"thread": "a", "code": "1"}))
        await asyncio.sleep(INTERLEAVE_S)
        events_while_first_blocked = list(page_a.events)

        page_a.sleep_gate.set()
        await bounded(asyncio.gather(first, second))
        return events_while_first_blocked, page_a.events

    events_while_first_blocked, events = asyncio.run(scenario())

    assert events_while_first_blocked == ["sleep-start"]
    assert events == ["sleep-start", "sleep-end", "eval"]


def test_calls_on_a_closed_context_stop_once_and_later_calls_require_restart(tmp_path, monkeypatch):
    launches = []
    forbid_launch(monkeypatch, launches)

    async def scenario():
        context = FakeContext(closed=True)
        runtime = started_runtime(tmp_path, context)
        stops = []
        original_stop = runtime._stop_async

        async def counting_stop():
            stops.append(True)
            await asyncio.sleep(INTERLEAVE_S)
            return await original_stop()

        runtime._stop_async = counting_stop
        in_flight = await bounded(
            asyncio.gather(
                runtime.call_async("new", {"thread": "a"}),
                runtime.call_async("new", {"thread": "b"}),
                return_exceptions=True,
            )
        )
        late = await bounded(
            asyncio.gather(runtime.call_async("new", {"thread": "c"}), return_exceptions=True)
        )
        return stops, in_flight, late[0]

    stops, in_flight, late = asyncio.run(scenario())

    assert len(stops) == 1
    for result in in_flight:
        assert isinstance(result, RuntimeError) and CONTEXT_RESTART_REQUIRED in str(result), result
    assert isinstance(late, RuntimeError) and CONTEXT_RESTART_REQUIRED in str(late), late
    assert launches == []
