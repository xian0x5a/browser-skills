"""Patchright's own Chrome flags, read from the installed Patchright rather than copied into Surf."""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any

# Patchright appends this as the startup page of every persistent launch.
STARTUP_PAGE_ARG = "about:blank"
DISABLE_FEATURES_PREFIX = "--disable-features="
# Gates Chrome's on-device model (Gemini Nano), which it otherwise downloads into the
# profile at about 4 GB. Surf uses no Chrome AI feature.
ON_DEVICE_MODEL_FEATURE = "OptimizationGuideModelExecution"
# The stub exits at once; a slow answer means Patchright never started it.
CAPTURE_TIMEOUT_MS = 10_000


def keep_disposable_data_out_of_profile(args: list[str], *, disk_cache_dir: Path | None) -> list[str]:
    """Chrome flags that stop the on-device model download and, given a directory, move the HTTP cache there.

    Chrome keeps the HTTP cache and Code Cache under <disk_cache_dir>/<profile name>/.
    Service Worker storage stays in the profile: it is site data, not a cache.
    """
    # Chrome reads only the last --disable-features, so extend Patchright's own one.
    has_disable_features = any(arg.startswith(DISABLE_FEATURES_PREFIX) for arg in args)
    merged = [
        f"{arg},{ON_DEVICE_MODEL_FEATURE}" if arg.startswith(DISABLE_FEATURES_PREFIX) else arg
        for arg in args
    ]
    if not has_disable_features:
        merged.append(DISABLE_FEATURES_PREFIX + ON_DEVICE_MODEL_FEATURE)
    if disk_cache_dir is not None:
        merged.append(f"--disk-cache-dir={disk_cache_dir}")
    return merged


async def capture_default_args(chromium: Any, **launch_options: Any) -> list[str]:
    """The flags Patchright would pass to Chrome for these launch options.

    Patchright launches a stub instead of Chrome. The stub records its argv and
    exits, so no browser starts and nothing takes focus. A launch with
    ignore_default_args=True needs this full list, and reading it here keeps Surf
    in step with whatever Patchright version is installed.
    """
    with tempfile.TemporaryDirectory(prefix="surf-patchright-args-") as directory:
        recorded = Path(directory) / "argv"
        stub = Path(directory) / "chrome"
        # /bin/sh rather than a Python shebang: an interpreter path containing spaces
        # breaks a shebang line. NUL separators keep every argument intact.
        stub.write_text(
            "#!/bin/sh\n"
            f"for arg in \"$@\"; do printf '%s\\0' \"$arg\"; done > {shlex.quote(str(recorded))}\n"
        )
        stub.chmod(0o700)
        failure: Exception | None = None
        try:
            await chromium.launch_persistent_context(
                executable_path=str(stub), timeout=CAPTURE_TIMEOUT_MS, **launch_options
            )
        except Exception as exc:  # expected: the stub exits instead of answering Patchright
            failure = exc
        if not recorded.exists():
            raise RuntimeError("could not read Patchright's Chrome flags: its launch never ran the executable") from failure
        return recorded.read_bytes().decode().split("\0")[:-1]
