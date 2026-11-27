"""Live contract: a thread window that never has OS focus still behaves like a foreground page.

Each named thread is its own Chrome window, created in the background, so it sits out
of view and never has OS focus. Patchright's focus emulation keeps such pages visible
and focused so they render, and Surf relies on it (docs/adr/0011). Because a background
window never has OS focus, the hasFocus() assertion fails whenever a Patchright release
stops emulating focus, wherever the compositor puts the window. It is read before any
input: a click gives the page focus, which would mask missing emulation.

Headed on purpose: headless Chrome has no windows. Local pages only.
"""

from __future__ import annotations

import asyncio
import os
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from runtime_driver import SyncRuntime
from surf_agent.backends.patchright.bridge import PatchrightRuntime
from test_patchright_screenshot_zoom import fails_if_still_running_after, page_value

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

FORM_PAGE = """<title>Form</title>
<input id="name">
<button id="go" onclick="document.body.dataset.clicks = (+(document.body.dataset.clicks || 0)) + 1">Go</button>"""

# An unfocused, hidden page renders no frames, so actionability checks never pass and
# actions run into Surf's 5s action timeout. A rendering page acts in well under a
# second; 2s leaves slack for a loaded machine while staying far below that timeout.
PROMPT_ACTION_S = 2.0
# Past this the run is hung, not slow: fail instead of blocking pytest.
HUNG_AFTER_S = 60.0

THREADS = ("one", "two")


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "form.html").write_text(FORM_PAGE)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.fixture
def launch(tmp_path):
    """Start a headed runtime on a fresh profile; Chrome opens no window until a thread asks."""
    runtimes: list[SyncRuntime] = []

    def start() -> SyncRuntime:
        runtime = SyncRuntime(PatchrightRuntime(profile_dir=tmp_path / "profile", headless=False))
        runtimes.append(runtime)
        runtime._run(runtime._start_async())
        return runtime

    try:
        yield start
    finally:
        for runtime in runtimes:
            runtime.stop()


def test_background_thread_windows_act_like_foreground_pages_concurrently(launch, site):
    runtime = launch()
    for thread in THREADS:
        runtime.call("new", {"thread": thread})
    page_state = "() => ({visibility: document.visibilityState, focused: document.hasFocus()})"
    states = {thread: page_value(runtime, page_state, thread=thread) for thread in THREADS}
    assert states == {thread: {"visibility": "visible", "focused": True} for thread in THREADS}
    durations: dict[tuple[str, str], float] = {}

    async def act(thread: str) -> None:
        steps = [
            ("open", {"url": f"{site}/form.html"}),
            ("fill", {"uid": "#name", "text": thread}),
            ("type", {"text": "+typed"}),
            ("click", {"uid": "#go"}),
        ]
        for name, args in steps:
            started = time.monotonic()
            await runtime.call_async(name, {"thread": thread, **args})
            durations[(thread, name)] = time.monotonic() - started
            # Hand the loop to the other thread's call between every step.
            await asyncio.sleep(0)

    async def act_concurrently() -> None:
        await asyncio.gather(*(act(thread) for thread in THREADS))

    with fails_if_still_running_after(HUNG_AFTER_S, "actions on background thread windows"):
        runtime._run(act_concurrently())
        pages = {
            thread: page_value(
                runtime,
                """() => ({
                    input: document.getElementById('name').value,
                    clicks: document.body.dataset.clicks,
                })""",
                thread=thread,
            )
            for thread in THREADS
        }

    assert pages == {
        thread: {"input": f"{thread}+typed", "clicks": "1"} for thread in THREADS
    }
    slow = {
        f"{thread} {name}": round(seconds, 2)
        for (thread, name), seconds in durations.items()
        if seconds > PROMPT_ACTION_S
    }
    assert slow == {}

