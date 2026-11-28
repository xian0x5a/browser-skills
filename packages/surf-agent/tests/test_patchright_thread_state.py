"""Live contract: `list` and `state` stay truthful about threads whose pages misbehave (issues #72, #74).

Local pages only; never loads a remote site.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from test_patchright_screenshot_zoom import (  # noqa: F401
    FREEZE_SETTLE_S,
    UNFROZEN_URL,
    fails_if_still_running_after,
    launch,
    page_value,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

# Surf's own action timeout is 5s; a read that never answers must not exceed it by much.
STATE_READ_DEADLINE_S = 5.0
FREEZE_AFTER_MS = 50


def test_list_and_state_answer_while_a_threads_page_is_frozen(launch):  # noqa: F811
    runtime = launch()
    runtime.call("open", {"thread": "healthy", "url": UNFROZEN_URL})
    runtime.call("open", {"thread": "frozen", "url": "data:text/html,<title>Doomed</title>"})
    # eval returns before the timer fires, so the call itself does not hang.
    runtime.call("eval", {"thread": "frozen", "code": f"() => void setTimeout(() => {{ while (true) {{}} }}, {FREEZE_AFTER_MS})"})
    time.sleep(FREEZE_SETTLE_S)

    try:
        with fails_if_still_running_after(STATE_READ_DEADLINE_S, "list while a page is frozen"):
            listed = json.loads(runtime.call("list", {}))
        titles = {row["thread"]: row["title"] for row in listed["pages"]}
        assert titles == {"healthy": "Unfrozen", "frozen": ""}

        with fails_if_still_running_after(STATE_READ_DEADLINE_S, "state of a frozen page"):
            state = json.loads(runtime.call("state", {"thread": "frozen"}))
        assert state["open"] is True
    finally:
        # The spinning renderer never answers a graceful close; closing the page drops the target.
        runtime.call("close-matching", {"pattern": "frozen"})


def test_state_reports_a_thread_closed_outside_surf_as_not_open(launch):  # noqa: F811
    runtime = launch()
    runtime.call("open", {"thread": "kept", "url": UNFROZEN_URL})
    runtime.call("open", {"thread": "closed-by-human", "url": UNFROZEN_URL})
    assert json.loads(runtime.call("state", {"thread": "closed-by-human"}))["open"] is True

    # What a human closing the window does: the page goes away without a Surf command.
    runtime._run(runtime.pages["closed-by-human"].page.close())

    with fails_if_still_running_after(STATE_READ_DEADLINE_S, "state of a closed page"):
        state = json.loads(runtime.call("state", {"thread": "closed-by-human"}))
    assert state["open"] is False
    assert json.loads(runtime.call("state", {"thread": "kept"}))["open"] is True
