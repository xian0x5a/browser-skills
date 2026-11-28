"""Live contract: popups a thread's page opens become popup threads `<root>/<n>` (issue #71).

Local pages only; never loads a remote site.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from surf_agent.backends.bridge_common import BridgeCodedError
from surf_agent.backends.patchright.tabs import list_tabs
from surf_agent.errors import ErrorCode
from test_patchright_screenshot_zoom import fails_if_still_running_after, launch  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

HUNG_AFTER_S = 90.0
WAIT_TIMEOUT_S = 10.0
LOGIN_FEATURES = "popup,width=400,height=500"

PAGES = {
    "/app.html": """<title>App</title>
<button id="signin" onclick="window.open('/login.html', 'login', '%(features)s')">Sign in</button>
<a id="newtab" target="_blank" href="/plain.html">new tab</a>
<div id="result">none</div>
<script>window.addEventListener('message', e => { document.getElementById('result').textContent = e.data; });</script>""",
    # Posts the user name to its opener, then closes itself.
    "/login.html": """<title>Login</title>
<form id="form" onsubmit="window.opener.postMessage(document.getElementById('user').value, '*'); window.close(); return false;">
<input id="user"><button id="submit" type="submit">Go</button></form>""",
    # A popup that opens a popup of its own.
    "/chain.html": """<title>Chain</title>
<button id="next" onclick="window.open('/plain.html')">next</button>
<button id="blind" onclick="window.open('/no-content')">blind</button>
<button id="hop" onclick="window.open('/plain.html'); window.close()">hop</button>""",
    "/plain.html": "<title>Plain</title>plain",
}
PAGES["/opener.html"] = """<title>Opener</title>
<button id="chain" onclick="window.open('/chain.html')">chain</button>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/no-content":
            # A first response without a document: Chrome opens a tab that never commits a page.
            self.send_response(204)
            self.end_headers()
            return
        template = PAGES.get(path)
        if template is None:
            self.send_error(404)
            return
        body = (template % {"features": LOGIN_FEATURES} if "%(features)s" in template else template).encode()
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


def open_pages(runtime) -> list:
    return [page for page in runtime.browser_or_context.pages if not page.is_closed()]


def wait_until(runtime, condition, what: str) -> None:
    # Patchright delivers events only while its loop runs, so wait on that loop.
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}; open: {[p.url for p in open_pages(runtime)]}"
        runtime._run(asyncio.sleep(0.05))


def thread_names(runtime) -> list[str]:
    return [row["thread"] for row in json.loads(runtime.call("list", {}))["pages"]]


def announcement(output: str) -> str:
    return output.rstrip("\n").splitlines()[-1]


def open_chain_popup(runtime, site: str) -> None:
    """Thread `app` with its first popup `app/1`, which holds the `chain.html` buttons."""
    runtime.call("open", {"thread": "app", "url": f"{site}/opener.html"})
    runtime.call("click", {"thread": "app", "uid": "#chain"})


def open_app(runtime, site: str) -> None:
    runtime.call("open", {"thread": "app", "url": f"{site}/app.html"})


def test_sign_in_popup_is_a_thread_that_reports_back_to_its_opener(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "sign-in popup flow"):
        open_app(runtime, site)

        output = runtime.call("click", {"thread": "app", "uid": "#signin"})
        prefix = 'opened popup Thread("app/1"): '
        assert announcement(output).startswith(prefix), f"click output lacks the announcement:\n{output}"
        assert "login.html" in announcement(output)
        assert thread_names(runtime) == ["app", "app/1"]

        assert runtime.call("fill", {"thread": "app/1", "uid": "#user", "text": "ada"}) == "filled\n"
        runtime.call("click", {"thread": "app/1", "uid": "#submit"})

        wait_until(runtime, lambda: len(open_pages(runtime)) == 1, "the popup to close itself")
        # postMessage delivers on the opener's next task, shortly after the popup went.
        runtime.call("wait-for", {"thread": "app", "text": "ada"})
        # The opener's own page was never swapped for the popup.
        assert runtime.call("text", {"thread": "app", "target": "#result"}) == "ada\n"
        assert json.loads(runtime.call("eval", {"thread": "app", "code": "window.name"})) == ""

        for command, args in [("text", {}), ("click", {"uid": "#submit"}), ("open", {"url": f"{site}/login.html"})]:
            with pytest.raises(BridgeCodedError) as failure:
                runtime.call(command, {"thread": "app/1", **args})
            assert failure.value.code == ErrorCode.PAGE_CLOSED, f"{command}: {failure.value}"
            assert "app" in str(failure.value)
        assert len(open_pages(runtime)) == 1, "a command on the closed popup thread reopened a window"
        assert thread_names(runtime) == ["app"]


def test_target_blank_link_opens_a_popup_thread(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "target=_blank popup"):
        open_app(runtime, site)
        output = runtime.call("click", {"thread": "app", "uid": "#newtab"})
        assert announcement(output).startswith('opened popup Thread("app/1"): '), output
        assert "plain.html" in announcement(output)
        assert runtime.call("text", {"thread": "app/1"}).strip() == "plain"


def test_chained_popups_are_flat_and_close_with_their_opener(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "chained popups"):
        runtime.call("open", {"thread": "app", "url": f"{site}/opener.html"})
        first = runtime.call("click", {"thread": "app", "uid": "#chain"})
        assert announcement(first).startswith('opened popup Thread("app/1"): '), first

        second = runtime.call("click", {"thread": "app/1", "uid": "#next"})
        assert announcement(second).startswith('opened popup Thread("app/2"): '), second
        assert thread_names(runtime) == ["app", "app/1", "app/2"]

        runtime.call("close", {"thread": "app/1"})
        assert thread_names(runtime) == ["app"], "closing app/1 must close app/2 and keep app"
        assert len(open_pages(runtime)) == 1
        for stale in ("app/1", "app/2"):
            with pytest.raises(BridgeCodedError) as failure:
                runtime.call("text", {"thread": stale})
            assert failure.value.code == ErrorCode.PAGE_CLOSED

        # Numbers are never reused, so a stale handle cannot reach a newer popup.
        third = runtime.call("click", {"thread": "app", "uid": "#chain"})
        assert announcement(third).startswith('opened popup Thread("app/3"): '), third

        runtime.call("close", {"thread": "app"})
        assert open_pages(runtime) == []
        assert runtime.finish_response("close") is True


def test_eval_that_opens_a_popup_stays_json_and_the_next_action_announces_it(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "announcement after eval"):
        open_app(runtime, site)
        output = runtime.call("eval", {"thread": "app", "code": "void window.open('/plain.html')"})
        assert json.loads(output) is None
        wait_until(runtime, lambda: len(open_pages(runtime)) == 2, "Patchright to report the popup")

        assert "opened popup" not in runtime.call("list", {})
        # Not focus: Thread.focus() drops its output, so focus never carries announcements.
        assert "opened popup" not in runtime.call("focus", {"thread": "app"})
        announced = runtime.call("scroll", {"thread": "app", "direction": "down"})
        assert announcement(announced).startswith('opened popup Thread("app/1"): '), announced
        assert "opened popup" not in runtime.call("scroll", {"thread": "app", "direction": "up"}), "announced twice"


def test_popup_that_opens_a_popup_and_closes_itself_hands_the_new_page_to_the_root(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "popup that hops and closes"):
        open_chain_popup(runtime, site)

        # The same task opens app/2 and closes app/1, so Chrome's openers are all that link them.
        output = runtime.call("click", {"thread": "app/1", "uid": "#hop"})
        assert announcement(output).startswith('opened popup Thread("app/2"): '), f"no announcement:\n{output}"
        assert "plain.html" in announcement(output)
        wait_until(runtime, lambda: thread_names(runtime) == ["app", "app/2"], "app/1 to leave and app/2 to be listed")
        assert runtime.call("text", {"thread": "app/2"}).strip() == "plain"


def test_announcement_never_names_a_popup_that_closed_before_the_next_action_output(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "announcement of a closed popup"):
        open_app(runtime, site)
        runtime.call("eval", {"thread": "app", "code": "void window.open('/plain.html')"})
        wait_until(runtime, lambda: "app/1" in thread_names(runtime), "the popup to be reported as app/1")

        runtime.call("close", {"thread": "app/1"})
        output = runtime.call("scroll", {"thread": "app", "direction": "down"})
        assert "opened popup" not in output, f"announced a popup that is closed:\n{output}"


def test_closing_a_popup_thread_closes_the_tabs_its_page_opened_that_never_committed(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "close with an uncommitted tab"):
        open_chain_popup(runtime, site)
        session = runtime._run(runtime.browser_or_context.browser.new_browser_cdp_session())

        def tab_urls() -> list[str]:
            return [tab["url"] for tab in runtime._run(list_tabs(session))]

        # The 204 popup is a blank tab Patchright never reports, so only Chrome lists it.
        runtime.call("eval", {"thread": "app/1", "code": "document.getElementById('blind').click()"})
        wait_until(runtime, lambda: len(tab_urls()) == 3, "Chrome to open the tab that never commits")

        runtime.call("close", {"thread": "app/1"})
        left = tab_urls()
        assert len(left) == 1 and "opener.html" in left[0], f"tabs the closed popup thread opened are still open: {left}"


def test_call_in_flight_when_its_popup_closes_itself_names_the_open_threads(launch, site):  # noqa: F811
    runtime = launch()
    with fails_if_still_running_after(HUNG_AFTER_S, "in-flight call on a closing popup"):
        open_chain_popup(runtime, site)
        runtime.call("eval", {"thread": "app/1", "code": "void setTimeout(() => window.close(), 300)"})

        with pytest.raises(BridgeCodedError) as failure:
            runtime.call("wait-for", {"thread": "app/1", "text": "never appears", "timeoutMs": 30000})
        assert failure.value.code == ErrorCode.PAGE_CLOSED, str(failure.value)
        assert "open threads of app: app" in str(failure.value), str(failure.value)
