"""What a caller sees when an uncaught error ends a script or a session cell.

Both run modes must print the same shape: a Surf error shows only the caller's
frames and `ExceptionName [code]: message`; any other error keeps its full
traceback, Surf frames included.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread as ServerThread

import pytest

import surf_agent
from surf_agent.backends.bridge_common import (
    BridgeCodedError,
    BridgeRequestHandler,
    bridge_health_payload,
)
from surf_agent.constants import PATCHRIGHT_BACKEND
from surf_agent.errors import ErrorCode
from surf_agent.processes import iter_process_args

SUBPROCESS_TIMEOUT_S = 60
SURF_PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(surf_agent.__file__)) + os.sep
FRAME_LINE = re.compile(r'^\s*File "(?P<file>[^"]+)", line \d+, in (?P<function>\S+)', re.MULTILINE)
BRIDGE_FAILURE = "timed out waiting for #never"


class AlwaysTimingOutBridge:
    """A healthy bridge for the configured profile whose every tool call times out."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir

    def health_payload(self) -> dict[str, object]:
        return bridge_health_payload(PATCHRIGHT_BACKEND, self.profile_dir)

    def call(self, name: str, args: dict[str, object]) -> str:
        raise BridgeCodedError(ErrorCode.WAIT_TIMEOUT, BRIDGE_FAILURE)


@pytest.fixture
def surf_environment(short_tmp_path: Path):
    """Environment for a child interpreter whose Surf calls reach a stub bridge, not Chrome."""
    profile_dir = short_tmp_path / "profile"
    runtime_dir = short_tmp_path / "runtime"
    handler = type(
        "Handler", (BridgeRequestHandler,), {"runtime": AlwaysTimingOutBridge(profile_dir)}
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    server_thread = ServerThread(target=server.serve_forever, daemon=True)
    server_thread.start()
    environment = dict(os.environ)
    environment.pop("SURF_SESSION_WORKER_COMMAND", None)
    environment.update(
        SURF_AGENT_PORT=str(server.server_port),
        SURF_AGENT_HOME=str(short_tmp_path / "home"),
        SURF_AGENT_PROFILE_DIR=str(profile_dir),
        XDG_RUNTIME_DIR=str(runtime_dir),
    )
    try:
        yield environment
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        # A session that was never killed (a test failing midway) must not outlive the test.
        for pid, arguments in iter_process_args():
            command = " ".join(arguments)
            if str(runtime_dir) in command and "surf_agent.session" in command:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


def run_fresh_script(
    code: str, environment: dict[str, str], directory: Path, *, expect_failure: bool = True
) -> tuple[str, str]:
    """Run code as an ordinary script; returns (caller file name, stderr)."""
    script = directory / "caller_script.py"
    script.write_text(code)
    result = subprocess.run(
        [sys.executable, str(script)],
        env=environment, cwd=directory, capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert (result.returncode != 0) == expect_failure, result.stderr
    return str(script), result.stderr


def run_session_cell(
    code: str, environment: dict[str, str], directory: Path, *, expect_failure: bool = True
) -> tuple[str, str]:
    """Run code as the first cell of a new session, then kill it; returns ("<cell>", stderr)."""
    def session_request(request: dict, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "surf_agent.session", "run", json.dumps(request)],
            env=environment, cwd=directory, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_S, **kwargs,
        )

    result = session_request({"op": "cell", "mode": "new", "argv": ["-"], "ttl": 60}, input=code)
    session_id = re.search(r"session_id: (\S+)", result.stdout)
    assert session_id, result.stdout + result.stderr
    killed = session_request({"op": "kill", "session": session_id.group(1)})
    assert killed.returncode == 0, killed.stderr
    assert (result.returncode != 0) == expect_failure, result.stderr
    return "<cell>", result.stderr


RUN_MODES = {"fresh script": run_fresh_script, "session cell": run_session_cell}


def frames(output: str) -> list[tuple[str, str]]:
    return [(match["file"], match["function"]) for match in FRAME_LINE.finditer(output)]


def is_surf_frame(file: str) -> bool:
    return os.path.abspath(file).startswith(SURF_PACKAGE_DIRECTORY)


# A caller helper sits between the caller's top level and Surf, so the caller's
# own stack is more than one frame deep.
CODED_SURF_ERROR = """\
from surf_agent import Thread

def read_title(thread):
    return thread.evaluate("document.title")

read_title(Thread("tracebacks"))
"""

UNCODED_SURF_ERROR = """\
from surf_agent import Thread

def open_thread(name):
    return Thread(name)

open_thread("not a valid name!")
"""

# Surf rejects an empty text target with a plain ValueError raised in its own code.
NON_SURF_ERROR = """\
from surf_agent import Thread

def read_region(thread):
    return thread.text("")

read_region(Thread("tracebacks"))
"""


@pytest.mark.parametrize("run_mode", RUN_MODES.values(), ids=RUN_MODES.keys())
@pytest.mark.parametrize(
    ("code", "helper", "summary_prefix", "message_part"),
    [
        (CODED_SURF_ERROR, "read_title", "BridgeToolError [wait_timeout]: ", BRIDGE_FAILURE),
        (UNCODED_SURF_ERROR, "open_thread", "SurfAgentError: ", "--thread may contain only"),
    ],
    ids=["with code", "without code"],
)
def test_surf_error_prints_only_caller_frames_and_summary(
    run_mode, code, helper, summary_prefix, message_part, surf_environment, short_tmp_path
):
    caller_file, stderr = run_mode(code, surf_environment, short_tmp_path)

    printed_frames = frames(stderr)
    assert {file for file, _ in printed_frames} == {caller_file}, stderr
    assert [function for _, function in printed_frames] == ["<module>", helper], stderr
    summaries = [line for line in stderr.splitlines() if line.startswith(summary_prefix)]
    assert summaries and message_part in summaries[0], stderr


@pytest.mark.parametrize("run_mode", RUN_MODES.values(), ids=RUN_MODES.keys())
def test_other_error_keeps_full_traceback(run_mode, surf_environment, short_tmp_path):
    caller_file, stderr = run_mode(NON_SURF_ERROR, surf_environment, short_tmp_path)

    assert "Traceback (most recent call last):" in stderr, stderr
    printed_frames = frames(stderr)
    assert (caller_file, "<module>") in printed_frames, stderr
    assert (caller_file, "read_region") in printed_frames, stderr
    assert any(is_surf_frame(file) for file, _ in printed_frames), stderr
    assert any(line.startswith("ValueError: ") for line in stderr.splitlines()), stderr


# The caller starts a thread of its own; a Surf error ends that thread, not the script.
THREAD_NAME = "reader-thread"
SURF_ERROR_IN_THREAD = """\
import threading
from surf_agent import Thread

def read_title(thread):
    return thread.evaluate("document.title")

def work():
    read_title(Thread("tracebacks"))

worker = threading.Thread(target=work, name="reader-thread")
worker.start()
worker.join()
"""

NON_SURF_ERROR_IN_THREAD = """\
import threading
from surf_agent import Thread

def read_region(thread):
    return thread.text("")

def work():
    read_region(Thread("tracebacks"))

worker = threading.Thread(target=work, name="reader-thread")
worker.start()
worker.join()
"""


@pytest.mark.parametrize("run_mode", RUN_MODES.values(), ids=RUN_MODES.keys())
def test_surf_error_in_caller_thread_prints_only_caller_frames_and_summary(
    run_mode, surf_environment, short_tmp_path
):
    caller_file, stderr = run_mode(
        SURF_ERROR_IN_THREAD, surf_environment, short_tmp_path, expect_failure=False
    )

    lines = stderr.splitlines()
    assert f"Exception in thread {THREAD_NAME}:" in lines, stderr
    printed_frames = frames(stderr)
    assert {file for file, _ in printed_frames} == {caller_file}, stderr
    assert [function for _, function in printed_frames] == ["work", "read_title"], stderr
    summaries = [line for line in lines if line.startswith("BridgeToolError [wait_timeout]: ")]
    assert summaries and BRIDGE_FAILURE in summaries[0], stderr


@pytest.mark.parametrize("run_mode", RUN_MODES.values(), ids=RUN_MODES.keys())
def test_other_error_in_caller_thread_keeps_full_traceback(run_mode, surf_environment, short_tmp_path):
    caller_file, stderr = run_mode(
        NON_SURF_ERROR_IN_THREAD, surf_environment, short_tmp_path, expect_failure=False
    )

    assert f"Exception in thread {THREAD_NAME}:" in stderr.splitlines(), stderr
    assert "Traceback (most recent call last):" in stderr, stderr
    printed_frames = frames(stderr)
    assert (caller_file, "read_region") in printed_frames, stderr
    assert any(is_surf_frame(file) for file, _ in printed_frames), stderr
    assert any(line.startswith("ValueError: ") for line in stderr.splitlines()), stderr


CELL_ERROR_FRAME = re.compile(r"^--- cell #1 error: (?P<detail>.*) \(\d+ ms\) ---$", re.MULTILINE)


@pytest.mark.parametrize(
    ("code", "detail_prefix", "detail_part"),
    [
        ("x = (\n", "SyntaxError: ", "never closed"),
        (CODED_SURF_ERROR, "BridgeToolError [wait_timeout]: ", BRIDGE_FAILURE),
        ('raise RuntimeError("first line\\nsecond line")\n', "RuntimeError: ", "first line"),
    ],
    ids=["syntax error", "surf error", "multi-line message"],
)
def test_cell_error_frame_names_the_error(
    code, detail_prefix, detail_part, surf_environment, short_tmp_path
):
    """The session's per-cell status line says which error ended the cell, and why."""
    _, stderr = run_session_cell(code, surf_environment, short_tmp_path)

    frame = CELL_ERROR_FRAME.search(stderr)
    assert frame, stderr
    detail = frame["detail"]
    assert detail.startswith(detail_prefix), detail
    assert detail_part in detail, detail
    assert "second line" not in detail, detail
