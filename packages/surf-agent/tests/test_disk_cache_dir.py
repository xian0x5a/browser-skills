"""Which Chrome profiles keep their HTTP cache outside the profile, and where."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from surf_agent.runtime import APP_DIRS, SurfAgent

CACHE_FLAG = "--disk-cache-dir"
DISABLE_FEATURES_FLAG = "--disable-features="


def spawn_commands(agent: SurfAgent) -> tuple[list[str], list[str]]:
    """(bridge spawn command, Chrome open_profile argv) of an agent whose launches are stubbed."""
    commands: list[list[str]] = []
    record_launch = patch(
        "surf_agent.backends.local_bridge.subprocess.Popen",
        side_effect=lambda command, **_: commands.append(command) or object(),
    )
    with (
        record_launch,
        patch("surf_agent.backends.local_bridge.time.sleep", return_value=None),
    ):
        # The bridge reports healthy once it has been spawned.
        with patch.object(agent.patchright_client, "_health_ok", side_effect=lambda: bool(commands)):
            agent.patchright_client._ensure_running()
        with patch.object(agent.patchright_client, "_health_ok", return_value=False):
            agent.profile_open("https://x.test")
    bridge_command, chrome_argv = commands
    return bridge_command, chrome_argv


def bridge_cache_dir(bridge_command: list[str]) -> str | None:
    return bridge_command[bridge_command.index(CACHE_FLAG) + 1] if CACHE_FLAG in bridge_command else None


def chrome_cache_dir(chrome_argv: list[str]) -> str | None:
    values = [arg.removeprefix(CACHE_FLAG + "=") for arg in chrome_argv if arg.startswith(CACHE_FLAG + "=")]
    return values[0] if values else None


@pytest.mark.parametrize(
    ("environment", "expected_cache_dir"),
    [
        ("home", "{home}/cache/profiles/chrome"),
        ("nothing", "{user_cache}/profiles/chrome"),
        ("profile dir", None),
    ],
)
def test_only_surfs_own_profile_gets_a_cache_dir(
    environment, expected_cache_dir, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    # The launches are stubbed, but Surf still creates the profile directory.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    for variable in ("SURF_AGENT_HOME", "SURF_AGENT_PROFILE_DIR"):
        monkeypatch.delenv(variable, raising=False)
    if environment == "home":
        monkeypatch.setenv("SURF_AGENT_HOME", str(home))
    elif environment == "profile dir":
        monkeypatch.setenv("SURF_AGENT_HOME", str(home))
        monkeypatch.setenv("SURF_AGENT_PROFILE_DIR", str(tmp_path / "custom-profile"))
    expected = (
        expected_cache_dir.format(home=home, user_cache=APP_DIRS.user_cache_dir)
        if expected_cache_dir
        else None
    )

    agent = SurfAgent(chrome_bin="chrome", command_timeout_s=1)
    bridge_command, chrome_argv = spawn_commands(agent)

    assert bridge_cache_dir(bridge_command) == expected, bridge_command
    assert chrome_cache_dir(chrome_argv) == expected, chrome_argv
    # The on-device model stays disabled whatever the profile.
    assert f"{DISABLE_FEATURES_FLAG}OptimizationGuideModelExecution" in chrome_argv
