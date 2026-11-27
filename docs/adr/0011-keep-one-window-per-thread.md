---
status: accepted
---

# Keep one window per thread

Each thread has its own Chrome window. That started from a concern that a tab behind another tab is hidden to Chrome and stops rendering. The concern turned out to be moot: Patchright sends `Emulation.setFocusEmulationEnabled` to every page unless its undocumented `focus_control` option is set, and that keeps a background tab visible to its page. Measured on Linux, a background tab with emulation ran about 60 animation frames per second and clicked in 0.03 s; without emulation it reported `hidden`, ran no frames and its click timed out. So tabs became possible, and threads as tabs in one shared window were built and reviewed, then dropped.

The deciding reason is isolation, not rendering. A window has one front tab and one close button, and with several agents and a human working at once, every thread in a shared window contends for both. A popup or `target=_blank` tab opened by any thread becomes the window's front tab, so while the user types a captcha or a code in one thread's tab, another thread's popup can take the view and the keystrokes. Closing the shared window by hand ends every agent's threads. A window per thread gives each thread its own front tab and close button.

## Considered options

Weighted for isolation during a human handoff (35), a tidy desktop (20), simplicity (25), never taking focus (10) and cost to ship (10):

- **One window per thread.** Chosen (4.40).
- **Every thread a tab in one shared window.** Rejected (3.70): the shared front tab and close button above.
- **Tabs for agent-only threads, converted to a window when a human is needed.** Rejected (2.95): CDP cannot move an existing tab to another window (`Browser.executeBrowserCommand` accepts only `openTabSearch`, `closeTabSearch` and `openGlic`), reopening the URL loses page state such as forms and OAuth state, and the extension API that can move tabs (`chrome.windows.create({tabId})`) needs an extension that branded Chrome no longer loads from `--load-extension`.
- **Tab groups.** Rejected: a window still has one active tab across groups, and CDP has no tab group API.
- **One window per agent, its threads as tabs.** Rejected: `Target.createTarget` has no window parameter and puts new tabs in the most recently activated window, which Surf cannot choose without activating it; separate browser contexts would get their own windows but lose the profile's logins; and Surf has no agent identity.

A second opinion (Gemini 4 Carbon) reached the same choice and confirmed these blockers in Chromium's source.

## Consequences

- Thread windows depend on focus emulation too. A thread window usually sits out of view and never has OS focus; without emulation, its page measured hidden or unfocused, at 0 to 6 animation frames per second, with actions taking a second or timing out. Surf adds no runtime check for it: Patchright keeps emulation on by default, as upstream Playwright does, so a check would cost every thread creation a round trip and an extra failure path to guard an unlikely upstream change. `packages/surf-agent/tests/test_patchright_focus_emulation.py` pins it instead: it reads `document.hasFocus()` on new thread pages before any input. A background window never has OS focus, so the read fails without emulation wherever the compositor puts the window; it comes before input because a click gives the page focus.
- Window clutter stays a desktop concern. On Niri, a tabbed column can show several Surf windows in one column while each stays independent; joining new windows into that column needs a compositor rule or an IPC consumer (`niri msg action consume-or-expel-window-left --id <id>`), and conflicts with opening them maximized.
