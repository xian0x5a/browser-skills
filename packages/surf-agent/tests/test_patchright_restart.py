from __future__ import annotations

from contextlib import ExitStack, contextmanager
import asyncio
import threading
from pathlib import Path

from runtime_driver import running_bridge
from surf_agent.backends.bridge_common import bridge_health_payload
from surf_agent.backends.patchright.backend import PatchrightBridgeClient
from surf_agent.backends.patchright.constants import CONTEXT_RESTART_REQUIRED
from surf_agent.constants import PATCHRIGHT_BACKEND


class IdentityRuntime:
    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir

    def health_payload(self) -> dict[str, object]:
        return bridge_health_payload(PATCHRIGHT_BACKEND, self.profile_dir)

    async def finish_response(self, _name: str) -> bool:
        return False

    async def stop_async(self) -> str:
        return "stopped\n"


class RestartRequiredRuntime(IdentityRuntime):
    async def call_async(self, _name: str, _args: dict[str, object]) -> str:
        raise RuntimeError(CONTEXT_RESTART_REQUIRED)

    async def finish_response(self, _name: str) -> bool:
        # The bridge process exits so the client relaunches it.
        return True


class HealthyRuntime(IdentityRuntime):
    async def call_async(self, name: str, args: dict[str, object]) -> str:
        assert name == "open"
        return f"opened {args['url']}\n"


def test_patchright_client_restarts_bridge_and_retries_interrupted_request(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    with ExitStack() as bridges:
        _first_server, first_port, _first_thread = bridges.enter_context(
            running_bridge(RestartRequiredRuntime(profile_dir))
        )
        client = PatchrightBridgeClient(timeout_s=1, port=first_port, profile_dir=profile_dir)

        @contextmanager
        def start_replacement_bridge():
            _server, port, _thread = bridges.enter_context(running_bridge(HealthyRuntime(profile_dir)))
            client.port = port
            yield

        client.before_start = start_replacement_bridge

        assert client.call_tool("open", {"thread": "default", "url": "https://reddit.com"}) == "opened https://reddit.com\n"


class SlowRuntime(HealthyRuntime):
    """Blocks `sleep` longer than the client's health probe timeout."""

    def __init__(self, profile_dir: Path) -> None:
        super().__init__(profile_dir)
        self.sleep_started = threading.Event()

    async def call_async(self, name: str, args: dict[str, object]) -> str:
        if name == "sleep":
            self.sleep_started.set()
            await asyncio.sleep(1.5)
            return "slept\n"
        return await super().call_async(name, args)


def test_busy_bridge_is_treated_as_running_not_relaunched(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    runtime = SlowRuntime(profile_dir)
    with running_bridge(runtime) as (_server, port, _thread):
        busy_client = PatchrightBridgeClient(timeout_s=5, port=port, profile_dir=profile_dir)
        waiting_client = PatchrightBridgeClient(timeout_s=5, port=port, profile_dir=profile_dir)
        launch_path_entered: list[bool] = []

        @contextmanager
        def record_launch_attempt():
            launch_path_entered.append(True)
            yield

        waiting_client.before_start = record_launch_attempt
        busy_call = threading.Thread(target=busy_client.call_tool, args=("sleep", {"thread": "default"}), daemon=True)

        busy_call.start()
        assert runtime.sleep_started.wait(timeout=3)
        result = waiting_client.call_tool("open", {"thread": "default", "url": "https://example.com"})
        assert launch_path_entered == []
        assert result == "opened https://example.com\n"
        busy_call.join(timeout=5)
