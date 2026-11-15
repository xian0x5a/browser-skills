"""Live contract of screenshots on a profile with browser zoom (issue #31).

The Surf profile is a real Chrome profile, so zoom saved in it persists across
sessions. Under zoom a screenshot shows only part of the viewport, or nothing
once the page is scrolled. Surf resets saved zoom when it launches Chrome, and
refuses a screenshot of a page still zoomed when the shot runs.

Checking zoom asks the page's renderer, so a frozen page must not hang the shot.
"""

from __future__ import annotations

import json
import math
import os
import signal
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from surf_agent.backends.patchright import bridge
from runtime_driver import SyncRuntime
from surf_agent.backends.patchright.bridge import PatchrightRuntime

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

# A red marker below the fold of a tall white page: scrolled into view, its
# place in the image shows whether the shot covers the viewport it claims to.
TALL_PAGE = """<title>Tall</title>
<body style="margin:0;background:#fff;height:3000px">
  <div id="marker" style="position:absolute;left:60px;top:1040px;width:60px;height:30px;background:#f00"></div>
</body>"""
SCROLL_Y = 1000

# Chrome saves zoom as a level: factor = 1.2 ** level. "x" is the partition key
# of ordinary browsing, the one pages Surf opens use.
DEFAULT_PARTITION = "x"
ZOOMED_IN_LEVEL = math.log(1.5) / math.log(1.2)

# Chrome's own page-zoom setting. Synthetic key presses do not zoom and CDP has
# no zoom command, so the settings page is how a test zooms the way a human can
# mid-session; changing the default zoom applies to pages already open.
ZOOM_SETTINGS_URL = "chrome://settings/appearance"
ZOOM_SETTING_SELECTOR = "#zoomLevel"
ZOOM_APPLIED_TIMEOUT_S = 5.0

# Loads, then never yields again: its renderer answers no CDP request.
FROZEN_PAGE = """<title>Frozen</title>
<body>Frozen
  <script>addEventListener('load', () => setTimeout(() => { while (true) {} }, 200))</script>
</body>"""
FREEZE_SETTLE_S = 1.0
# Another site, so Chrome renders it in a process the frozen page does not block.
UNFROZEN_URL = "data:text/html,<title>Unfrozen</title>"
# Surf's action timeout is 5s; before the fix Patchright's own 30s applied, and
# one version hung for good. Generous slack, still well short of either.
FROZEN_SCREENSHOT_DEADLINE_S = 15.0


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "tall.html").write_text(TALL_PAGE)
    (root / "frozen.html").write_text(FROZEN_PAGE)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.fixture
def launch(tmp_path):
    """Start a runtime on a fresh profile, optionally holding saved preferences."""
    runtimes: list[PatchrightRuntime] = []

    def start(preferences: dict | None = None) -> PatchrightRuntime:
        profile = tmp_path / "profile"
        if preferences is not None:
            (profile / "Default").mkdir(parents=True)
            (profile / "Default" / "Preferences").write_text(json.dumps(preferences))
        runtime = SyncRuntime(PatchrightRuntime(profile_dir=profile, headless=True))
        runtimes.append(runtime)
        return runtime

    try:
        yield start
    finally:
        for runtime in runtimes:
            runtime.stop()


def page_value(runtime: PatchrightRuntime, code: str, *, thread: str = "default"):
    return json.loads(runtime.call("eval", {"thread": thread, "code": code}))


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def red_box_in_image(runtime: PatchrightRuntime, image_url: str) -> dict:
    """Bounding box of pure-red pixels in the image, decoded by the browser itself.

    right and bottom stay -1 when the image has no red pixel at all.
    """
    return page_value(
        runtime,
        f"""async () => {{
            const image = new Image();
            image.src = {json.dumps(image_url)};
            await image.decode();
            const canvas = document.createElement('canvas');
            canvas.width = image.naturalWidth;
            canvas.height = image.naturalHeight;
            const context = canvas.getContext('2d');
            context.drawImage(image, 0, 0);
            const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
            let left = -1, top = -1, right = -1, bottom = -1;
            for (let y = 0; y < canvas.height; y++) {{
                for (let x = 0; x < canvas.width; x++) {{
                    const i = (y * canvas.width + x) * 4;
                    if (pixels[i] > 240 && pixels[i + 1] < 15 && pixels[i + 2] < 15) {{
                        if (left < 0 || x < left) left = x;
                        if (top < 0) top = y;
                        right = Math.max(right, x); bottom = Math.max(bottom, y);
                    }}
                }}
            }}
            return {{left, top, right, bottom}};
        }}""",
    )


@contextmanager
def fails_if_still_running_after(seconds: float, what: str) -> Iterator[None]:
    """Fail the test, instead of hanging pytest, when the block outlives *seconds*.

    pytest-timeout is not a dependency; SIGALRM interrupts the event loop's wait.
    """

    def hung(_signum, _frame):
        pytest.fail(f"{what} still running after {seconds:g}s", pytrace=False)

    previous = signal.signal(signal.SIGALRM, hung)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def zoom_like_a_human(runtime: PatchrightRuntime, zoom_label: str) -> None:
    """Change Chrome's default page zoom from its settings page, in a thread of its own."""
    runtime.call("open", {"thread": "settings", "url": ZOOM_SETTINGS_URL})
    runtime.call("fill", {"thread": "settings", "uid": ZOOM_SETTING_SELECTOR, "text": zoom_label})


def wait_for_page_zoom(runtime: PatchrightRuntime, thread: str, factor: float) -> float:
    """The page's devicePixelRatio once it reflects *factor* (headless renders at device scale 1)."""
    deadline = time.monotonic() + ZOOM_APPLIED_TIMEOUT_S
    while True:
        ratio = page_value(runtime, "() => devicePixelRatio", thread=thread)
        if ratio == pytest.approx(factor, abs=0.01) or time.monotonic() >= deadline:
            return ratio
        time.sleep(0.05)


@pytest.mark.parametrize(
    "saved_zoom",
    [
        pytest.param({"default_zoom_level": {DEFAULT_PARTITION: ZOOMED_IN_LEVEL}}, id="default-zoom"),
        pytest.param(
            {
                "per_host_zoom_levels": {
                    DEFAULT_PARTITION: {
                        "127.0.0.1": {"zoom_level": ZOOMED_IN_LEVEL, "last_modified": "13400000000000000"}
                    }
                }
            },
            id="site-zoom",
        ),
    ],
)
def test_saved_zoom_is_reset_so_scrolled_screenshot_shows_the_viewport(launch, site, saved_zoom):
    url, root = site
    runtime = launch({"partition": saved_zoom})
    runtime.call("open", {"url": f"{url}/tall.html"})
    viewport = tuple(page_value(runtime, f"() => (scrollTo(0, {SCROLL_Y}), [innerWidth, innerHeight])"))
    marker = page_value(
        runtime,
        """() => {
            const box = document.getElementById('marker').getBoundingClientRect();
            return {left: box.left, top: box.top, right: box.right - 1, bottom: box.bottom - 1};
        }""",
    )
    shot = root / "shot.png"

    runtime.call("screenshot", {"path": str(shot)})

    assert (png_size(shot), red_box_in_image(runtime, f"{url}/shot.png")) == (viewport, marker)


def test_zoom_reset_keeps_other_saved_preferences(launch, site):
    url, _root = site
    # A rendering preference whose effect a page can read: Chrome's default font size.
    runtime = launch({
        "partition": {"default_zoom_level": {DEFAULT_PARTITION: ZOOMED_IN_LEVEL}},
        "webkit": {"webprefs": {"default_font_size": 20}},
    })

    runtime.call("open", {"url": f"{url}/tall.html"})

    assert page_value(runtime, "() => getComputedStyle(document.body).fontSize") == "20px"


# Paired, not crossed: one case per risk. Zooming out must count too (a `zoom > 1`
# check would miss it), and full-page shots must be guarded like viewport ones.
@pytest.mark.parametrize(
    ("zoom_label", "factor", "full_page"),
    [
        pytest.param("150%", 1.5, False, id="zoomed-in-viewport"),
        pytest.param("90%", 0.9, True, id="zoomed-out-full-page"),
    ],
)
def test_screenshot_of_page_zoomed_mid_session_fails_without_writing_an_image(
    launch, site, tmp_path, zoom_label, factor, full_page
):
    url, _root = site
    runtime = launch()
    runtime.call("open", {"thread": "agent", "url": f"{url}/tall.html"})
    zoom_like_a_human(runtime, zoom_label)
    assert wait_for_page_zoom(runtime, "agent", factor) == pytest.approx(factor, abs=0.01)
    shot = tmp_path / "shot.png"

    with pytest.raises(bridge.BridgeCodedError) as raised:
        runtime.call("screenshot", {"thread": "agent", "path": str(shot), "fullPage": full_page})

    # The string, not ErrorCode.PAGE_ZOOMED: fail on behaviour before that member exists.
    assert raised.value.code == "page_zoomed"
    assert not shot.exists()


def test_screenshot_of_frozen_page_times_out_and_leaves_the_bridge_usable(launch, site, tmp_path):
    url, _root = site
    runtime = launch()
    runtime.call("open", {"thread": "frozen", "url": f"{url}/frozen.html"})
    time.sleep(FREEZE_SETTLE_S)
    shot = tmp_path / "shot.png"

    with fails_if_still_running_after(FROZEN_SCREENSHOT_DEADLINE_S, "screenshot of a frozen page"):
        with pytest.raises(bridge.BridgeCodedError) as raised:
            runtime.call("screenshot", {"thread": "frozen", "path": str(shot)})

    assert raised.value.code == "action_timeout"
    with fails_if_still_running_after(FROZEN_SCREENSHOT_DEADLINE_S, "another thread after the frozen screenshot"):
        runtime.call("open", {"thread": "unfrozen", "url": UNFROZEN_URL})
        assert page_value(runtime, "() => document.title", thread="unfrozen") == "Unfrozen"
