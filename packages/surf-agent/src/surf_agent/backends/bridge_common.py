from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ErrorCode

STALE_REF_MESSAGE = "Ref {ref!r} not found in the current page snapshot. Capture a new snapshot."
CLOSED_TARGET_MESSAGE = "Target page, context or browser has been closed"
SNAPSHOT_DEPTH: int | None = None
SNAPSHOT_BOXES = False
# Playwright's aria-ref selector accepts AI snapshot refs from main frames and iframes.
NATIVE_ARIA_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")


class BridgeCodedError(RuntimeError):
    """Bridge failure whose category crosses the HTTP boundary as ``code``."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PageSlot:
    page: Any
    page_id: int
    # The page's CDP target, which Chrome names as the opener of the page's popups.
    target_id: str
    # Pages the thread's page opened, and the ones those opened in turn.
    popups: list[Any] = field(default_factory=list)



def bridge_health_payload(backend: str, profile_dir: Path) -> dict[str, object]:
    """Affirm one normalized profile without exposing its machine-specific path."""
    normalized_profile = str(profile_dir.expanduser().resolve())
    return {
        "status": "ok",
        "backend": backend,
        "profile_identity": hashlib.sha256(normalized_profile.encode()).hexdigest(),
    }
