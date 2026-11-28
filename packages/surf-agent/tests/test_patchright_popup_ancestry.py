"""Closing a thread closes the tabs its page opened, even once Chrome no longer links them to it (issue #66).

When a tab's opener closes, Chrome hands the tab to the opener's own opener (``openerId``),
or drops the link once none is left. Patchright reports a popup only once it has set the
popup up, never while its opener is closing or closed. The fake models both: Chrome lists
its popups, and Patchright never reports them.
"""
from __future__ import annotations

import asyncio
import itertools
import types
from pathlib import Path
from typing import Any, Callable

import pytest

from runtime_driver import SyncRuntime
from surf_agent.backends.patchright.bridge import PatchrightRuntime
from surf_agent.backends.patchright.tabs import PopupTracker

MISSING_TARGET = "No target with given id found"


class FakeChrome:
    """Chrome's targets plus the CDP surface Surf may use; any other method fails the test."""

    def __init__(self) -> None:
        self.targets: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, FakePage] = {}  # Patchright's context.pages, by targetId
        self.sessions: list[FakeSession] = []
        self._ids = itertools.count(1)

    def create_tab(self, url: str, opener_id: str | None = None) -> str:
        target_id = f"tab-{next(self._ids)}"
        info = {"targetId": target_id, "type": "page", "url": url}
        if opener_id is not None:
            info["openerId"] = opener_id
        self.targets[target_id] = info
        self.pages[target_id] = FakePage(self, target_id, url)
        self._emit("Target.targetCreated", {"targetInfo": dict(info)})
        return target_id

    def open_popup(self, opener_id: str, url: str) -> str:
        """A popup Chrome lists from the start, which Patchright never reports."""
        return self.create_tab(url, opener_id=opener_id)

    def close_tab(self, target_id: str) -> None:
        if target_id not in self.targets:
            raise Exception(MISSING_TARGET)
        closed = self.targets.pop(target_id)
        self.pages.pop(target_id).closed = True
        for info in self.targets.values():
            if info.get("openerId") == target_id:
                if "openerId" in closed:
                    info["openerId"] = closed["openerId"]
                else:
                    del info["openerId"]
                self._emit("Target.targetInfoChanged", {"targetInfo": dict(info)})
        self._emit("Target.targetDestroyed", {"targetId": target_id})

    def open_page_urls(self) -> list[str]:
        return sorted(info["url"] for info in self.targets.values())

    def _emit(self, event: str, params: dict[str, Any]) -> None:
        for session in self.sessions:
            if session.discovering:
                for handler in session.handlers.get(event, []):
                    handler(params)


class FakePage:
    def __init__(self, chrome: FakeChrome, target_id: str, url: str) -> None:
        self.chrome, self.target_id, self.url = chrome, target_id, url
        self.closed = False
        self.before_close: Callable[[], None] = lambda: None

    def is_closed(self) -> bool:
        return self.closed

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        pass  # Patchright never reports these popups, so no event fires

    async def close(self) -> None:
        if not self.closed:
            self.before_close()
        if not self.closed:
            self.chrome.close_tab(self.target_id)


class FakeSession:
    def __init__(self, chrome: FakeChrome, page: FakePage | None) -> None:
        self.chrome, self.page = chrome, page
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.discovering = False
        self.attached = True
        chrome.sessions.append(self)

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def detach(self) -> None:
        self.attached = False
        self.discovering = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        assert self.attached, f"{method} sent on a detached session"
        chrome = self.chrome
        if method == "Target.getTargets":
            return {"targetInfos": [dict(info) for info in chrome.targets.values()]}
        if method == "Target.setDiscoverTargets":
            self.discovering = params["discover"]
            if self.discovering:
                for info in list(chrome.targets.values()):
                    for handler in self.handlers.get("Target.targetCreated", []):
                        handler({"targetInfo": dict(info)})
            return {}
        if method == "Target.createTarget":
            return {"targetId": chrome.create_tab(params["url"])}
        if method == "Target.closeTarget":
            chrome.close_tab(params["targetId"])
            return {"success": True}
        if method == "Target.getTargetInfo" and self.page is not None:
            return {"targetInfo": dict(chrome.targets[self.page.target_id])}
        raise AssertionError(f"fake Chrome does not support {method} {params}")


class FakeContext:
    def __init__(self, chrome: FakeChrome) -> None:
        self.chrome = chrome
        self.browser = types.SimpleNamespace(new_browser_cdp_session=lambda: FakeSession(chrome, None))

    @property
    def pages(self) -> list[FakePage]:
        # Patchright lists the pages it has set up, so only tabs it knows are here.
        return list(self.chrome.pages.values())

    def new_cdp_session(self, page: FakePage) -> FakeSession:
        return FakeSession(self.chrome, page)

    def new_page(self) -> None:
        raise AssertionError("a new page would open a foreground window")


def open_threads(*threads: str) -> tuple[SyncRuntime, FakeChrome, list[str]]:
    """A runtime on a fake Chrome with the named threads open; returns their page targetIds."""
    chrome = FakeChrome()
    runtime = SyncRuntime(PatchrightRuntime(profile_dir=Path("/tmp/surf-patchright-test")))
    runtime.browser_or_context = FakeContext(chrome)
    page_ids = []
    for thread in threads:
        runtime.call("new", {"thread": thread})
        page_ids.append(next(target_id for target_id in chrome.targets if target_id not in page_ids))
    return runtime, chrome, page_ids


def popup_opened_while_closing(runtime, chrome, page_a):
    chrome.pages[page_a].before_close = lambda: chrome.open_popup(page_a, "popup-of-a")


def page_closed_before_surf_closes_it(runtime, chrome, page_a):
    popup = chrome.open_popup(page_a, "popup-of-a")
    chrome.open_popup(popup, "popup-of-popup")
    chrome.close_tab(page_a)  # as a human, or window.close(), would


SCENARIOS = {
    "popup opens while the thread closes": popup_opened_while_closing,
    "thread page closed beforehand": page_closed_before_surf_closes_it,
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_close_closes_popups_chrome_no_longer_links_to_the_thread(scenario):
    runtime, chrome, (page_a, page_b) = open_threads("a", "b")
    # Thread "b" has a popup of its own, which closing "a" must leave alone.
    popup_of_b = chrome.open_popup(page_b, "popup-of-b")
    SCENARIOS[scenario](runtime, chrome, page_a)

    runtime.call("close", {"thread": "a"})

    assert set(chrome.targets) == {page_b, popup_of_b}, f"left open: {chrome.open_page_urls()}"

    runtime.call("close", {"thread": "b"})
    assert chrome.targets == {}
    assert runtime.finish_response("close") is True


def test_closing_a_popup_closes_its_child_chrome_re_pointed_to_the_root_meanwhile():
    chrome = FakeChrome()
    root = chrome.create_tab("root")
    popup = chrome.open_popup(root, "popup")
    child = chrome.open_popup(popup, "child")
    tracker = PopupTracker(lambda: FakeSession(chrome, None))

    async def scenario() -> None:
        await tracker.watch()  # replays the tabs with their openers: child -> popup -> root
        # The fake re-points the child to the root and announces it, as Chrome does when its opener closes.
        chrome.close_tab(popup)
        await tracker.close_popups_of(popup)

    asyncio.run(scenario())

    assert child not in chrome.targets, f"left open: {chrome.open_page_urls()}"
    assert root in chrome.targets
