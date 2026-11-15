---
status: done
---

## Goal

Calls on different Surf threads run concurrently on the shared Patchright bridge, so one agent's
long call (navigation, `wait-for`, `sleep`) no longer blocks other agents. Tracked as issue #52.

## Intention

Several agents share one bridge per profile, which keeps the user's logins and window rules.
Today the bridge is a single-threaded stdlib `HTTPServer`, and every call runs to completion before
the next request is read. Branch `fix/busy-bridge-health` (stacked below this one) made waiting
honest; this plan removes the wait.

## Scope & Constraints

- One Patchright driver, one persistent context, one page (window) per Surf thread: unchanged.
- All Patchright objects stay on one event loop in one OS thread.
- Calls on the same Surf thread keep their order.
- No relaunch inside a bridge that is stopping: a relaunch must go through the client's
  cross-process launch guard, which runs the cookie-import preflight.
- Out of scope: Google pacing (already cross-process in `surf-google-search/coordination.py`).

## Design

Layer 1, runtime made safe for interleaved calls (server unchanged):

- `PatchrightRuntime.call_async(name, args)`: public coroutine, one per request.
- Per-Surf-thread `asyncio.Lock`: one thread's calls stay ordered. `rename-thread` takes both
  locks in sorted order; `close-matching` takes each matched thread's lock in turn.
- Browser lock (`asyncio.Lock`): launch-once and window creation, from the managed-target snapshot
  through `Target.createTarget`, the unmanaged-tab sweep and registration in `self.pages`.
  Navigation runs outside it.
- In-flight count plus a one-way `stopping` state. Idle stop fires only when no call is in flight
  and no page is visible. Once stopping (idle stop, restart, explicit stop), new calls fail with
  `CONTEXT_RESTART_REQUIRED`, which the client already handles: wait for exit, relaunch, retry.
  Stop and restart run once.

Layer 2, server swap: uvicorn + Starlette serve `GET /health` and `POST /call` on the runtime's loop.
After each response, `finish_response` decides whether the process exits (explicit stop, idle stop,
closed-context restart) and returns only once the shared stop task has finished; the server then
sets `server.should_exit`. A graceful-shutdown timeout cancels a wedged call so exit cannot hang.
A call runs to completion even if its client disconnects (see Decisions).

## Decisions

- uvicorn + Starlette over a threaded stdlib front end with a loop thread (weighted 3.95 vs 3.65,
  advisor review): one loop in one thread by construction, standard graceful shutdown.
  FastAPI rejected: its validation and OpenAPI layers add Pydantic for two internal endpoints.
- Readers-writer lock rejected (advisor): asyncio has none in the stdlib, and an exclusive stop
  queued behind a long or wedged call would freeze every new call. Stop/restart are terminal, so
  a one-way `stopping` state covers them.
- Hand-written asyncio HTTP rejected: reimplements HTTP parsing.
- Cancel-on-disconnect rejected after review (weighted 2.7 vs 4.1 for dropping it): cancelling an
  abandoned call mid-launch, mid-window-creation or mid-stop stranded half-made state (a second
  driver, an unregistered window that blocked idle stop, an exit request skipped). Every
  browser-wide operation would have to stay cancellation-safe forever, while the gain is small:
  bounded calls end on their own timeouts, and a frozen renderer hangs the next call on that page
  anyway. A wedged call blocks only its own thread; `stop_bridge()` recovers it.

## Validation

Independent agents write tests before each layer and see them fail first.

Layer 1 (runtime level, gated async fakes):
1. Two threads run `new` at once: both windows survive, each registered to its thread.
2. Two first calls arrive at once: one launch.
3. A closes the last page while B's `new` is mid-creation: no idle stop; B succeeds.
4. A long call on thread A does not delay a call on thread B; two calls on one thread stay ordered.
5. Calls arriving while stopping get `CONTEXT_RESTART_REQUIRED`; stop runs once.

Layer 2 (HTTP level):
6. Health answers while a call is in flight.
7. A call abandoned mid-window-creation still registers its window; closing every thread then
   idle-stops the bridge.
8. Stop during an in-flight call returns promptly and the process exits; while a stop is still
   running, concurrent calls get `CONTEXT_RESTART_REQUIRED` and the process does not exit early.

Live: scratchpad repro with two clients on an isolated `SURF_AGENT_HOME` and port, headless where
possible; never live Google.

## Progress

- [x] Layer 1 tests red (5 of 6; the overlap case passes on the old runtime and guards over-locking).
- [x] Layer 1 implemented; full suite green (8e2c985).
- [x] Layer 2 implemented (766ee98); HTTP-level tests written after it, by an independent agent.
- [x] Live repro: B's call returned in 0.08 s while A's 4 s call ran (2/2); bridge exited after final close.
- [x] Review findings fixed (cancel-on-disconnect dropped; exit waits for stop) in 625fff1;
  re-review found no defects.
- [x] Runtime pinned at 625fff1; ADR 0010; backend doc updated.

## Surprises & Discoveries

- `test_screenshot_of_frozen_page_times_out_and_leaves_the_bridge_usable` (live) already fails on
  main: opening another thread after a frozen screenshot hangs. Unchanged by this work.

## Outcomes & Retrospective

Calls on different Surf threads now overlap: live, a second agent's `open` returned in 0.08 s while
another agent's 4 s call ran, where it previously waited it out. The lasting decisions are in
`docs/adr/0010-serve-the-bridge-concurrently.md`. Lesson: the advisor's cancel-on-disconnect idea
looked like robustness but made every browser-wide operation responsible for cancellation safety;
the review caught it before shipping. Untested branch: exiting when the stop itself fails.
