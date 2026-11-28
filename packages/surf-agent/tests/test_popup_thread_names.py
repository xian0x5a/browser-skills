"""Popup thread names and `Thread.popups()` (issue #71); no browser needed."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from surf_agent.backends.patchright.bridge import PatchrightRuntime
from surf_agent.errors import SurfAgentError
from surf_agent.runtime import safe_thread_name
from surf_agent.thread import Thread
from runtime_driver import SyncRuntime


@pytest.mark.parametrize("name", ["app/1", "app/12", "my-app.v2/3"])
def test_popup_thread_names_are_accepted(name: str) -> None:
    assert safe_thread_name(name) == name


@pytest.mark.parametrize("name", ["a/b", "/1", "app/", "app/1/2", "app/x1", "../1", ".a/1"])
def test_other_uses_of_slash_are_rejected(name: str) -> None:
    with pytest.raises(SurfAgentError):
        safe_thread_name(name)


def test_new_refuses_popup_thread_names_before_launching_a_browser(tmp_path) -> None:
    runtime = SyncRuntime(PatchrightRuntime(profile_dir=tmp_path / "profile", headless=True))
    try:
        with pytest.raises(Exception, match="(?i)popup"):
            runtime.call("new", {"thread": "app/1"})
        assert runtime.browser_or_context is None, "new launched a browser for a popup thread name"
    finally:
        runtime.stop()


@dataclass
class ListingBackend:
    names: list[str]

    def list_threads(self) -> list[dict]:
        return [{"thread": name} for name in self.names]


@dataclass
class ListingAgent:
    browser_backend: ListingBackend


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch):
    # Listed as the bridge sorts them (by string), which is not opening order.
    names = ["app", "app/1", "app/10", "app/2", "app2/1", "apple", "other", "other/1"]
    monkeypatch.setattr("surf_agent.thread._create_agent", lambda _name: ListingAgent(ListingBackend(names)))


@pytest.mark.usefixtures("listed")
@pytest.mark.parametrize("handle", ["app", "app/2"])
def test_popups_lists_the_roots_popup_threads_in_opening_order(handle: str) -> None:
    popups = Thread(handle).popups()
    assert [popup.name for popup in popups] == ["app/1", "app/2", "app/10"]
    assert all(isinstance(popup, Thread) for popup in popups)
