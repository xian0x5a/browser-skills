---
status: open
---

# Popup threads (issue #71)

## Goal

Let an agent act in the popups and tabs its thread's page opens, such as an OAuth "Sign in with …" popup, a 3-D Secure window or a `target=_blank` tab, without losing `window.opener`. Decision and rejected options: [ADR 0012](../docs/adr/0012-give-popups-to-the-thread-that-opened-them.md). Terms: [GLOSSARY.md](../GLOSSARY.md).

## Intention

A thread reaches only its own page today. Probe (headless, local pages): after `click("#signin")` opened a login popup, the click returned `clicked`, `snapshot()` still showed the opener, and `fill("#user", …)` failed with "neither a current snapshot ref nor a matching selector". Opening the popup's URL in a thread instead drops `window.opener`, so the popup can't post the sign-in result back.

## Contract

- A popup that a thread's page opens, directly or through its popup threads, becomes popup thread `<root>/<n>`. `<root>` is the thread whose own page started the chain, so a popup's popup is `app/2`, never `app/1/1`. `n` counts up from 1 per root name for the bridge's life and is never reused, so a stale handle can never reach a newer popup.
- The action output that opened it ends with a line `opened popup Thread("app/1"): <url>`. Action outputs are `new`, `open`, `back`, `click`, `click-at`, `fill`, `upload`, `type`, `press`, `scroll`, `sleep` and `wait-for`. A popup that opens after its action returned is announced by the next action output in the same family. An announcement names only popup threads still open when the output is written, with their URL at that moment. JSON outputs (`state`, `list`, `eval`, `responses`, `response-body`, `close-matching`), `snapshot`, `text` and `focus` (whose output the Python API drops) never carry announcements.
- `thread.popups() -> list[Thread]` returns the open popup threads of a thread, in opening order. Called on a popup thread, it returns its root's.
- No handle changes page. `Thread("app")` keeps acting on the app's page while popups come and go.
- Every page command works on a popup thread while its page is open, and `open(url)` navigates the popup. Once its page has closed, every command but `state` (which reports it closed) and `close` (which does nothing) raises `page_closed` with a message naming the family's open threads (for example `popup thread app/1 is not open …; open threads of app: app, app/2`), and nothing reopens it; so does a call in flight when the page closes. `new` refuses popup thread names, and `safe_thread_name` accepts `/` only in the `<name>/<digits>` form.
- `close()` on a thread closes its page and every popup thread (already true since #68). `close()` on a popup thread closes that page and the popups it opened, then drops their popup threads.
- `rename-thread` refuses a popup thread, and refuses a thread that has open popup threads (layer 2 may move them instead).
- Docs (SKILL.md and python-api.md): treat a popup that closed itself as an unknown outcome, and check the opener page rather than the closed handle (Codex's `browserAuth` guidance).

## Design

- Family membership and ownership both come from the openers Chrome names, which `PopupTracker` (`backends/patchright/tabs.py`) records as each tab is created. Patchright only supplies the page objects: on the context's `page` event, which fires for every page Patchright sets up, the bridge reads the page's CDP target ID and walks the recorded openers up to the nearest page a thread holds; that thread's root owns the popup. Patchright's per-page `popup` event is not used: it fires only while the opener is still open, so a popup that opens another and closes itself in one task would be lost.
- The target ID is read with a page-level session and `Target.getTargetInfo`, which the browser answers even while the renderer spins (probe: 1 ms). The session is never detached: detaching sends `Runtime.runIfWaitingForDebugger` to the renderer and hangs on a busy page (issue #53). The session ends when the page closes.
- Adoption runs one page at a time, behind a FIFO lock, so numbers follow the order Patchright reports the pages.
- Every `PageSlot` holds its page's target ID. Closing any thread, popup thread or not, closes its page and then `PopupTracker.close_popups_of(target_id)`, which also covers popups Patchright never reported. A popup thread's slot is dropped on its page's `close` event.
- Report wait: an action during which Chrome opened a tab in the family waits, up to 3 s, until every such tab still open is a thread's page. It compares target IDs, so a slow popup from an earlier action can't stand in for this one's.
- Announcements: per root, the names of popup threads not yet announced, flushed into the next action output of any thread in that family; only those still open are named, with the URL read then.
- `_page(thread)` must never call `_new_page` for a popup thread name; that is the fail-closed rule.
- Popup thread names are parsed (`<root>/<n>`) rather than stored on `PageSlot`: only Surf creates them, and `safe_thread_name` rejects any other name with `/`.

## Work plan

1. Layer 1, end to end: registration, naming, announcements in action output, `popups()`, fail-closed popup threads, closing a popup thread, name validation, docs. Acceptance: the live sign-in flow below.
2. Layer 2: `rename-thread` moving popup threads, `browser.threads()` showing each popup thread's root, and anything layer 1's review surfaces.

## Validation

- An independent agent writes the acceptance tests from the contract before or alongside the implementation (sonnet, medium):
  - live: a click opens a login popup; `popups()` returns `Thread("app/1")`, which fills and submits it; the popup posts to `window.opener` and closes itself; the opener shows the result; the closed `app/1` raises `page_closed` naming `app`;
  - live: a `target=_blank` link becomes a popup thread;
  - live: a popup's popup is `app/2`, closing `app/1` closes `app/2`, and closing `app` closes everything and makes the bridge idle;
  - unit where cheap: name validation, `new` refusing popup thread names, no announcements in JSON outputs.
- Independent review (fable, high) before shipping. Then run the full live suite once: `SURF_TEST_LIVE_PATCHRIGHT=1 uv run pytest -q` in `packages/surf-agent`. Live tests stay local and headless; never load Google.

## Progress

- 2026-11-27: Decided D after prior-art research (Codex's browser plugin read from its local install, Playwright MCP, Chrome DevTools MCP, browser-use, Stagehand, agent-browser, T3 Code's preview probed live). Reports: `~/.agents/artifacts/outputs/browser-skills/2026-11-27/popup-prior-art/report.md`, `~/.agents/artifacts/outputs/browser-skills/2026-11-27/codex-browser-use/report.md`. Issue #71, ADR 0012 and this plan written; layer 1 not started.
- 2026-11-27: Layer 1 implemented on Patchright's `popup` event (f7ba17b). Unit 276 passed, the live popup tests 9/9 in three runs, the full live suite 356 passed.
- 2026-11-27: Review (fable, high) found four defects: a popup opened by a popup that closed in the same task was lost; announcements outlived their popups; `focus` dropped announcements; `popups()[-1]` could name an older popup. Membership moved onto Chrome's openers (Design above).

## Surprises & Discoveries

- `target=_blank` pages get no `window.opener` (Chrome implies `noopener`), yet `Target.targetCreated` still names their opener, and Patchright still fires `popup` for them.
- Chrome refuses to prerender pages Patchright controls (`PrerenderingDisabledByDevTools`), so a page's target ID is stable.
- Patchright's click does not always wait for the popup it opens: the `popup` event can come after the action returned. Chrome, though, announces the new tab before it acknowledges the input, so an action can tell cheaply whether it opened one.
- Patchright fires `popup` only while the opener is open, and reports a popup only once its first navigation commits. A popup whose first response takes over 3 s is announced by a later action; one that never commits (a 204) stays a blank tab until its owner closes.
- A sign-in popup's `postMessage` reaches the opener about 60 ms after the popup closed itself, so a check of the opener must wait for the result.
- T3 Code's preview lists a `target=_blank` tab as unowned, which the agent cannot close, and doesn't list a `window.open` popup at all.

## Decisions

- Popup threads over auto-follow and over `pages()` / `switch()`: see ADR 0012.
- Flat numbering per root, never reused: a closed `app/1` must keep failing even after newer popups open (Codex's IAB uses never-reused IDs too).
- `focus` is not an action output: `Thread.focus()` returns nothing, so announcements flushed into it would be lost, and focusing a window opens no popup.
- Closing a thread does not wait for calls in flight on its popup threads: a popup can close itself at any moment, so callers of a popup thread handle `page_closed` anyway, and such a call fails with the family's open threads.
- No per-turn cleanup like Codex's (agent-created tabs closed at turn end): Surf closes popups with their thread, and Surf has no notion of a turn.

## Outcomes & Retrospective

(To fill when layer 1 lands.)
