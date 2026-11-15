from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from pathlib import Path

from surf_agent.backends.bridge_common import bridge_health_payload
from surf_agent.backends.patchright.backend import PatchrightBridgeClient
from surf_agent.backends.patchright.bridge import PatchrightHTTPServer, RequestHandler
from surf_agent.backends.patchright.constants import CONTEXT_RESTART_REQUIRED
from surf_agent.constants import PATCHRIGHT_BACKEND


class IdentityRuntime:
    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir

    def health_payload(self) -> dict[str, object]:
        return bridge_health_payload(PATCHRIGHT_BACKEND, self.profile_dir)


class RestartRequiredRuntime(IdentityRuntime):
    restart_requested = False

    def call(self, _name: str, _args: dict[str, object]) -> str:
        self.restart_requested = True
        raise RuntimeError(CONTEXT_RESTART_REQUIRED)

    def service_actions(self) -> bool:
        return False

    def stop(self) -> str:
        return "stopped\n"


class HealthyRuntime(IdentityRuntime):
    restart_requested = False

    def call(self, name: str, args: dict[str, object]) -> str:
        assert name == "open"
        return f"opened {args['url']}\n"

    def after_response(self, _name: str) -> None:
        return

    def service_actions(self) -> bool:
        return False

    def stop(self) -> str:
        return "stopped\n"


def serve(server: PatchrightHTTPServer) -> None:
    try:
        server.serve_forever()
    finally:
        server.server_close()


def test_patchright_client_restarts_bridge_and_retries_interrupted_request(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    first_runtime = RestartRequiredRuntime(profile_dir)
    RequestHandler.runtime = first_runtime
    first_server = PatchrightHTTPServer(("127.0.0.1", 0), RequestHandler)
    first_thread = threading.Thread(target=serve, args=(first_server,), daemon=True)
    first_thread.start()

    client = PatchrightBridgeClient(
        timeout_s=1,
        port=first_server.server_address[1],
        profile_dir=profile_dir,
    )
    started_servers: list[tuple[PatchrightHTTPServer, threading.Thread]] = []

    @contextmanager
    def start_replacement_bridge():
        replacement_runtime = HealthyRuntime(profile_dir)
        RequestHandler.runtime = replacement_runtime
        replacement_server = PatchrightHTTPServer(("127.0.0.1", 0), RequestHandler)
        replacement_thread = threading.Thread(target=serve, args=(replacement_server,), daemon=True)
        replacement_thread.start()
        started_servers.append((replacement_server, replacement_thread))
        client.port = replacement_server.server_address[1]
        yield

    client.before_start = start_replacement_bridge

    try:
        assert client.call_tool("open", {"thread": "default", "url": "https://reddit.com"}) == "opened https://reddit.com\n"
    finally:
        first_server.shutdown()
        first_thread.join(timeout=1)
        for server, thread in started_servers:
            server.shutdown()
            thread.join(timeout=1)


class SlowRuntime(HealthyRuntime):
    """Blocks `sleep` longer than the client's health probe timeout."""

    def __init__(self, profile_dir: Path) -> None:
        super().__init__(profile_dir)
        self.sleep_started = threading.Event()

    def call(self, name: str, args: dict[str, object]) -> str:
        if name == "sleep":
            self.sleep_started.set()
            time.sleep(1.5)
            return "slept\n"
        return super().call(name, args)


def test_busy_bridge_is_treated_as_running_not_relaunched(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    runtime = SlowRuntime(profile_dir)
    RequestHandler.runtime = runtime
    server = PatchrightHTTPServer(("127.0.0.1", 0), RequestHandler)
    server_thread = threading.Thread(target=serve, args=(server,), daemon=True)
    server_thread.start()
    port = server.server_address[1]

    busy_client = PatchrightBridgeClient(timeout_s=5, port=port, profile_dir=profile_dir)
    waiting_client = PatchrightBridgeClient(timeout_s=5, port=port, profile_dir=profile_dir)
    launch_path_entered: list[bool] = []

    @contextmanager
    def record_launch_attempt():
        launch_path_entered.append(True)
        yield

    waiting_client.before_start = record_launch_attempt
    busy_call = threading.Thread(target=busy_client.call_tool, args=("sleep", {"thread": "default"}), daemon=True)

    try:
        busy_call.start()
        assert runtime.sleep_started.wait(timeout=3)
        result = waiting_client.call_tool("open", {"thread": "default", "url": "https://example.com"})
        assert launch_path_entered == []
        assert result == "opened https://example.com\n"
    finally:
        busy_call.join(timeout=5)
        server.shutdown()
        server_thread.join(timeout=2)
