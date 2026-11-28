---
status: accepted
---

# Serve the Patchright bridge concurrently on one event loop

Several agents share one bridge per Surf profile, which keeps the user's logins, cookie import and window rules. The bridge was a single-threaded stdlib `HTTPServer`, so one agent's long call (navigation, `wait-for`, `sleep`) blocked every other agent's call, and a waiting agent once misread the silence as "stopped" and started a private browser. The bridge now serves each request as its own task on uvicorn's event loop, the same loop that owns the Patchright objects, so calls on different Surf threads overlap without crossing OS threads.

## Considered options

Weighted on meeting the requirement, simplicity of the end state, long-term fit, migration cost and install footprint:

- **uvicorn + Starlette on the Patchright loop.** Chosen (3.95). One loop in one thread by construction and standard graceful shutdown. It costs two small dependencies and a one-time test migration.
- **Threaded stdlib front end handing coroutines to a loop thread.** Rejected (3.65). It keeps two concurrency models, and shutdown has to be coordinated across threads by hand.
- **Single-threaded server that hands off without waiting.** Not possible: a handler must hold its connection until the response exists, so not waiting means detaching sockets, which amounts to a hand-built async server.
- **FastAPI.** Rejected: its validation and OpenAPI layers add Pydantic for two internal endpoints.

## Consequences

- Shared runtime state is guarded at await points rather than by serialization:
  - A per-Surf-thread lock keeps one thread's calls in order.
  - A browser lock covers launch and window creation, so one creation's sweep of restored tabs never closes another thread's window.
  - Idle stop waits until no call is in flight.
- Stop is one-way and runs once as a shared task. Calls arriving during a stop get `CONTEXT_RESTART_REQUIRED`, and the process exits only after the browser has stopped. The client then relaunches through its cross-process launch guard, which runs the cookie preflight.
- **A call runs to completion even if its client disconnects.** Cancelling abandoned calls was tried and rejected: cancellation mid-launch, mid-window-creation or mid-stop stranded half-made state, and keeping every browser-wide operation cancellation-safe would be a permanent cost. A wedged call blocks only its own thread until `Browser().stop_bridge()`.
- Calls on different threads now share one Chrome. Response capture waits on body reads across all pages, so a slow body on another thread can add latency. Google search pacing does not rely on bridge serialization: it is coordinated across processes in `surf-google-search`.
