from __future__ import annotations

from enum import StrEnum
import itertools
import os
import sys
import threading
import traceback
from types import TracebackType

PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(__file__)) + os.sep
THREADING_MODULE_FILE = os.path.abspath(threading.__file__)


class ErrorCode(StrEnum):
    """Stable failure categories; callers branch on these, never on message text."""

    STALE_REF = "stale_ref"
    NOT_FOUND = "not_found"
    NOT_VISIBLE = "not_visible"
    NOT_ENABLED = "not_enabled"
    NOT_EDITABLE = "not_editable"
    INTERCEPTED = "intercepted"
    ACTION_TIMEOUT = "action_timeout"
    WAIT_TIMEOUT = "wait_timeout"
    PAGE_CLOSED = "page_closed"
    PAGE_ZOOMED = "page_zoomed"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    OUTCOME_UNKNOWN = "outcome_unknown"


class SurfAgentError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code


class BridgeUnavailable(SurfAgentError):
    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.BRIDGE_UNAVAILABLE) -> None:
        super().__init__(message, code=code)


class BridgeIdentityUnproven(SurfAgentError):
    pass


class BridgeToolError(SurfAgentError):
    def __init__(self, *, backend_label: str, tool_name: str, detail: str, code: ErrorCode | None = None) -> None:
        self.detail = detail
        super().__init__(f"{backend_label} bridge tool {tool_name} failed: {detail}", code=code)


def _is_surf_frame(frame: traceback.FrameSummary) -> bool:
    # Placeholder names such as <cell> or <stdin> are caller code; resolving them
    # as paths would match the package when the working directory is inside it.
    if frame.filename.startswith("<"):
        return False
    return os.path.abspath(frame.filename).startswith(PACKAGE_DIRECTORY)


def _is_python_machinery_frame(frame: traceback.FrameSummary) -> bool:
    # Python's own runpy and importlib machinery, e.g. under `python -m`, and the
    # frames threading.Thread adds above a thread's target.
    return frame.filename.startswith("<frozen ") or os.path.abspath(frame.filename) == THREADING_MODULE_FILE


def surf_error_summary(error: SurfAgentError) -> str:
    """`ExceptionName [code]: message`, then one `Caused by:` line per wrapped error.

    A Surf error that wraps an OS or database failure often leaves that failure
    out of its own message, and the trimmed output has no other place to show it.
    """
    code = f" [{error.code}]" if error.code else ""
    lines = [f"{type(error).__name__}{code}: {error}\n"]
    cause = error.__cause__
    seen = {id(error)}
    # A chain can loop back on itself; the traceback module guards the same way.
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        lines.append("Caused by: " + "".join(traceback.format_exception_only(cause)))
        cause = cause.__cause__
    return "".join(lines)


def format_surf_error(error: SurfAgentError, trace: TracebackType | None) -> str:
    """The caller's frames and the error's summary.

    Surf's own frames are the same on every failure, so they only cost the caller
    tokens. The caller's frames are the run outside this package that ends at the
    first call into it; leading Surf frames are the session runner executing a cell.
    """
    frames = [frame for frame in traceback.extract_tb(trace) if not _is_python_machinery_frame(frame)]
    caller_frames = itertools.takewhile(
        lambda frame: not _is_surf_frame(frame),
        itertools.dropwhile(_is_surf_frame, frames),
    )
    return "".join(traceback.format_list(list(caller_frames))) + surf_error_summary(error)


def install_brief_surf_tracebacks() -> None:
    """Print uncaught Surf errors briefly in a script or a thread it starts; other errors keep the full traceback."""
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(
        error_type: type[BaseException], error: BaseException, trace: TracebackType | None
    ) -> None:
        if isinstance(error, SurfAgentError):
            sys.stderr.write(format_surf_error(error, trace))
        else:
            previous_hook(error_type, error, trace)

    def thread_excepthook(hook_args: threading.ExceptHookArgs) -> None:
        error = hook_args.exc_value
        if isinstance(error, SurfAgentError):
            # Same header as Python's default thread hook, so the caller knows which thread failed.
            thread_name = hook_args.thread.name if hook_args.thread else threading.get_ident()
            sys.stderr.write(
                f"Exception in thread {thread_name}:\n" + format_surf_error(error, hook_args.exc_traceback)
            )
        else:
            previous_thread_hook(hook_args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
