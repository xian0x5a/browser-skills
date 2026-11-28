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
- The action output that opened it ends with a line `opened popup Thread("app/1"): <url>`. Action outputs are `new`, `open`, `back`, `click`, `click-at`, `fill`, `upload`, `type`, `press`, `scroll`, `sleep`, `wait-for` and `focus`. A popup that opens after its action returned is announced by the next action output in the same family. JSON outputs (`state`, `list`, `eval`, `responses`, `response-body`, `close-matching`), `snapshot` and `text` never carry announcements.
- `thread.popups() -> list[Thread]` returns the open popup threads of a thread, in opening order. Called on a popup thread, it returns its root's.
- No handle changes page. `Thread("app")` keeps acting on the app's page while popups come and go.
- Every page command works on a popup thread while its page is open, and `open(url)` navigates the popup. Once its page has closed, every command raises `page_closed` with a message naming the family's open threads (for example `popup thread app/1 closed; open: app, app/2`), and nothing reopens it. `new` refuses popup thread names, and `safe_thread_name` accepts `/` only in the `<name>/<digits>` form.
- `close()` on a thread closes its page and every popup thread (already true since #68). `close()` on a popup thread closes that page and the popups it opened, then drops their popup threads.
- `rename-thread` refuses a popup thread, and refuses a thread that has open popup threads (layer 2 may move them instead).
- Docs (SKILL.md and python-api.md): treat a popup that closed itself as an unknown outcome, and check the opener page rather than the closed handle (Codex's `browserAuth` guidance).

## Design

- Family membership for acting comes from Patchright's `popup` event on every page of a family; those are the pages Patchright has set up, the only ones a thread can act on. Ownership for closing stays with `PopupTracker` (`backends/patchright/tabs.py`), which also covers popups Patchright never reports.
- A popup thread's slot needs its CDP target ID, so that closing it can close its own descendants through `PopupTracker.close_popups_of`. Read it with a page-level session and `Target.getTargetInfo`, which the browser answers, and never detach that session: detaching sends `Runtime.runIfWaitingForDebugger` to the renderer and hangs on a busy page (issue #53). The session ends when the page closes.
- `PageSlot` gains the root name for popup threads (None for threads agents name), so naming, `popups()` and the error message need no name parsing.
- Announcements: per root, a list of pending lines appended by the `popup` handler, flushed into the next action output of any thread in that family.
- `_page(thread)` must never call `_new_page` for a popup thread name; that is the fail-closed rule.
- Closing prunes the popup slots whose pages closed, after `close_popups_of` returns.

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

## Surprises & Discoveries

- `target=_blank` pages get no `window.opener` (Chrome implies `noopener`), yet `Target.targetCreated` still names their opener, and Patchright still fires `popup` for them.
- Chrome refuses to prerender pages Patchright controls (`PrerenderingDisabledByDevTools`), so a page's target ID is stable.
- T3 Code's preview lists a `target=_blank` tab as unowned, which the agent cannot close, and doesn't list a `window.open` popup at all.

## Decisions

- Popup threads over auto-follow and over `pages()` / `switch()`: see ADR 0012.
- Flat numbering per root, never reused: a closed `app/1` must keep failing even after newer popups open (Codex's IAB uses never-reused IDs too).
- No per-turn cleanup like Codex's (agent-created tabs closed at turn end): Surf closes popups with their thread, and Surf has no notion of a turn.

## Outcomes & Retrospective

(To fill when layer 1 lands.)
