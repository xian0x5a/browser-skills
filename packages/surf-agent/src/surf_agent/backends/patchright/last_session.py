"""Chrome's record of the last session, removed before Chrome starts on the Surf profile.

The Surf profile can inherit "Continue where you left off" from the signed-in Google
account's synced settings. Chrome then restores the previous launch's tabs into the first
window it opens: the window of the first thread Surf creates. Surf closes restored tabs
once that window exists, but by then they may have loaded their pages: visits nobody
asked for, Google searches included, which count toward Google's bot checks. Surf threads
never outlive a Chrome launch, so every restored tab is an orphan; removing the record
leaves Chrome nothing to restore.
"""

from __future__ import annotations

from pathlib import Path

from ...constants import CHROME_PROFILE_DIRECTORY

# Chrome stores the session as Session_<timestamp> and recently closed tabs as
# Tabs_<timestamp>; only the former is restored at startup. It writes both a cleartext
# and an encrypted copy, and which copy it restores from is a field-trial setting that
# can change without a Chrome update, so both copies are removed.
SESSION_DIRECTORIES = ("Sessions", "Sessions_Encrypted")
LAST_SESSION_GLOB = "Session_*"


def forget_last_session(profile_dir: Path) -> None:
    """Remove the profile's session record so Chrome restores no previous tabs.

    Call only while Chrome is not running on the profile: a running Chrome keeps
    writing its current session.
    """
    chrome_profile = profile_dir / CHROME_PROFILE_DIRECTORY
    for directory in SESSION_DIRECTORIES:
        for session_file in (chrome_profile / directory).glob(LAST_SESSION_GLOB):
            session_file.unlink()
