from __future__ import annotations

import time
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from runtime_driver import running_bridge
from surf_agent.backends.base import WaitConditions
from surf_agent.backends.bridge_common import BridgeCodedError
from surf_agent.backends.local_bridge import LocalBridgeBackend, LocalBridgeClient
from surf_agent.errors import ErrorCode, SurfAgentError
from surf_agent.thread import Thread


class StubRuntime:
    def __init__(self, behavior) -> None:
        self.behavior = behavior

    def health_payload(self) -> dict[str, object]:
        return {"status": "ok"}

    async def call_async(self, name: str, args: dict[str, object]) -> str:
        return self.behavior(name, args)

    async def finish_response(self, _name: str) -> bool:
        return False

    async def stop_async(self) -> str:
        return "stopped\n"


@pytest.fixture
def bridge_client(tmp_path: Path):
    bridges = ExitStack()

    def start(behavior, *, timeout_s: float = 5.0) -> LocalBridgeClient:
        _server, port, _thread = bridges.enter_context(running_bridge(StubRuntime(behavior)))
        return LocalBridgeClient(
            backend_label="Stub",
            module_name="unused",
            timeout_s=timeout_s,
            port=port,
            profile_dir=tmp_path,
            startup_error="unused",
        )

    with bridges:
        yield start


def test_bridge_error_code_reaches_the_caller(bridge_client) -> None:
    def fail(_name, _args):
        raise BridgeCodedError(ErrorCode.NOT_VISIBLE, "target '#x' is not visible")

    client = bridge_client(fail)

    with pytest.raises(SurfAgentError) as raised:
        client.call_tool("click", {"uid": "#x"})

    assert raised.value.code == ErrorCode.NOT_VISIBLE
    assert "not visible" in str(raised.value)


def test_uncoded_bridge_failure_has_no_code(bridge_client) -> None:
    def fail(_name, _args):
        raise RuntimeError("boom")

    with pytest.raises(SurfAgentError) as raised:
        bridge_client(fail).call_tool("click", {})

    assert raised.value.code is None


def test_transport_timeout_reports_unknown_outcome(bridge_client) -> None:
    def slow(_name, _args):
        time.sleep(0.5)
        return "clicked\n"

    with pytest.raises(SurfAgentError) as raised:
        bridge_client(slow, timeout_s=0.1).call_tool("click", {})

    assert raised.value.code == ErrorCode.OUTCOME_UNKNOWN


def test_wait_transport_timeout_covers_the_wait_duration(bridge_client) -> None:
    def slow_wait(_name, _args):
        time.sleep(0.3)
        return "waited\n"

    agent = SimpleNamespace(thread="research")
    backend = LocalBridgeBackend(agent, client=bridge_client(slow_wait, timeout_s=0.2), welcome_url=lambda: "about:blank")
    backend.client_attr = "unused"

    assert backend.wait_for(WaitConditions(text="Saved", timeout_ms=1_000)) == "waited\n"


def test_sleep_transport_timeout_covers_the_sleep_duration(bridge_client) -> None:
    def slow_sleep(_name, _args):
        time.sleep(0.3)
        return "waited\n"

    agent = SimpleNamespace(thread="research")
    backend = LocalBridgeBackend(agent, client=bridge_client(slow_sleep, timeout_s=0.2), welcome_url=lambda: "about:blank")
    backend.client_attr = "unused"

    assert backend.sleep_ms(300) == "waited\n"


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(self, name: str, args: dict[str, object], **_kwargs: object) -> str:
        self.calls.append((name, args))
        return f"{name} ok\n"


@pytest.fixture
def local_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = RecordingClient()
    agent = SimpleNamespace(thread="research", stub_client=client)
    backend = LocalBridgeBackend(agent, client=client, welcome_url=lambda: "about:blank")
    backend.client_attr = "stub_client"
    backend.display_name = "Stub"
    agent.browser_backend = backend
    monkeypatch.setattr("surf_agent.thread._create_agent", lambda _name: agent)
    return Thread("research"), client


def test_wait_conditions_reach_the_bridge(local_thread) -> None:
    thread, client = local_thread

    thread.wait("Saved", gone="Loading…", url="*/done*", timeout_ms=2_000)
    thread.wait("Saved")

    assert client.calls == [
        ("wait-for", {"thread": "research", "text": "Saved", "gone": "Loading…", "url": "*/done*", "timeoutMs": 2_000}),
        ("wait-for", {"thread": "research", "text": "Saved", "gone": None, "url": None, "timeoutMs": None}),
    ]


def test_sleep_reaches_the_bridge(local_thread) -> None:
    thread, client = local_thread

    thread.sleep(250)

    assert client.calls == [("sleep", {"thread": "research", "milliseconds": 250})]


@pytest.mark.parametrize(("milliseconds", "error"), [(-1, ValueError), (True, TypeError), ("250", TypeError)])
def test_sleep_rejects_non_durations(local_thread, milliseconds, error) -> None:
    thread, client = local_thread

    with pytest.raises(error):
        thread.sleep(milliseconds)

    assert client.calls == []


@pytest.mark.parametrize(
    ("args", "kwargs", "error"),
    [
        ((), {}, ValueError),
        ((250,), {}, TypeError),
        (("",), {}, ValueError),
        ((), {"gone": ""}, ValueError),
        (("x",), {"timeout_ms": 0}, ValueError),
        (("x",), {"timeout_ms": True}, TypeError),
    ],
)
def test_wait_rejects_ambiguous_or_empty_conditions(local_thread, args, kwargs, error) -> None:
    thread, client = local_thread

    with pytest.raises(error):
        thread.wait(*args, **kwargs)

    assert client.calls == []


def test_text_target_reaches_the_bridge(local_thread) -> None:
    thread, client = local_thread

    thread.text()
    thread.text("@e5")

    assert client.calls == [
        ("text", {"thread": "research"}),
        ("text", {"thread": "research", "target": "@e5"}),
    ]
