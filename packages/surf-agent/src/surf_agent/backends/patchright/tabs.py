"""Chrome's tabs as CDP lists them, and which tab opened which.

Tabs are read from CDP rather than context.pages: Patchright never lists tabs Chrome
restored from the last session, nor popups it has not finished setting up.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from collections.abc import Callable
from typing import Any

from ..bridge_common import CLOSED_TARGET_MESSAGE

# CDP's answer to Target.closeTarget for a target that no longer exists.
MISSING_TARGET_MESSAGE = "No target with given id found"
# A closed popup is gone once its unload handlers ran; past this, it is left to finish.
POPUP_CLOSE_TIMEOUT_S = 5.0
POPUP_CLOSE_POLL_INTERVAL_S = 0.05
# Openers are always tabs, and other targets would only add events.
PAGE_TARGETS = [{"type": "page"}]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def list_tabs(session: Any) -> list[dict[str, Any]]:
    # Chrome's own UI is type "browser_ui", and a subtype marks a page that is not a
    # tab, such as a prerender.
    response = await _maybe_await(session.send("Target.getTargets"))
    return [target for target in response["targetInfos"] if target["type"] == "page" and not target.get("subtype")]


async def close_tab(session: Any, target_id: str) -> None:
    try:
        await _maybe_await(session.send("Target.closeTarget", {"targetId": target_id}))
    except Exception as exc:
        # The tab can close on its own between listing and closing.
        if MISSING_TARGET_MESSAGE not in str(exc):
            raise


class PopupTracker:
    """Which tab opened which, recorded as Chrome announces each tab, for one browser.

    A thread owns the popups and tabs its page opens, and the ones those open in turn,
    so they close with the thread; left open, one would also keep the bridge from
    stopping. Neither Chrome nor Patchright can say which those are once it matters:
    when a tab's opener closes, Chrome hands the tab to the opener's own opener, and
    drops the link once none is left, so the thread's page closing cuts its tabs loose;
    Patchright reports a popup only once it has set the page up, never if the opener is
    closing. So the links are recorded from the start and kept after an opener closes.
    """

    def __init__(self, open_browser_session: Callable[[], Any]) -> None:
        self._open_browser_session = open_browser_session
        self._session: Any | None = None
        self._starting = asyncio.Lock()
        self._opener_of: dict[str, str] = {}

    async def watch(self) -> Any:
        """Start recording, once, and return the browser session it records on."""
        async with self._starting:
            if self._session is None:
                session = await _maybe_await(self._open_browser_session())
                session.on("Target.targetCreated", self._remember_opener)
                session.on("Target.targetInfoChanged", self._remember_opener)
                # Chrome announces the tabs already open as well, while they still name their openers.
                await _maybe_await(session.send("Target.setDiscoverTargets", {"discover": True, "filter": PAGE_TARGETS}))
                self._session = session
        return self._session

    def _remember_opener(self, event: dict[str, Any]) -> None:
        info = event["targetInfo"]
        # A tab names its opener from the moment it exists. Later updates may name an
        # ancestor instead, once the opener closed, or none: keep the last one named.
        if opener := info.get("openerId"):
            self._opener_of[info["targetId"]] = opener

    def _opened_by(self, opener_target_id: str) -> set[str]:
        opened: set[str] = set()
        openers = {opener_target_id}
        while openers:
            openers = {tab for tab, opener in self._opener_of.items() if opener in openers} - opened
            opened |= openers
        return opened

    def openers_of(self, target_id: str) -> list[str]:
        """The tabs that opened target_id, nearest first, as recorded: closed ones included."""
        openers: list[str] = []
        opener = self._opener_of.get(target_id)
        while opener is not None:
            openers.append(opener)
            opener = self._opener_of.get(opener)
        return openers

    def recorded_popups_of(self, opener_target_id: str) -> set[str]:
        """The tabs opener_target_id opened, directly or through others, as recorded so far, closed ones included."""
        return self._opened_by(opener_target_id)

    async def open_popups_of(self, opener_target_id: str) -> set[str]:
        """The tabs opener_target_id opened, directly or through others, that Chrome lists now."""
        session = await self.watch()
        listed = {tab["targetId"] for tab in await list_tabs(session)}
        # Only after the list: Chrome announces a tab before it answers a later list.
        return listed & self._opened_by(opener_target_id)

    async def close_popups_of(self, opener_target_id: str) -> None:
        """Close the tabs a closing or closed page opened, directly or through other tabs.

        Returns once Chrome lists none of them, nor the page itself: Chrome sends
        Patchright a tab's detach before it answers a list that leaves the tab out, so
        from then on the bridge no longer counts them as open pages.
        """
        try:
            session = await self.watch()
            await self._close_and_wait(session, opener_target_id)
        except Exception as exc:
            if CLOSED_TARGET_MESSAGE not in str(exc):
                raise
            # Chrome is gone, and every tab with it.

    async def _close_and_wait(self, session: Any, opener_target_id: str) -> None:
        deadline = time.monotonic() + POPUP_CLOSE_TIMEOUT_S
        closing: set[str] = set()
        while True:
            listed = {tab["targetId"] for tab in await list_tabs(session)}
            # Read the links only after the list: Chrome announces a tab before it
            # answers a later list, so the tabs opened by any page this list leaves out
            # are all recorded by now, even one opened while the page was closing.
            popups = self._opened_by(opener_target_id)
            still_open = listed & (popups | {opener_target_id})
            if not still_open:
                return
            if time.monotonic() >= deadline:
                # The thread is closed either way; until these go, the bridge stays up.
                print(
                    f"surf-agent: warning: {len(still_open)} tab(s) of a closed thread still closing after "
                    f"{POPUP_CLOSE_TIMEOUT_S:g}s",
                    file=sys.stderr,
                )
                return
            for target_id in (still_open & popups) - closing:
                await close_tab(session, target_id)
                closing.add(target_id)
            await asyncio.sleep(POPUP_CLOSE_POLL_INTERVAL_S)
