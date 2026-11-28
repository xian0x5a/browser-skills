"""Live contract: closing a thread's page also closes the pages it opened (issue #61).

A popup or target=_blank tab belongs to the thread whose page opened it, and so do
the popups that popup opens. Pages of other threads, and the popups those opened,
stay. Once the last thread closes, popups it left behind must not keep the bridge up.
"""

from __future__ import annotations

import asyncio
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from test_patchright_screenshot_zoom import fails_if_still_running_after, launch  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

# One page serves every role; the query string ("owner" thread, "role") tells pages apart in context.pages.
PAGE = """<title>Page</title>
<a id="child" target="_blank" href="/page.html?owner=%(owner)s&role=popup">child</a>
<a id="grandchild" target="_blank" href="/page.html?owner=%(owner)s&role=popup-of-popup">grandchild</a>"""
OWNERS = ("a", "b")

POPUPS_APPEAR_TIMEOUT_S = 10.0
# Past this the run is hung, not slow: fail instead of blocking pytest.
HUNG_AFTER_S = 90.0


# The popup keeps its renderer busy while it loads, so Patchright is slow to report it.
OPENER_PAGE = "<title>Opener</title>"
BUSY_POPUP_PAGE = "<title>Busy</title><script>const end = Date.now() + 1500; while (Date.now() < end) {}</script>"
# "click" waits until Patchright has reported the popup it opened, which leaves no race; "eval" returns at once.
OPEN_POPUP_CODE = "void window.open(%r)"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith(("/opener.html", "/busy.html")):
            body = (OPENER_PAGE if self.path.startswith("/opener") else BUSY_POPUP_PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        owner = self.path.split("owner=")[-1].split("&")[0] if "owner=" in self.path else "none"
        body = (PAGE % {"owner": owner}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def open_urls(runtime) -> list[str]:
    return [page.url for page in runtime.browser_or_context.pages if not page.is_closed()]


def urls_of(runtime, owner: str) -> list[str]:
    return sorted(url for url in open_urls(runtime) if f"owner={owner}" in url)


def wait_until_url_opens(runtime, url_end: str) -> None:
    deadline = time.monotonic() + POPUPS_APPEAR_TIMEOUT_S
    # Patchright delivers events only while its loop runs, so wait on that loop.
    while not any(url.endswith(url_end) for url in open_urls(runtime)):
        assert time.monotonic() < deadline, f"{url_end} never opened: {open_urls(runtime)}"
        runtime._run(asyncio.sleep(0.05))


def popup_page(runtime, owner: str, role: str):
    return next(page for page in runtime.browser_or_context.pages if page.url.endswith(f"owner={owner}&role={role}"))


def open_popups(runtime, site: str, owner: str) -> None:
    """Thread *owner* shows its page, follows its link, and the popup follows its own link."""
    runtime.call("open", {"thread": owner, "url": f"{site}/page.html?owner={owner}"})
    runtime.call("click", {"thread": owner, "uid": "#child"})
    wait_until_url_opens(runtime, f"owner={owner}&role=popup")
    runtime._run(popup_page(runtime, owner, "popup").click("#grandchild"))
    wait_until_url_opens(runtime, f"owner={owner}&role=popup-of-popup")


CLOSERS = {
    "close": lambda runtime, thread, site: runtime.call("close", {"thread": thread}),
    "close-matching": lambda runtime, thread, site: runtime.call("close-matching", {"pattern": thread}),
    "new": lambda runtime, thread, site: runtime.call("new", {"thread": thread}),
}


@pytest.mark.parametrize("closer", CLOSERS)
def test_closing_a_thread_page_closes_its_popups_and_leaves_other_threads(launch, site, closer):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, f"{closer} with popups"):
        for owner in OWNERS:
            open_popups(runtime, site, owner)
        assert len(urls_of(runtime, "a")) == 3 and len(urls_of(runtime, "b")) == 3

        # No wait after closing: the call returns only once its pages have closed.
        CLOSERS[closer](runtime, "a", site)

        assert urls_of(runtime, "a") == [], "popups of the closed thread are still open"
        assert len(urls_of(runtime, "b")) == 3, "the other thread or its popups were closed"

        if closer == "new":
            return
        CLOSERS[closer](runtime, "b", site)
        assert runtime.finish_response(closer) is True, f"bridge kept running; open pages: {open_urls(runtime)}"


def browser_session(runtime):
    return runtime._run(runtime.browser_or_context.browser.new_browser_cdp_session())


def page_targets(runtime, session) -> list[dict]:
    """Pages Chrome itself lists, including popups Patchright has not reported."""
    targets = runtime._run(session.send("Target.getTargets"))["targetInfos"]
    return [target for target in targets if target["type"] == "page"]


def test_closing_right_after_a_popup_opens_closes_the_popup(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "close right after popup"):
        runtime.call("open", {"thread": "race", "url": f"{site}/opener.html"})
        session = browser_session(runtime)
        # Chrome announces the popup the moment it exists, whether or not Patchright ever reports it.
        popups_seen: list[dict] = []
        session.on("Target.targetCreated", lambda event: popups_seen.append(event["targetInfo"]))
        runtime._run(session.send("Target.setDiscoverTargets", {"discover": True}))
        popups_seen.clear()

        # Cross-site (localhost vs 127.0.0.1), so the popup gets its own renderer and the opener stays responsive.
        popup_url = site.replace("127.0.0.1", "localhost") + "/busy.html?owner=race"
        runtime.call("eval", {"thread": "race", "code": OPEN_POPUP_CODE % popup_url})
        runtime.call("close", {"thread": "race"})

        # No wait: close returns only once Chrome has let the popup go, and the bridge
        # decides whether to stop right after that response.
        left = page_targets(runtime, session)
        assert [t for t in popups_seen if t["type"] == "page" and t.get("openerId")], "Chrome never opened the popup"
        assert left == [], f"pages of the closed thread are still open in Chrome: {[t['url'] for t in left]}"
        assert runtime.finish_response("close") is True, f"bridge kept running; open pages: {open_urls(runtime)}"
