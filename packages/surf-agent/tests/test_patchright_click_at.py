"""Live contract of the click-at bridge tool and CSS-scale screenshots.

click_at exists for page content no ref or selector reaches (a 1Password passkey
popup in a closed shadow root), aimed with coordinates read off a screenshot.
"""

from __future__ import annotations

import json
import os
import struct
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from surf_agent.backends.patchright import bridge
from runtime_driver import SyncRuntime
from surf_agent.backends.patchright.bridge import PatchrightRuntime
from surf_agent.errors import ErrorCode

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

# Mimics the extension popup: a button inside a closed shadow root, so neither a
# CSS selector nor a snapshot ref can reach it. Its listener records what the
# page saw; clicks anywhere else land in `stray`. Records live in the DOM because
# eval runs in an isolated world that cannot see page globals.
CLOSED_SHADOW_PAGE = """<title>Passkey</title>
<body style="margin:0">
  <div id="host" style="position:absolute;left:100px;top:100px;width:200px;height:80px"></div>
  <script>
    const record = (name, entry) => {
      document.body.dataset[name] = JSON.stringify([...JSON.parse(document.body.dataset[name]), entry]);
    };
    document.body.dataset.clicks = '[]';
    document.body.dataset.stray = '[]';
    const root = host.attachShadow({mode: 'closed'});
    root.innerHTML = '<button style="all:unset;display:block;width:200px;height:80px;background:#ccc">Use passkey</button>';
    root.querySelector('button').addEventListener('click', event => {
      record('clicks', {trusted: event.isTrusted, x: event.clientX, y: event.clientY});
    });
    document.addEventListener('click', event => {
      if (event.target !== host) record('stray', {x: event.clientX, y: event.clientY});
    });
  </script>
</body>"""

# A red target on a white page that also scrolls, so a full-page shot is taller
# than the viewport. The target sits near the origin so that doubling its centre
# (a device-pixel reading at scale 2) still lands inside even a small headless
# viewport, but off the target.
SCALED_PAGE = """<title>Scaled</title>
<body style="margin:0;background:#fff;height:2000px">
  <div id="target" style="position:absolute;left:60px;top:40px;width:60px;height:30px;background:#f00"></div>
  <script>
    document.body.dataset.hits = '[]';
    document.addEventListener('click', event => {
      const hits = JSON.parse(document.body.dataset.hits);
      hits.push({id: event.target.id, trusted: event.isTrusted, x: event.clientX, y: event.clientY});
      document.body.dataset.hits = JSON.stringify(hits);
    });
  </script>
</body>"""

# Not 1, so CSS and device pixels differ, as on a scaled (HiDPI) display.
DEVICE_SCALE_FACTOR = 2


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "shadow.html").write_text(CLOSED_SHADOW_PAGE)
    (root / "scaled.html").write_text(SCALED_PAGE)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def start_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PatchrightRuntime:
    # Keep failure cases fast; production uses the longer default.
    monkeypatch.setattr(bridge, "ACTION_TIMEOUT_MS", 500)
    return SyncRuntime(PatchrightRuntime(profile_dir=tmp_path / "profile", headless=True))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = start_runtime(tmp_path, monkeypatch)
    try:
        yield runtime
    finally:
        runtime.stop()


@pytest.fixture
def scaled_runtime(tmp_path, monkeypatch):
    """A runtime whose Chrome renders at DEVICE_SCALE_FACTOR, like a scaled display."""
    capture = bridge.capture_default_args

    async def capture_with_scale(chromium, **launch_options):
        # Chrome's own flag stands in for OS display scaling; the runtime has no other hook.
        return [*(await capture(chromium, **launch_options)), f"--force-device-scale-factor={DEVICE_SCALE_FACTOR}"]

    monkeypatch.setattr(bridge, "capture_default_args", capture_with_scale)
    runtime = start_runtime(tmp_path, monkeypatch)
    try:
        yield runtime
    finally:
        runtime.stop()


def page_value(runtime: PatchrightRuntime, code: str):
    return json.loads(runtime.call("eval", {"code": code}))


def recorded(runtime: PatchrightRuntime, name: str) -> list:
    return json.loads(page_value(runtime, f"() => document.body.dataset.{name}"))


def viewport_size(runtime: PatchrightRuntime) -> tuple[int, int]:
    width, height = page_value(runtime, "() => [innerWidth, innerHeight]")
    return width, height


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def red_box_in_image(runtime: PatchrightRuntime, image_url: str) -> dict:
    """Bounding box of pure-red pixels in the image, decoded by the browser itself."""
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
            let left = Infinity, top = Infinity, right = -1, bottom = -1;
            for (let y = 0; y < canvas.height; y++) {{
                for (let x = 0; x < canvas.width; x++) {{
                    const i = (y * canvas.width + x) * 4;
                    if (pixels[i] > 240 && pixels[i + 1] < 15 && pixels[i + 2] < 15) {{
                        left = Math.min(left, x); top = Math.min(top, y);
                        right = Math.max(right, x); bottom = Math.max(bottom, y);
                    }}
                }}
            }}
            return {{left, top, right, bottom}};
        }}""",
    )


def test_click_at_reaches_button_in_closed_shadow_root_with_trusted_click(runtime, site):
    url, _root = site
    runtime.call("open", {"url": f"{url}/shadow.html"})

    runtime.call("click-at", {"x": 150, "y": 140})

    clicks = recorded(runtime, "clicks")
    assert clicks == [{"trusted": True, "x": 150, "y": 140}]


@pytest.mark.parametrize(
    "point",
    [
        pytest.param(lambda w, h: (0, 0), id="top-left-corner"),
        pytest.param(lambda w, h: (w - 1, h - 1), id="bottom-right-pixel"),
        pytest.param(lambda w, h: (w - 0.5, h - 0.5), id="bottom-right-fraction"),
        pytest.param(lambda w, h: (10.5, 20.25), id="fractional"),
    ],
)
def test_click_at_lands_at_points_inside_the_viewport(runtime, site, point):
    url, _root = site
    runtime.call("open", {"url": f"{url}/shadow.html"})
    x, y = point(*viewport_size(runtime))

    runtime.call("click-at", {"x": x, "y": y})

    stray = recorded(runtime, "stray")
    assert len(stray) == 1
    # MouseEvent client coordinates may drop the fraction.
    assert abs(stray[0]["x"] - x) < 1 and abs(stray[0]["y"] - y) < 1


@pytest.mark.parametrize(
    "point",
    [
        pytest.param(lambda w, h: (w, 10), id="right-edge"),
        pytest.param(lambda w, h: (10, h), id="bottom-edge"),
        pytest.param(lambda w, h: (w + 50, h + 50), id="far-outside"),
        pytest.param(lambda w, h: (-1, 10), id="negative-x"),
        pytest.param(lambda w, h: (10, -0.5), id="negative-y"),
    ],
)
def test_click_at_outside_viewport_is_not_visible_and_clicks_nothing(runtime, site, point):
    url, _root = site
    runtime.call("open", {"url": f"{url}/shadow.html"})
    x, y = point(*viewport_size(runtime))

    with pytest.raises(bridge.BridgeCodedError) as raised:
        runtime.call("click-at", {"x": x, "y": y})

    assert raised.value.code == ErrorCode.NOT_VISIBLE
    assert (recorded(runtime, "clicks"), recorded(runtime, "stray")) == ([], [])


@pytest.mark.parametrize("full_page", [False, True], ids=["viewport", "full-page"])
def test_screenshot_is_in_css_pixels_on_scaled_display(scaled_runtime, site, tmp_path, full_page):
    url, _root = site
    scaled_runtime.call("open", {"url": f"{url}/scaled.html"})
    assert page_value(scaled_runtime, "() => devicePixelRatio") == DEVICE_SCALE_FACTOR
    if full_page:
        expected = tuple(page_value(scaled_runtime, "() => [innerWidth, document.documentElement.scrollHeight]"))
    else:
        expected = viewport_size(scaled_runtime)
    shot = tmp_path / "shot.png"

    scaled_runtime.call("screenshot", {"path": str(shot), "fullPage": full_page})

    assert png_size(shot) == expected


def test_point_read_from_screenshot_clicks_same_element_on_scaled_display(scaled_runtime, site):
    url, root = site
    scaled_runtime.call("open", {"url": f"{url}/scaled.html"})
    assert page_value(scaled_runtime, "() => devicePixelRatio") == DEVICE_SCALE_FACTOR
    scaled_runtime.call("screenshot", {"path": str(root / "shot.png")})

    box = red_box_in_image(scaled_runtime, f"{url}/shot.png")
    assert box["right"] >= box["left"], f"target not found in screenshot: {box}"
    x = (box["left"] + box["right"]) / 2
    y = (box["top"] + box["bottom"]) / 2
    scaled_runtime.call("click-at", {"x": x, "y": y})

    hits = recorded(scaled_runtime, "hits")
    assert [(hit["id"], hit["trusted"]) for hit in hits] == [("target", True)]
