---
status: open
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
Idle shutdown and restart set `server.should_exit`; lifespan shutdown stops the browser before
uvicorn drains requests, with a graceful-shutdown timeout so a wedged call cannot block exit.
A call whose client disconnects is cancelled, so its thread lock is released.

## Decisions

- uvicorn + Starlette over a threaded stdlib front end with a loop thread (weighted 3.95 vs 3.65,
  advisor review): one loop in one thread by construction, standard shutdown, cancel-on-disconnect.
  FastAPI rejected: its validation and OpenAPI layers add Pydantic for two internal endpoints.
- Readers-writer lock rejected (advisor): asyncio has none in the stdlib, and an exclusive stop
  queued behind a long or wedged call would freeze every new call. Stop/restart are terminal, so
  a one-way `stopping` state covers them.
- Hand-written asyncio HTTP rejected: reimplements HTTP parsing.

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
7. A client disconnect mid-call releases the thread for the next call.
8. Stop during an in-flight call returns promptly and the process exits.

Live: scratchpad repro with two clients on an isolated `SURF_AGENT_HOME` and port, headless where
possible; never live Google.

## Progress

- [ ] Layer 1 tests red.
- [ ] Layer 1 implemented; full suite green.
- [ ] Layer 2 tests red.
- [ ] Layer 2 implemented; full suite and live repro green.
- [ ] Independent review; runtime pin; docs.

## Surprises & Discoveries

## Outcomes & Retrospective
