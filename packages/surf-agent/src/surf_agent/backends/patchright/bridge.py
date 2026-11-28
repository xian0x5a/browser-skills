from __future__ import annotations

import argparse
import asyncio
import fnmatch
import inspect
import json
import math
import os
import sys
import time
from pathlib import Path

from patchright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from platformdirs import PlatformDirs
from typing import Any, NoReturn
from ...constants import DEFAULT_PATCHRIGHT_APP_ID, DEFAULT_WAIT_TIMEOUT_MS, PATCHRIGHT_BACKEND, POPUP_THREAD_SEPARATOR
from ...errors import ErrorCode
from ...snapshots import input_value_lines, mask_input_values
from .constants import CONTEXT_RESTART_REQUIRED
from .launch_args import STARTUP_PAGE_ARG, capture_default_args, keep_disposable_data_out_of_profile
from .last_session import forget_last_session
from .network_capture import NetworkCapture
from .saved_zoom import clear_saved_zoom
from .server import create_bridge_server
from .tabs import PopupTracker, close_tab, list_tabs
from ..bridge_common import (
    CLOSED_TARGET_MESSAGE,
    NATIVE_ARIA_REF_PATTERN,
    SNAPSHOT_BOXES,
    SNAPSHOT_DEPTH,
    STALE_REF_MESSAGE,
    BridgeCodedError,
    PageSlot,
    bridge_health_payload,
)

SNAPSHOT_ARIA_TIMEOUT_MS = 3_000
SNAPSHOT_BODY_TIMEOUT_MS = 3_000
# Must stay below the client's transport timeout (15s default) so a blocked
# action returns a classified error instead of an unknown-outcome timeout.
ACTION_TIMEOUT_MS = 5_000
# Short: probes run after the action already waited ACTION_TIMEOUT_MS.
ACTIONABILITY_PROBE_TIMEOUT_MS = 250
# A page hides a field's text with a password input or with this CSS property.
SECRET_FIELD_TEST = """(field) => field.type === "password" ||
  !["", "none"].includes(getComputedStyle(field).getPropertyValue("-webkit-text-security"))"""
FRAME_HAS_SECRET_FIELD_SCRIPT = f"""() => {{
  const isSecret = {SECRET_FIELD_TEST};
  const visit = (root) => [...root.querySelectorAll("*")].some((element) =>
    (element.shadowRoot && visit(element.shadowRoot)) ||
    (element.matches("input, textarea") && isSecret(element)));
  return visit(document);
}}"""
# A secret can sit in the markup: prefilled, or synced to a value attribute by a framework.
# Copying into an inert document keeps a read from running custom element constructors.
HTML_WITHOUT_SECRET_VALUES_SCRIPT = f"""() => {{
  if (!document.documentElement) return "";
  const isSecret = {SECRET_FIELD_TEST};
  const root = document.implementation.createHTMLDocument("").importNode(document.documentElement, true);
  const copies = root.querySelectorAll("input, textarea");
  document.querySelectorAll("input, textarea").forEach((field, index) => {{
    if (!isSecret(field)) return;
    copies[index].removeAttribute("value");
    copies[index].textContent = "";
  }});
  return root.outerHTML;
}}"""
WAIT_POLL_INTERVAL_S = 0.1
# Returns a description of the element receiving pointer events at the target's
# center, or null when the target (or its shadow content) receives them itself.
HIT_TEST_BLOCKER_JS = """element => {
  const box = element.getBoundingClientRect();
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return null;
  const hit = document.elementFromPoint(x, y);
  if (!hit || element.contains(hit) || hit.shadowRoot?.contains(element)) return null;
  const id = hit.id ? `#${hit.id}` : '';
  const classes = [...hit.classList].map(name => `.${name}`).join('');
  const text = (hit.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
  return `<${hit.tagName.toLowerCase()}${id}${classes}>` + (text ? ` "${text}"` : '');
}"""
CDP_NEW_WINDOW_TIMEOUT_S = 3.0
CDP_NEW_WINDOW_POLL_INTERVAL_S = 0.05
# Patchright reports a popup once its first navigation commits, which a slow server
# delays; past this, the next action's output announces it instead.
POPUP_REPORT_TIMEOUT_S = 3.0
POPUP_REPORT_POLL_INTERVAL_S = 0.05
# Linux v11 cookies require Chrome’s real OS password store/keychain, not Patchright automation defaults.
PATCHRIGHT_INCOMPATIBLE_DEFAULT_ARGS = ("--password-store=basic", "--use-mock-keychain")
# Chrome starts without a window: on macOS showing one activates Chrome and steals
# focus. Thread windows are created later, in the background.
NO_STARTUP_WINDOW_ARG = "--no-startup-window"
BLANK_PAGE_URL = "about:blank"
# Read-only snapshots and commands that lock the threads they touch themselves.
COMMANDS_WITHOUT_THREAD_LOCK = frozenset({"list", "state", "close-matching", "rename-thread"})
# Commands whose output reports an action, and so also the popups it opened. JSON
# output and page text never carry them: they would no longer parse, or read as page.
# Nor does focus: Thread.focus() drops its output, and focusing opens no popup.
ACTION_COMMANDS = frozenset(
    {"new", "open", "back", "click", "click-at", "fill", "upload", "type", "press", "scroll", "sleep", "wait-for"}
)
# Chrome's Page.resetNavigationHistory error while a back/forward navigation is pending.
HISTORY_NAVIGATION_PENDING_MESSAGE = "History cannot be pruned"
# The windowless launch relies on Patchright skipping its first-page wait; if a
# Patchright release stops skipping it, fail after this instead of hanging.
LAUNCH_TIMEOUT_MS = 30_000
# Chrome reports 90% zoom as 0.8999999761581421, so compare with a tolerance.
ZOOM_TOLERANCE = 1e-3


class PatchrightRuntime:
    def __init__(
        self,
        *,
        profile_dir: Path,
        disk_cache_dir: Path | None = None,
        headless: bool = False,
        app_id: str = DEFAULT_PATCHRIGHT_APP_ID,
    ) -> None:
        self.profile_dir = profile_dir
        self.disk_cache_dir = disk_cache_dir
        self.headless = headless
        self.app_id = app_id
        self.manager: Any | None = None
        self.browser_or_context: Any | None = None
        self.pages: dict[str, PageSlot] = {}
        self.network = NetworkCapture()
        # Never reset: a runtime launches at most one browser (see _stopping below).
        self._popups = PopupTracker(lambda: self._context().browser.new_browser_cdp_session())
        self._next_page_id = 1
        # Per thread: the last popup thread number given, never reused so a stale handle
        # can't reach a newer popup, and the popup threads no action output announced yet.
        self._popup_numbers: dict[str, int] = {}
        self._unannounced_popups: dict[str, list[str]] = {}
        # One new page at a time, in the order Patchright reports them, so popup thread
        # numbers follow the order the popups opened.
        self._adopting = asyncio.Lock()
        self.shutdown_requested = False
        self._close_shutdown_pending = False
        # Calls from different Surf threads interleave at every await on one event loop.
        # A thread's lock keeps its own calls in order; the browser lock covers launch and
        # window creation, so two creations never both find Chrome windowless and each
        # close the other's new window as a restored tab.
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._browser_lock = asyncio.Lock()
        self._calls_in_flight = 0
        # One-way: once stop or restart begins, this process never relaunches the browser;
        # the client relaunches through its cross-process launch guard (cookie preflight).
        self._stopping = False
        self._stop_task: asyncio.Future[str] | None = None

    def health_payload(self) -> dict[str, object]:
        return bridge_health_payload(PATCHRIGHT_BACKEND, self.profile_dir)

    async def stop_async(self) -> str:
        """Stop the browser once, however many callers ask (restart, stop, idle, exit)."""
        self._stopping = True
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._stop_async())
        # Shielded: a cancelled caller must not cancel the shared stop.
        return await asyncio.shield(self._stop_task)

    async def call_async(self, name: str, args: dict[str, Any]) -> str:
        if name == "stop":
            return await self.stop_async()
        self._raise_if_stopping()
        self._calls_in_flight += 1
        try:
            if name in COMMANDS_WITHOUT_THREAD_LOCK:
                return await self._call_async(name, args)
            thread = str(args.get("thread") or "default")
            async with self._thread_lock(thread):
                if name in ACTION_COMMANDS:
                    return await self._act_and_announce_popups(name, args, thread)
                return await self._call_async(name, args)
        except BridgeCodedError:
            raise
        except Exception as exc:
            if self._is_closed_target_error(exc):
                thread = str(args.get("thread") or "default")
                if self._is_popup_thread(thread):
                    # A popup can close itself mid-call, as a sign-in window does.
                    raise self._closed_popup_thread_error(thread) from exc
                raise BridgeCodedError(ErrorCode.PAGE_CLOSED, str(exc)) from exc
            raise
        finally:
            self._calls_in_flight -= 1

    async def _act_and_announce_popups(self, name: str, args: dict[str, Any], thread: str) -> str:
        root = self._root_of(thread)
        root_target_id = self._target_id_of(root)
        recorded = self._popups.recorded_popups_of(root_target_id) if root_target_id else set()
        output = await self._call_async(name, args)
        await self._wait_for_popups_adopted_since(root, recorded)
        return output + self._announce_popups(root)

    async def _wait_for_popups_adopted_since(self, root: str, recorded: set[str]) -> None:
        # Patchright's click does not always wait for the popup it opened, and reports a
        # popup only once its first navigation commits. Wait for every popup Chrome
        # opened during the action, so the action's output names it and popups() lists it.
        root_target_id = self._target_id_of(root)
        # Chrome creates a popup, and announces it, before it acknowledges the input that
        # opened it, so an action that opened none needs no round trip to Chrome.
        if root_target_id is None or not self._popups.recorded_popups_of(root_target_id) - recorded:
            return
        deadline = time.monotonic() + POPUP_REPORT_TIMEOUT_S
        while time.monotonic() < deadline:
            opened = await self._popups.open_popups_of(root_target_id) - recorded
            if opened <= {slot.target_id for slot in self.pages.values()}:
                return
            await asyncio.sleep(POPUP_REPORT_POLL_INTERVAL_S)
        # Past this, a later action's output announces it.

    def _announce_popups(self, root: str) -> str:
        # Only the ones still open, with the URL they have now: a popup can close, or its
        # thread be closed, before an action output announces it.
        names = [name for name in self._unannounced_popups.pop(root, []) if name in self.pages]
        return "".join(f'opened popup Thread("{name}"): {self._page_url(self.pages[name].page)}\n' for name in names)

    def _target_id_of(self, thread: str) -> str | None:
        slot = self.pages.get(thread)
        return slot.target_id if slot is not None else None

    def _thread_lock(self, thread: str) -> asyncio.Lock:
        return self._thread_locks.setdefault(thread, asyncio.Lock())

    def _raise_if_stopping(self) -> None:
        if self._stopping:
            raise RuntimeError(CONTEXT_RESTART_REQUIRED)

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _start_async(self) -> None:
        if self.browser_or_context is not None:
            return
        async with self._browser_lock:
            # A call that passed the entry check may arrive here after stop began.
            self._raise_if_stopping()
            if self.browser_or_context is None:
                await self._launch_async()

    async def _launch_async(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        clear_saved_zoom(self.profile_dir)
        forget_last_session(self.profile_dir)
        # One identity for both flags: window rules match it as the Wayland app-id or X11 WM_CLASS.
        launch_args = [f"--class={self.app_id}", f"--name={self.app_id}"]
        self.manager = async_playwright()
        if hasattr(self.manager, "__aenter__"):
            playwright = await self.manager.__aenter__()
        else:
            playwright = self.manager.__enter__()
        self._cancel_shutdown_request()
        launch_options = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "no_viewport": True,
            # Patchright otherwise emulates light mode instead of honoring the desktop theme.
            "color_scheme": "null",
            "chromium_sandbox": True,
        }
        # --no-startup-window (see NO_STARTUP_WINDOW_ARG) hangs Patchright: it opens a startup
        # page and waits for it, skipping the wait only when it adds no args of its own. So
        # pass its own flags back without that page.
        # Remove this workaround (capture_default_args, ignore_default_args) once Patchright
        # launches with --no-startup-window without hanging; requested upstream at
        # https://redirect.github.com/Kaliiiiiiiiii-Vinyzu/patchright/discussions/232
        patchright_args = await capture_default_args(playwright.chromium, **launch_options)
        chrome_args = [
            arg for arg in patchright_args
            if arg != STARTUP_PAGE_ARG and arg not in PATCHRIGHT_INCOMPATIBLE_DEFAULT_ARGS
        ]
        self.browser_or_context = await self._maybe_await(
            playwright.chromium.launch_persistent_context(
                **launch_options,
                # Chrome channel keeps existing Chrome profile behavior; bundled browsers break that reuse.
                channel="chrome",
                ignore_default_args=True,
                timeout=LAUNCH_TIMEOUT_MS,
                args=keep_disposable_data_out_of_profile(
                    [*chrome_args, *launch_args, NO_STARTUP_WINDOW_ARG], disk_cache_dir=self.disk_cache_dir
                ),
            )
        )
        self.network.attach(self.browser_or_context)
        self.browser_or_context.on("page", self._adopt_if_popup)

    async def _stop_async(self) -> str:
        if self.manager is not None:
            if hasattr(self.manager, "__aexit__"):
                await self._maybe_await(self.manager.__aexit__(None, None, None))
            else:
                self.manager.__exit__(None, None, None)
        self.manager = None
        self.browser_or_context = None
        self.pages.clear()
        self.network = NetworkCapture()
        self._cancel_shutdown_request()
        return "stopped\n"

    async def _call_async(self, name: str, args: dict[str, Any]) -> str:
        thread = str(args.get("thread") or "default")
        if name == "state":
            slot = self.pages.get(thread)
            if not slot:
                return json.dumps({"backend": "patchright", "open": False, "thread": thread}) + "\n"
            return json.dumps({"backend": "patchright", "open": True, "thread": thread, **(await self._metadata(slot))}) + "\n"
        if name == "list":
            rows = [{"thread": key, **(await self._metadata(slot))} for key, slot in sorted(self.pages.items())]
            return json.dumps({"backend": "patchright", "pages": rows}, sort_keys=True) + "\n"
        if name == "rename-thread":
            destination_thread = args.get("destination_thread")
            if not isinstance(destination_thread, str) or not destination_thread:
                raise RuntimeError("rename-thread requires destination_thread")
            if self._is_popup_thread(thread) or self._is_popup_thread(destination_thread):
                raise RuntimeError("popup threads keep the names Surf gave them")
            if thread == destination_thread:
                if thread not in self.pages:
                    raise RuntimeError(f"thread not found: {thread}")
                return f"renamed {destination_thread}\n"
            # Lock both names (sorted, so two renames cannot deadlock): an unlocked rename
            # onto a thread mid-creation would be overwritten when that window registers.
            first, second = sorted((thread, destination_thread))
            async with self._thread_lock(first), self._thread_lock(second):
                if thread not in self.pages:
                    raise RuntimeError(f"thread not found: {thread}")
                if destination_thread in self.pages:
                    raise RuntimeError(f"thread already exists: {destination_thread}")
                if self._popup_threads_of(thread):
                    raise RuntimeError(f"thread {thread} has open popup threads; close them before renaming it")
                self.pages[destination_thread] = self.pages.pop(thread)
            return f"renamed {destination_thread}\n"
        if name == "close":
            old = self.pages.pop(thread, None)
            if old:
                await self._close_thread_pages(old)
            # The HTTP handler arms this only after its success response is written.
            self._close_shutdown_pending = True
            return "closed\n"
        if name == "close-matching":
            pattern = str(args.get("pattern") or "").strip()
            if not pattern:
                raise RuntimeError("close-matching requires a thread glob pattern")
            result: dict[str, Any] = {"pattern": pattern, "closed": [], "failed": []}
            for managed_thread in sorted(self.pages):
                if not fnmatch.fnmatchcase(managed_thread, pattern):
                    continue
                # Wait out the thread's in-flight call so its page is not closed mid-action.
                async with self._thread_lock(managed_thread):
                    slot = self.pages.get(managed_thread)
                    if slot is None:
                        continue
                    item = {"thread": managed_thread, "page_id": slot.page_id}
                    try:
                        await self._close_thread_pages(slot)
                    except Exception:
                        result["failed"].append(item)
                    else:
                        self.pages.pop(managed_thread, None)
                        result["closed"].append(item)
            # The HTTP handler arms this only after its success response is written.
            if not result["failed"]:
                self._close_shutdown_pending = True
            return json.dumps(result, sort_keys=True) + "\n"
        if name == "scroll" and str(args.get("direction") or "down") not in {"up", "down", "top", "bottom"}:
            raise RuntimeError("scroll requires direction: up, down, top, or bottom")
        if self._is_popup_thread(thread):
            # Before any launch: only a popup a thread's page opens makes a popup thread.
            if name == "new":
                raise RuntimeError(f"new cannot create {thread}: popup threads come only from popups a thread's page opens")
            slot = self.pages.get(thread)
            if slot is None or not self._page_is_open(slot.page):
                raise self._closed_popup_thread_error(thread)
        await self._start_async()
        if name == "new":
            url = str(args.get("url") or BLANK_PAGE_URL)
            slot = await self._new_page(thread, url=url)
            return self._format_opened(slot.page)
        if name == "open":
            url = str(args["url"])
            existing = self.pages.get(thread)
            if not existing or not self._page_is_open(existing.page):
                slot = await self._new_page(thread, url=url)
                return self._format_opened(slot.page)

            async def open_page(slot: PageSlot) -> str:
                self.network.clear(slot.page)
                await self._maybe_await(slot.page.goto(url, wait_until="domcontentloaded"))
                return self._format_opened(slot.page)

            try:
                return await open_page(existing)
            except Exception as exc:
                if not self._is_closed_target_error(exc):
                    raise
                slot = await self._new_page(thread, url=url)
                return self._format_opened(slot.page)
        slot = await self._page(thread)
        if name == "back":
            # BFCache restores do not emit DOMContentLoaded again. Wait for the
            # history commit, then inspect document readiness rather than waiting
            # for a lifecycle event that may already belong to the cached page.
            await self._maybe_await(slot.page.go_back(wait_until="commit"))
            await self._maybe_await(slot.page.wait_for_function(
                "document.readyState === 'complete' || "
                "(performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd ?? 0) > 0"
            ))
            return self._format_opened(slot.page)
        if name == "text":
            target = args.get("target")
            if target is None:
                return await self._body_text(slot.page)
            locator = await self._target_locator(slot, str(target))
            text = await self._maybe_await(locator.inner_text(timeout=ACTION_TIMEOUT_MS))
            return text + ("" if text.endswith("\n") else "\n")
        if name == "snapshot":
            return await self._snapshot(slot)
        if name == "click":
            target = str(args["uid"])
            locator = await self._target_locator(slot, target)
            await self._act(target, locator, lambda: locator.click(timeout=ACTION_TIMEOUT_MS), editable=False)
            return "clicked\n"
        if name == "click-at":
            x, y = float(args["x"]), float(args["y"])
            width, height = await self._maybe_await(slot.page.evaluate("() => [innerWidth, innerHeight]"))
            # A point off the viewport hits nothing, and the mouse API would not say so.
            if not (0 <= x < width and 0 <= y < height):
                raise BridgeCodedError(
                    ErrorCode.NOT_VISIBLE, f"point ({x:g}, {y:g}) is outside the {width}x{height} viewport"
                )
            await self._maybe_await(slot.page.mouse.click(x, y))
            return "clicked\n"
        if name == "fill":
            target = str(args["uid"])
            locator = await self._target_locator(slot, target)
            text = str(args.get("text") or "")
            await self._act(target, locator, lambda: self._fill_or_select(locator, text), editable=True)
            return "filled\n"
        if name == "upload":
            target = str(args["uid"])
            locator = await self._target_locator(slot, target)
            paths = [str(path) for path in args["paths"]]
            # set_input_files skips the visibility check, so hidden inputs behind styled dropzones work.
            await self._act(target, locator, lambda: locator.set_input_files(paths, timeout=ACTION_TIMEOUT_MS), editable=False)
            return "uploaded\n"
        if name == "type":
            await self._maybe_await(slot.page.keyboard.type(str(args.get("text") or "")))
            return "typed\n"
        if name == "press":
            await self._maybe_await(slot.page.keyboard.press(str(args.get("key") or "Enter")))
            return "pressed\n"
        if name == "scroll":
            direction = str(args.get("direction") or "down")
            if direction not in {"up", "down", "top", "bottom"}:
                raise RuntimeError("scroll requires direction: up, down, top, or bottom")
            delta = -700 if direction in {"up", "top"} else 700
            if direction == "top":
                await self._maybe_await(slot.page.evaluate("() => window.scrollTo(0, 0)"))
            elif direction == "bottom":
                await self._maybe_await(slot.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)"))
            else:
                await self._maybe_await(slot.page.mouse.wheel(0, delta))
            return "scrolled\n"
        if name == "sleep":
            await self._maybe_await(slot.page.wait_for_timeout(float(args["milliseconds"])))
            return "waited\n"
        if name == "wait-for":
            await self._wait_for(
                slot.page,
                text=args.get("text"),
                gone=args.get("gone"),
                url=args.get("url"),
                timeout_ms=int(args.get("timeoutMs") or DEFAULT_WAIT_TIMEOUT_MS),
            )
            return "waited\n"
        if name == "screenshot":
            path = str(args["path"])
            full_page = args.get("fullPage") is True
            await self._require_unzoomed(slot.page)
            # CSS scale keeps image pixels equal to click_at coordinates on scaled (HiDPI) displays.
            await self._maybe_await(slot.page.screenshot(path=path, full_page=full_page, scale="css"))
            return f"screenshot: {path}\n"
        if name == "eval":
            result = await self._maybe_await(slot.page.evaluate(str(args.get("code") or "")))
            return json.dumps(result, ensure_ascii=False) + "\n"
        if name == "focus":
            await self._maybe_await(slot.page.bring_to_front())
            return "focused\n"
        if name == "responses":
            return json.dumps({"responses": await self.network.responses(slot.page)}, ensure_ascii=False) + "\n"
        if name == "response-body":
            key = str(args.get("key") or "")
            try:
                entry = await self.network.response(slot.page, key)
            except KeyError:
                raise BridgeCodedError(
                    ErrorCode.NOT_FOUND, f"no captured response {key!r}; open() clears capture and old entries are evicted"
                ) from None
            detail = {**entry.summary(), "body": entry.body}
            detail.pop("shape", None)
            return json.dumps(detail, ensure_ascii=False) + "\n"
        raise RuntimeError(f"unsupported Patchright command: {name}")

    def after_response(self, name: str) -> None:
        if name not in {"close", "close-matching"} or not self._close_shutdown_pending:
            return
        self._close_shutdown_pending = False
        if self._is_busy():
            return
        self.shutdown_requested = True

    async def finish_response(self, name: str) -> bool:
        """Run after a call's response is sent; True once this bridge process should exit.

        Explicit stop, idle stop and closed-context restart all end the process. Returns only
        after the browser has stopped, so a client that relaunches never races the old Chrome.
        """
        self.after_response(name)
        if self.shutdown_requested and not self._stopping:
            if self._is_busy():
                self._cancel_shutdown_request()
            else:
                self._stopping = True
        if not self._stopping:
            return False
        try:
            await self.stop_async()
        except Exception as exc:
            # Exit anyway: calls already answered CONTEXT_RESTART_REQUIRED wait for this
            # process to go, and stop_bridge() kills whatever the failed stop left behind.
            print(f"surf-agent: warning: could not stop Patchright bridge cleanly: {exc}", file=sys.stderr)
        return True

    def _is_busy(self) -> bool:
        # An in-flight call may be creating a window that is not visible yet.
        return bool(self._calls_in_flight or self._visible_pages())

    def _visible_pages(self) -> list[Any]:
        if self.browser_or_context is None or not hasattr(self.browser_or_context, "pages"):
            return []
        return [page for page in list(self.browser_or_context.pages) if self._page_is_open(page)]

    def _cancel_shutdown_request(self) -> None:
        self.shutdown_requested = False
        self._close_shutdown_pending = False

    def _context(self) -> Any:
        if self.browser_or_context is None:
            raise RuntimeError("Patchright runtime is not started")
        # persistent_context=True returns BrowserContext. Non-persistent would return Browser.
        if hasattr(self.browser_or_context, "new_page") and hasattr(self.browser_or_context, "pages"):
            return self.browser_or_context
        return self.browser_or_context.new_context()

    async def _page(self, thread: str) -> PageSlot:
        slot = self.pages.get(thread)
        if slot and self._page_is_open(slot.page):
            return slot
        return await self._new_page(thread)

    async def _new_page(self, thread: str, url: str | None = None) -> PageSlot:
        if self._is_popup_thread(thread):
            # A new window would not be the popup: it would have lost window.opener.
            raise self._closed_popup_thread_error(thread)
        self._cancel_shutdown_request()
        old = self.pages.pop(thread, None)
        if old:
            await self._close_thread_pages(old)
        async with self._browser_lock:
            self._raise_if_stopping()
            try:
                # Before the window exists, so every tab it opens is recorded as its own.
                await self._popups.watch()
                page, target_id = await self._create_new_window_page()
            except Exception as exc:
                if not self._is_closed_target_error(exc):
                    raise
                # Manual window close can close the whole persistent context; the client
                # relaunches it in a fresh bridge process and retries the request.
                await self._restart_closed_context()
            slot = PageSlot(page=page, page_id=self._next_page_id, target_id=target_id)
            self._next_page_id += 1
            self.pages[thread] = slot
        if url and url != BLANK_PAGE_URL:
            # Navigate here rather than handing the URL to Target.createTarget: the target
            # appears before that navigation commits, so the page came back without its URL
            # or content, and under Patchright 1.60 never left about:blank (issue #38).
            await self._maybe_await(page.goto(url, wait_until="domcontentloaded"))
            await self._forget_blank_history_entry(page)
        return slot

    async def _adopt_if_popup(self, page: Any) -> None:
        """Give a popup to the thread whose page opened it, directly or through other tabs, as a popup thread (docs/adr/0012)."""
        async with self._adopting:
            # Kept attached: detaching a page session can hang on a busy renderer (issue #53).
            target_id = await self._page_target_id(page.context, page, keep_session=True)
            if target_id is None:
                return
            # Chrome's recorded openers, not Patchright's: Patchright names no opener, and
            # fires no popup event, once the opener closed, as when a popup opens another
            # and closes itself in one go.
            threads_by_target = {slot.target_id: name for name, slot in self.pages.items()}
            owner = next(
                (threads_by_target[opener] for opener in self._popups.openers_of(target_id) if opener in threads_by_target),
                None,
            )
            if owner is None:
                # A thread's own window, or a tab of a page no thread holds, which closes
                # with the thread that let it go.
                return
            root = self._root_of(owner)
            number = self._popup_numbers.get(root, 0) + 1
            self._popup_numbers[root] = number
            name = f"{root}{POPUP_THREAD_SEPARATOR}{number}"
            self.pages[name] = PageSlot(page=page, page_id=self._next_page_id, target_id=target_id)
            self._next_page_id += 1
            self._unannounced_popups.setdefault(root, []).append(name)
            page.on("close", lambda _page: self._forget_popup_thread(name, page))

    def _forget_popup_thread(self, name: str, page: Any) -> None:
        slot = self.pages.get(name)
        if slot is not None and slot.page is page:
            del self.pages[name]

    def _is_popup_thread(self, thread: str) -> bool:
        return POPUP_THREAD_SEPARATOR in thread

    def _root_of(self, thread: str) -> str:
        return thread.partition(POPUP_THREAD_SEPARATOR)[0]

    def _popup_threads_of(self, root: str) -> list[str]:
        """The popup threads of root, in opening order."""
        prefix = root + POPUP_THREAD_SEPARATOR
        return sorted((name for name in self.pages if name.startswith(prefix)), key=lambda name: int(name.removeprefix(prefix)))

    def _closed_popup_thread_error(self, thread: str) -> BridgeCodedError:
        root = self._root_of(thread)
        open_threads = [
            name for name in [root, *self._popup_threads_of(root)] if name in self.pages and self._page_is_open(self.pages[name].page)
        ]
        return BridgeCodedError(
            ErrorCode.PAGE_CLOSED,
            f"popup thread {thread} is not open, and popup threads never reopen; "
            f"open threads of {root}: {', '.join(open_threads) or 'none'}",
        )

    async def _close_thread_pages(self, slot: PageSlot) -> None:
        if self._page_is_open(slot.page):
            await self._maybe_await(slot.page.close())
        # Even when the page had already closed, as when a human closed it: every tab it
        # opened, its popup threads and the tabs Patchright never reported, belongs to it.
        await self._popups.close_popups_of(slot.target_id)

    async def _forget_blank_history_entry(self, page: Any) -> None:
        # The window commits about:blank before its first page, so back() would return
        # to it; a thread's history starts at the page it was opened at.
        session = await self._maybe_await(self._context().new_cdp_session(page))
        try:
            await self._maybe_await(session.send("Page.resetNavigationHistory"))
        except Exception as exc:
            # Chrome refuses while the page itself runs a history navigation; the page
            # then owns its history and the open has still succeeded.
            if HISTORY_NAVIGATION_PENDING_MESSAGE not in str(exc):
                raise
        finally:
            await self._best_effort_detach(session)

    async def _create_new_window_page(self) -> tuple[Any, str]:
        context = self._context()
        # Look for the new window only among pages that appear after it is created. Asking
        # a page that was already open for its target would hang if that page is stuck in
        # a script: detaching the session first sends the page's renderer
        # Runtime.runIfWaitingForDebugger, which it never answers. Hold the pages, not
        # their id()s: a page freed meanwhile could pass its id on to the new one.
        existing_pages = list(context.pages)
        session = None
        target_id: str | None = None
        try:
            # Browser-level: a page-level session needs an open page, and opening one
            # while Chrome has no window shows it in the foreground.
            session = await self._maybe_await(context.browser.new_browser_cdp_session())
            # Chrome restores the last session's tabs only into a window it opens after
            # having none: at launch, or after a human closes every Surf window.
            chrome_was_windowless = not await list_tabs(session)
            # Background: a foreground window activates Chrome and steals focus from the
            # user's app (measured on macOS). Thread.focus() raises a window on request.
            response = await self._maybe_await(
                session.send("Target.createTarget", {"url": BLANK_PAGE_URL, "newWindow": True, "background": True})
            )
            target_id = response.get("targetId") if isinstance(response, dict) else None
            if not isinstance(target_id, str) or not target_id:
                raise RuntimeError("Patchright CDP Target.createTarget returned no valid target")
            try:
                page = await self._wait_for_created_target_page(context, existing_pages, target_id)
            except Exception:
                await self._best_effort_close_target(session, target_id)
                raise
            finally:
                # Even when the window failed: restored tabs left as Chrome's only tabs
                # would make every later creation skip this sweep.
                if chrome_was_windowless:
                    await self._close_restored_tabs(session, new_window_target_id=target_id)
            return page, target_id
        finally:
            await self._best_effort_detach(session)

    async def _close_restored_tabs(self, session: Any, *, new_window_target_id: str) -> None:
        # Chrome was windowless, so every other tab was restored into the new window, and
        # restored tabs are orphans: threads never outlive their windows. While any window
        # is open, other tabs belong to someone, such as a popup a thread's page opened or
        # a tab a human opened, so they are never swept. The restored tabs were present
        # when open() returned in every live run; one restored later stays open.
        for tab in await list_tabs(session):
            if tab["targetId"] != new_window_target_id:
                await close_tab(session, tab["targetId"])

    async def _wait_for_created_target_page(
        self, context: Any, existing_pages: list[Any], target_id: str
    ) -> Any:
        deadline = time.monotonic() + CDP_NEW_WINDOW_TIMEOUT_S
        while time.monotonic() < deadline:
            candidates = [
                page
                for page in list(context.pages)
                if not any(page is existing for existing in existing_pages) and self._page_is_open(page)
            ]
            for page in candidates:
                if await self._page_target_id(context, page) == target_id:
                    return page
            await asyncio.sleep(CDP_NEW_WINDOW_POLL_INTERVAL_S)
        raise RuntimeError(
            f"Patchright CDP Target.createTarget did not expose target {target_id!r}"
        )

    async def _require_unzoomed(self, page: Any) -> None:
        # Saved zoom is cleared at launch, but a human can still zoom a window mid-session.
        # Under zoom, screenshots show only part of the viewport, or nothing once scrolled.
        session = await self._maybe_await(self._context().new_cdp_session(page))
        try:
            # The renderer answers this query, so a page stuck in a script would block the bridge for good.
            metrics = await asyncio.wait_for(
                self._maybe_await(session.send("Page.getLayoutMetrics")), ACTION_TIMEOUT_MS / 1000
            )
        except TimeoutError as exc:
            raise BridgeCodedError(
                ErrorCode.ACTION_TIMEOUT, f"page did not report its zoom within {ACTION_TIMEOUT_MS}ms; it may be busy"
            ) from exc
        # Detach only after an answer: behind an unanswered query the detach hangs too.
        # A session left attached on failure ends when its page closes.
        await self._best_effort_detach(session)
        zoom = metrics["cssVisualViewport"]["zoom"]
        if not math.isclose(zoom, 1, abs_tol=ZOOM_TOLERANCE):
            raise BridgeCodedError(
                ErrorCode.PAGE_ZOOMED,
                f"browser zoom is {zoom:.0%} on this page, and screenshots need 100%; reset it in the window "
                "(Ctrl+0, or Cmd+0 on macOS; if that does not help, Settings > Appearance > Page zoom) "
                "or stop the bridge, which closes every thread and clears saved zoom at the next launch",
            )

    async def _page_target_id(self, context: Any, page: Any, *, keep_session: bool = False) -> str | None:
        session = None
        try:
            session = await self._maybe_await(context.new_cdp_session(page))
            response = await self._maybe_await(session.send("Target.getTargetInfo"))
            target_id = response["targetInfo"]["targetId"]
            if not isinstance(target_id, str) or not target_id:
                raise RuntimeError("Patchright CDP Target.getTargetInfo returned no valid targetId")
            if not self._page_is_open(page):
                return None
            return target_id
        except Exception as exc:
            if self._is_closed_target_error(exc) or not self._page_is_open(page):
                return None
            raise
        finally:
            if not keep_session:
                await self._best_effort_detach(session)

    async def _best_effort_close_target(self, session: Any, target_id: str) -> None:
        try:
            await self._maybe_await(session.send("Target.closeTarget", {"targetId": target_id}))
        except Exception:
            return

    async def _best_effort_detach(self, session: Any | None) -> None:
        if session is None:
            return
        detach = getattr(session, "detach", None)
        if detach is None:
            return
        try:
            await self._maybe_await(detach())
        except Exception:
            return

    def _page_url(self, page: Any) -> str:
        return str(getattr(page, "url", "") or "")

    async def _restart_closed_context(self) -> NoReturn:
        # This runtime cannot safely invoke the CLI process's lifecycle coordinator:
        # Patchright objects are thread-affine and this is the bridge server thread.
        # Stop and request a fresh bridge process so LocalBridgeClient.before_start
        # performs the cookie preflight before the next persistent-context launch.
        self._cancel_shutdown_request()
        try:
            await self.stop_async()
        except Exception:
            self.manager = None
            self.browser_or_context = None
            self.pages.clear()
        raise RuntimeError(CONTEXT_RESTART_REQUIRED)

    def _page_is_open(self, page: Any) -> bool:
        try:
            return not page.is_closed()
        except Exception as exc:
            if self._is_closed_target_error(exc):
                return False
            raise

    def _is_closed_target_error(self, exc: Exception) -> bool:
        return CLOSED_TARGET_MESSAGE in str(exc)

    async def _body_text(self, page: Any) -> str:
        try:
            text = await self._maybe_await(page.locator("body").inner_text(timeout=SNAPSHOT_BODY_TIMEOUT_MS))
        except Exception:
            text = await self._maybe_await(page.evaluate(HTML_WITHOUT_SECRET_VALUES_SCRIPT))
        return text + ("" if text.endswith("\n") else "\n")

    async def _snapshot(self, slot: PageSlot) -> str:
        page = slot.page
        parts = ["snapshot:"]
        secret_before_read = await self._has_secret_field(page)
        try:
            aria = await self._mask_secrets(page, await self._aria_snapshot(page), secret_before_read)
        except Exception:
            try:
                aria = await self._mask_secrets(
                    page, await self._aria_snapshot(page.locator("body")), secret_before_read
                )
            except Exception:
                aria = (await self._body_text(page)).strip()
        if aria:
            parts.append(str(aria).rstrip())
        return "\n".join(parts).rstrip() + "\n"

    async def _aria_snapshot(self, target: Any) -> str:
        # Match Playwright CLI snapshots: AI-mode ARIA tree, optional depth, optional boxes.
        options = {
            "mode": "ai",
            "timeout": SNAPSHOT_ARIA_TIMEOUT_MS,
            "depth": SNAPSHOT_DEPTH,
            "boxes": SNAPSHOT_BOXES,
        }
        try:
            return str(await self._maybe_await(target.aria_snapshot(**options)))
        except TypeError:
            try:
                return str(await self._maybe_await(target.aria_snapshot(mode="ai", timeout=SNAPSHOT_ARIA_TIMEOUT_MS)))
            except TypeError:
                return str(await self._maybe_await(target.aria_snapshot(timeout=SNAPSHOT_ARIA_TIMEOUT_MS)))

    async def _has_secret_field(self, page: Any) -> bool:
        try:
            return await asyncio.wait_for(self._scan_for_secret_field(page), SNAPSHOT_ARIA_TIMEOUT_MS / 1000)
        except Exception:
            return True

    async def _scan_for_secret_field(self, page: Any) -> bool:
        # The event loop only runs inside a call, so page.frames misses a frame
        # attached between calls until one round trip delivers the event.
        await self._maybe_await(page.evaluate("() => 0"))
        found_in_frames = await asyncio.gather(
            *(self._maybe_await(frame.evaluate(FRAME_HAS_SECRET_FIELD_SCRIPT)) for frame in page.frames)
        )
        return any(found_in_frames)

    async def _mask_secrets(self, page: Any, snapshot_text: str, secret_before_read: bool) -> str:
        # One scan spares normal fields a round trip each. It looks on both sides of
        # the read: a submit can remove a field just read, or a form can render during it.
        if not secret_before_read and not await self._has_secret_field(page):
            return snapshot_text
        value_lines = input_value_lines(snapshot_text)
        may_hold_secret = await asyncio.gather(
            *(self._may_hold_secret(page, value_line.ref) for value_line in value_lines)
        )
        return mask_input_values(
            snapshot_text,
            [value_line for value_line, masked in zip(value_lines, may_hold_secret, strict=True) if masked],
        )

    async def _may_hold_secret(self, page: Any, ref: str | None) -> bool:
        # Identify the field by element, not by comparing values: a fill can change
        # the value between two reads, and a stale ref then masks instead of leaking.
        # Without a ref the element is unreachable (e.g. pointer-events: none).
        if ref is None:
            return True
        try:
            # All probes share the wait, so a page with thousands of inputs needs the
            # snapshot's full budget or its text fields would mask at random.
            return await self._maybe_await(
                page.locator(f"aria-ref={ref}").evaluate(SECRET_FIELD_TEST, timeout=SNAPSHOT_ARIA_TIMEOUT_MS)
            )
        except Exception:
            return True

    async def _fill_or_select(self, locator: Any, text: str) -> None:
        # Playwright's fill rejects <select>; agents reach for fill on dropdowns,
        # so choose the option whose value or visible label matches instead.
        tag = await self._maybe_await(locator.evaluate("element => element.tagName", timeout=ACTION_TIMEOUT_MS))
        if tag == "SELECT":
            await self._maybe_await(locator.select_option(text, timeout=ACTION_TIMEOUT_MS))
        else:
            await self._maybe_await(locator.fill(text, timeout=ACTION_TIMEOUT_MS))

    async def _act(self, target: str, locator: Any, action: Any, *, editable: bool) -> None:
        try:
            await self._maybe_await(action())
        except Exception as exc:
            if not isinstance(exc, PlaywrightTimeoutError):
                raise
            raise await self._actionability_error(target, locator, editable=editable) from exc

    async def _actionability_error(self, target: str, locator: Any, *, editable: bool) -> BridgeCodedError:
        """Classify a timed-out action by probing the element's current state."""
        probe_ms = ACTIONABILITY_PROBE_TIMEOUT_MS
        if await self._maybe_await(locator.count()) == 0:
            if self._is_ref_target(target):
                return BridgeCodedError(ErrorCode.STALE_REF, STALE_REF_MESSAGE.format(ref=target.removeprefix("@")))
            return BridgeCodedError(ErrorCode.NOT_FOUND, f"target {target!r} no longer matches any element")
        if not await self._maybe_await(locator.is_visible()):
            return BridgeCodedError(ErrorCode.NOT_VISIBLE, f"target {target!r} is not visible")
        if not await self._maybe_await(locator.is_enabled(timeout=probe_ms)):
            return BridgeCodedError(ErrorCode.NOT_ENABLED, f"target {target!r} is disabled")
        if editable and not await self._maybe_await(locator.is_editable(timeout=probe_ms)):
            return BridgeCodedError(ErrorCode.NOT_EDITABLE, f"target {target!r} is not editable")
        blocker = await self._maybe_await(locator.evaluate(HIT_TEST_BLOCKER_JS, timeout=probe_ms))
        if blocker:
            return BridgeCodedError(
                ErrorCode.INTERCEPTED, f"target {target!r} is covered by {blocker}; dismiss or target that element"
            )
        return BridgeCodedError(
            ErrorCode.ACTION_TIMEOUT, f"target {target!r} looked actionable but the action did not complete in {ACTION_TIMEOUT_MS}ms"
        )

    async def _wait_for(
        self, page: Any, *, text: str | None, gone: str | None, url: str | None, timeout_ms: int
    ) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            unmet = await self._unmet_conditions(page, text=text, gone=gone, url=url)
            if not unmet:
                return
            if time.monotonic() >= deadline:
                raise BridgeCodedError(
                    ErrorCode.WAIT_TIMEOUT,
                    f"wait timed out after {timeout_ms}ms: {'; '.join(unmet)}; "
                    f"page url {self._page_url(page)!r}, title {await self._title(page)!r}",
                )
            await asyncio.sleep(WAIT_POLL_INTERVAL_S)

    async def _unmet_conditions(self, page: Any, *, text: str | None, gone: str | None, url: str | None) -> list[str]:
        unmet: list[str] = []
        if text is not None and await self._visible_text_count(page, text) == 0:
            unmet.append(f"text {text!r} not visible")
        if gone is not None and await self._visible_text_count(page, gone) > 0:
            unmet.append(f"text {gone!r} still visible")
        if url is not None and not fnmatch.fnmatchcase(self._page_url(page), url):
            unmet.append(f"url does not match {url!r}")
        return unmet

    async def _visible_text_count(self, page: Any, text: str) -> int:
        return await self._maybe_await(page.get_by_text(text).filter(visible=True).count())

    def _is_ref_target(self, target: str) -> bool:
        return target.startswith("@") or NATIVE_ARIA_REF_PATTERN.fullmatch(target) is not None

    async def _target_locator(self, slot: PageSlot, target: str) -> Any:
        normalized = target[1:] if target.startswith("@") else target
        is_native_ref = NATIVE_ARIA_REF_PATTERN.fullmatch(normalized) is not None
        if target.startswith("@") and not is_native_ref:
            raise BridgeCodedError(ErrorCode.STALE_REF, STALE_REF_MESSAGE.format(ref=normalized))
        if is_native_ref:
            return await self._ref_locator(slot, normalized)
        return await self._selector_locator(slot, target)

    async def _ref_locator(self, slot: PageSlot, ref: str) -> Any:
        try:
            locator = slot.page.locator(f"aria-ref={ref}")
            if await self._maybe_await(locator.count()) != 1:
                raise RuntimeError
            return locator
        except Exception as exc:
            raise BridgeCodedError(ErrorCode.STALE_REF, STALE_REF_MESSAGE.format(ref=ref)) from exc

    async def _locator_candidates(self, locator: Any, *, limit: int) -> list[Any]:
        candidates: list[Any] = []
        try:
            count = min(await self._maybe_await(locator.count()), limit)
        except Exception:
            return candidates
        for index in range(count):
            try:
                candidates.append(locator.nth(index))
            except Exception:
                continue
        return candidates

    async def _selector_locator(self, slot: PageSlot, selector: str) -> Any:
        try:
            locator = slot.page.locator(selector)
            if await self._maybe_await(locator.count()) < 1:
                raise RuntimeError
            candidate = locator.first
            if hasattr(candidate, "is_visible") and not await self._maybe_await(candidate.is_visible(timeout=250)):
                for item in await self._locator_candidates(locator, limit=50):
                    if await self._maybe_await(item.is_visible(timeout=250)):
                        return item
            return candidate
        except Exception as exc:
            raise BridgeCodedError(
                ErrorCode.NOT_FOUND, f"target {selector!r} is neither a current snapshot ref nor a matching selector"
            ) from exc

    async def _metadata(self, slot: PageSlot) -> dict[str, str | int]:
        page = slot.page
        return {"page_id": slot.page_id, "url": str(getattr(page, "url", "") or ""), "title": await self._title(page)}

    async def _title(self, page: Any) -> str:
        try:
            return str(await self._maybe_await(page.title()))
        except Exception:
            return ""

    def _format_opened(self, page: Any) -> str:
        return f"opened {getattr(page, 'url', '')}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("SURF_AGENT_PORT", "9346")))
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("SURF_AGENT_PROFILE_DIR") or "",
    )
    parser.add_argument("--disk-cache-dir", default="")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--app-id", default=os.environ.get("SURF_AGENT_APP_ID") or DEFAULT_PATCHRIGHT_APP_ID)
    args = parser.parse_args(argv)
    home = os.environ.get("SURF_AGENT_HOME")
    default_profile_dir = (Path(home).expanduser() if home else Path(PlatformDirs("surf-agent", appauthor=False).user_data_dir)) / "profiles" / "chrome"
    profile_dir = Path(args.profile_dir).expanduser() if args.profile_dir else default_profile_dir
    disk_cache_dir = Path(args.disk_cache_dir).expanduser() if args.disk_cache_dir else None
    runtime = PatchrightRuntime(
        profile_dir=profile_dir, disk_cache_dir=disk_cache_dir, headless=args.headless, app_id=args.app_id
    )
    create_bridge_server(runtime, args.port).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
