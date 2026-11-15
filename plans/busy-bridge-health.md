---
status: done
---

## Goal

Parallel agents sharing one Surf bridge must not misread a busy bridge as stopped.

## Intention

A research run (2026-11-14) had four agents on one bridge. One agent's call failed with
`destination Surf browser profile is active; cannot refresh cookies before startup`, so it
started a private Surf with a custom app-id, whose window escaped the niri rule and stole focus.

## Root cause

The Patchright bridge is a single-threaded `HTTPServer` by design (Patchright objects are bound to
one thread). While it serves agent A's call, agent B's `GET /health` waits in the accept queue.
`LocalBridgeClient._health_payload` gives up after 1 s and returns `None`, which every caller reads
as "bridge not running". `_ensure_running` then enters the launch guard; with cookie import
configured, the lifecycle sees Chrome alive on the profile and raises. Without cookie import it
spawns a duplicate bridge that cannot bind. `profile_open` has the same misread and could start a
manual Chrome on an automated profile.

## Fix

`_health_payload`: refused or no listener -> `None` (stopped). Connection accepted but no reply
within the command timeout -> raise `BridgeUnavailable` saying the bridge is busy or wedged.
Otherwise wait for the reply, since a single-threaded bridge answers health after its in-flight call.

## Decisions

- Rejected: concurrent async bridge. Real parallelism, but rewrites the serialized runtime's state
  handling (threads, window sweep, idle stop, restart); larger risk than the observed bug needs.
- Rejected: threaded health endpoint plus browser worker thread. Instant health, but adds thread
  hand-off for every call and still queues calls; more moving parts for the same user outcome.
- Follow-up, out of scope: queued calls still count against the caller's command timeout.

## Validation

- Regression test (independent agent): real `PatchrightHTTPServer`, fake runtime whose call blocks
  longer than 1 s; a second client's `call_tool` must succeed without entering the launch path.
- Live loop: scratchpad `repro_concurrent_health.py` against an isolated `SURF_AGENT_HOME` and port.
  Red before the fix (3/3 runs).

## Progress

- [x] Repro red, deterministic.
- [x] Regression test red (independent agent), then green.
- [x] Fix; full suite (248 passed) and live repro (2/2) green.
- [x] Independent review; its finding fixed (below); runtime pinned; docs updated.

## Surprises & Discoveries

- Review: the busy probe raising aborted `stop_bridge()` before its process kill, the documented
  recovery for a wedged bridge. `LocalBridgeClient.stop()` now treats busy as a skipped graceful stop.
- Probes during bridge shutdown now wait for the process to exit instead of guessing after 1 s.

## Outcomes & Retrospective

Agents sharing the bridge now wait their turn instead of failing or racing a second launch. The
behavior is documented in `skills/browser/docs/patchright-backend.md`. Calls still serialize, so a
long call delays others and can push them past their command timeout: tracked as a follow-up.
