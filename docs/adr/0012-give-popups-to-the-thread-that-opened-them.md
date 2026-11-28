---
status: accepted
---

# Give popups to the thread that opened them

A popup that a thread's page opens, directly or through its own popups, becomes a popup thread of that thread. It is named `<thread>/<n>` in the order the popups open, and a number is never reused. The action that opened it announces it, and `thread.popups()` lists the open ones. No handle ever moves to another page. A popup thread whose page closed fails with `page_closed` and is never reopened, and closing a thread closes its popup threads. Before this, a thread reached only its own page, so OAuth sign-in and 3-D Secure popups, which hand their result back through `window.opener`, could not be completed; opening the popup's URL in a thread of its own loses that opener.

The deciding reason is how agents run Surf: several actions per Python cell, with the output read only once the cell ends. A thread that followed new pages would move the rest of a cell onto a page the script did not choose, and the notice of the switch would arrive after the damage.

## Considered options

Weighted for completing popup flows (35), predictable targeting (25), API simplicity (20), cost and risk (10) and fit with the thread model (10):

- **Popup threads owned by the opener's thread.** Chosen (4.25).
- **Auto-follow: a thread acts in its newest open page and returns when that closes.** Rejected (3.95): it moves a cell's later lines onto a page the script did not choose. Stagehand works this way; agent-browser does too, but added `--pin-tab` to turn it off.
- **A thread's pages behind `pages()` and `switch(i)`.** Rejected (3.25): a current page that every handle of the name shares, and indices that shift when a popup closes. Codex's browser plugin avoids both with fixed tab handles.
- **Reopening the popup's URL in a thread of its own.** Rejected: the page loses `window.opener`.

Prior art: Codex's browser plugin keeps fixed tab handles with IDs it never reuses, fails on a closed tab with an error naming the open ones, and never tells the agent a popup opened. Chrome DevTools MCP reports new pages in the output of the action that opened them. Playwright MCP, browser-use and T3 Code's preview list new pages without switching to them.

## Consequences

- Thread names gain `/`, accepted only as `<thread>/<n>` for popup threads, which only Surf creates.
- Ownership comes from the opener Chrome names when it creates a tab, which Surf records at that moment (issue #66). `target=_blank` tabs count too: Chrome names their opener even though the page gets no `window.opener`.
- Tabs opened with Ctrl-, Shift- or middle-click have no opener and belong to no thread; Surf's clicks never use modifiers, so only a human opens them.
