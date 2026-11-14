"""Chrome's record of the last session, removed before Chrome starts on the Surf profile.

The Surf profile can inherit "Continue where you left off" from the signed-in Google
account's synced settings. Chrome then restores the previous launch's tabs into the first
window it opens: the window of the first thread Surf creates. Patchright does not expose
those restored tabs as pages, so Surf never closes them, and each launch restores the
previous launch's leftovers again. Surf threads never outlive a Chrome launch, so every
restored tab is an orphan; removing the record leaves Chrome nothing to restore.
"""

from __future__ import annotations

from pathlib import Path

from ...constants import CHROME_PROFILE_DIRECTORY

# Chrome stores the session as Sessions/Session_<timestamp> and recently closed tabs
# as Sessions/Tabs_<timestamp>; only the former is restored at startup.
LAST_SESSION_GLOB = "Session_*"


def forget_last_session(profile_dir: Path) -> None:
    """Remove the profile's session record so Chrome restores no previous tabs.

    Call only while Chrome is not running on the profile: a running Chrome keeps
    writing its current session.
    """
    for session_file in (profile_dir / CHROME_PROFILE_DIRECTORY / "Sessions").glob(LAST_SESSION_GLOB):
        session_file.unlink()
