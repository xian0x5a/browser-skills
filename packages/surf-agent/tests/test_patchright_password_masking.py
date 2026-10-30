"""Snapshots go to the model, so a password field's value must never appear in one."""

from __future__ import annotations

import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from patchright.async_api import Locator

from surf_agent.backends.patchright import bridge
from surf_agent.backends.patchright.bridge import PatchrightRuntime

pytestmark = pytest.mark.skipif(
    os.environ.get("SURF_TEST_LIVE_PATCHRIGHT") != "1",
    reason="requires installed Chrome and Patchright; set SURF_TEST_LIVE_PATCHRIGHT=1",
)

MASK = "•" * 8
YAML_SPECIAL_SECRET = 'a: "b" #c\\d'

# (label, extra attributes, secret). The label is the accessible name via aria-label, so any
# characters are allowed; the secret is what must never leave the browser.
LIGHT_DOM_PASSWORDS = [
    ("PwPlain", "", "S3cr3t-plain"),
    ("PwFocused", "", "S3cr3t-focused"),
    ("Password: required #1", "", "S3cr3t-quoted"),
    ("Dave's secret", "", "S3cr3t-apostrophe"),
    ("PwNested", 'placeholder="Enter it"', "S3cr3t-nested"),
    ("PwYaml", "", YAML_SPECIAL_SECRET),
    ("PwCombobox", 'role="combobox"', "S3cr3t-combobox"),
    ("PwSearchbox", 'role="searchbox"', "S3cr3t-searchbox"),
    ("PwSpinbutton", 'role="spinbutton"', "S3cr3t-spinbutton"),
]
POINTER_EVENTS_NONE = ("PwNoRef", "S3cr3t-noref")
TABLE_CELL = ("PwTable", "S3cr3t-table")
SHADOW = ("PwShadow", "S3cr3t-shadow")
IFRAME = ("PwFrame", "S3cr3t-frame")

MASKING_PAGE = (
    "<title>Masking</title><body>"
    + "".join(
        f'<input id="light{i}" type="password" aria-label="{label}" {attrs}>'
        for i, (label, attrs, _) in enumerate(LIGHT_DOM_PASSWORDS)
    )
    + f'<input id="noref" type="password" style="pointer-events:none" aria-label="{POINTER_EVENTS_NONE[0]}">'
    + f'<table><tr><td><input id="table" type="password" aria-label="{TABLE_CELL[0]}"></td></tr></table>'
    + '<div id="host"></div>'
    + f"<script>host.attachShadow({{mode:'open'}}).innerHTML='<input type=password aria-label={SHADOW[0]}>'</script>"
    + f"<iframe id=\"frame\" srcdoc='<input type=password aria-label={IFRAME[0]}>'></iframe>"
    + "</body>"
)

SET_VALUES_SCRIPT = """() => {
  const set = (input, value) => { input.value = value; };
  set(document.getElementById('noref'), %(noref)s);
  set(document.getElementById('table'), %(table)s);
  set(host.shadowRoot.querySelector('input'), %(shadow)s);
  set(frame.contentDocument.querySelector('input'), %(frame)s);
  document.getElementById('light1').focus();
}"""


def js(value: str) -> str:
    import json

    return json.dumps(value)


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def publish(name: str, html: str) -> str:
        (root / name).write_text(html)
        return f"http://127.0.0.1:{server.server_port}/{name}"

    try:
        yield publish
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ACTION_TIMEOUT_MS", 500)
    runtime = PatchrightRuntime(profile_dir=tmp_path / "profile", headless=True)
    try:
        yield runtime
    finally:
        runtime.stop()


def field_block(snapshot: str, name: str) -> str:
    """The node line that carries *name* plus its nested lines."""
    lines = snapshot.splitlines()
    start = next(i for i, line in enumerate(lines) if name in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return "\n".join(block)


def ref_of(snapshot: str, name: str) -> str:
    return re.search(r"\[ref=(\w+)\]", field_block(snapshot, name).splitlines()[0]).group(1)


def open_page(runtime: PatchrightRuntime, site, html: str) -> None:
    runtime.call("open", {"url": site("index.html", html)})


def snapshot_of(runtime: PatchrightRuntime) -> str:
    return runtime.call("snapshot", {})


def test_filled_password_fields_render_only_the_mask(runtime, site):
    open_page(runtime, site, MASKING_PAGE)
    for i, (_, _, secret) in enumerate(LIGHT_DOM_PASSWORDS):
        assert runtime.call("fill", {"uid": f"#light{i}", "text": secret}) == "filled\n"
    runtime.call(
        "eval",
        {
            "code": SET_VALUES_SCRIPT
            % {
                "noref": js(POINTER_EVENTS_NONE[1]),
                "table": js(TABLE_CELL[1]),
                "shadow": js(SHADOW[1]),
                "frame": js(IFRAME[1]),
            }
        },
    )
    snapshot = snapshot_of(runtime)

    cases = [(label, secret) for label, _, secret in LIGHT_DOM_PASSWORDS]
    cases += [POINTER_EVENTS_NONE, TABLE_CELL, SHADOW, IFRAME]
    problems = []
    for label, secret in cases:
        block = field_block(snapshot, label)
        # The YAML-special secret is printed escaped, so look for a fragment that survives escaping.
        leaked_fragment = "#c" if secret == YAML_SPECIAL_SECRET else secret
        if leaked_fragment in snapshot:
            problems.append(f"{label}: value visible in\n{block}")
        if MASK not in block or re.search("•{9,}", block):
            problems.append(f"{label}: exact mask missing in\n{block}")
    assert "[active]" in field_block(snapshot, "PwFocused"), "fixture lost focus; the focused case is not exercised"
    assert not problems, "\n\n".join(problems) + f"\n\nfull snapshot:\n{snapshot}"


def test_mask_hides_password_length(runtime, site):
    html = '<input id="short" type="password" aria-label="LenShort"><input id="long" type="password" aria-label="LenLong">'
    open_page(runtime, site, html)
    runtime.call("fill", {"uid": "#short", "text": "zq"})
    runtime.call("fill", {"uid": "#long", "text": "z" * 30})
    snapshot = snapshot_of(runtime)

    def normalized(name: str) -> str:
        # Focus follows the last fill, so [active] is not part of what the mask must hide.
        block = field_block(snapshot, name).replace(" [active]", "")
        return re.sub(r"\s*\[ref=\w+\]", "", block).replace(name, "NAME")

    assert normalized("LenShort") == normalized("LenLong")
    assert MASK in normalized("LenShort")


def test_empty_password_field_has_no_mask_until_filled(runtime, site):
    open_page(runtime, site, '<input id="pw" type="password" aria-label="PwEmpty">')
    assert MASK not in snapshot_of(runtime)
    runtime.call("fill", {"uid": "#pw", "text": "S3cr3t-later"})
    snapshot = snapshot_of(runtime)
    assert "S3cr3t-later" not in snapshot
    assert MASK in snapshot


def test_short_password_does_not_damage_the_rest_of_the_snapshot(runtime, site):
    html = (
        '<p>Every sentence here keeps its letters</p><input id="text" aria-label="Plain" value="tree">'
        '<button>Seven</button><input id="pw" type="password" aria-label="PwShort">'
    )
    open_page(runtime, site, html)
    before = snapshot_of(runtime)
    runtime.call("fill", {"uid": "#pw", "text": "e"})
    after = snapshot_of(runtime)

    # Focus moves to the filled field, so [active] is not part of the comparison.
    def unchanged(text: str) -> list[str]:
        return [line.replace(" [active]", "") for line in text.splitlines() if "PwShort" not in line]

    assert unchanged(after) == unchanged(before)
    assert re.search(r"\[ref=e\d+\]", after)
    assert "Every sentence here keeps its letters" in after
    assert MASK in field_block(after, "PwShort")


NON_PASSWORD_FIELDS = [
    ("PlainText", '<input id="f0" type="text" aria-label="PlainText">', "visible-text"),
    ("EmailField", '<input id="f1" type="email" aria-label="EmailField">', "me@example.test"),
    ("SearchField", '<input id="f2" type="search" aria-label="SearchField">', "visible-search"),
    ("NumberField", '<input id="f3" type="number" aria-label="NumberField">', "424242"),
    ("AreaField", '<textarea id="f4" aria-label="AreaField"></textarea>', "visible-area"),
    (
        "ListField",
        '<input id="f5" list="opts" aria-label="ListField"><datalist id="opts"><option value="visible-list"></datalist>',
        "visible-list",
    ),
]


def test_non_password_fields_keep_their_values(runtime, site):
    open_page(runtime, site, "".join(html for _, html, _ in NON_PASSWORD_FIELDS))
    for i, (_, _, value) in enumerate(NON_PASSWORD_FIELDS):
        runtime.call("fill", {"uid": f"#f{i}", "text": value})
    snapshot = snapshot_of(runtime)
    missing = [name for name, _, value in NON_PASSWORD_FIELDS if value not in field_block(snapshot, name)]
    assert not missing, f"{missing} lost their value in\n{snapshot}"


def test_masked_field_keeps_placeholder_and_a_usable_ref(runtime, site):
    html = '<input id="pw" type="password" aria-label="PwNested" placeholder="Enter it">'
    open_page(runtime, site, html)
    runtime.call("fill", {"uid": "#pw", "text": "S3cr3t-first"})
    snapshot = snapshot_of(runtime)
    block = field_block(snapshot, "PwNested")
    assert "Enter it" in block
    assert "S3cr3t-first" not in snapshot

    assert runtime.call("fill", {"uid": f"@{ref_of(snapshot, 'PwNested')}", "text": "new"}) == "filled\n"
    assert runtime.call("eval", {"code": "() => pw.value"}).strip().strip('"') == "new"


def test_html_fallback_omits_password_value_attribute(runtime, site, monkeypatch):
    # Force the last resort: both ARIA attempts and inner_text fail, so the snapshot is page HTML.
    def fail(*args, **kwargs):
        raise RuntimeError("forced failure")

    async def aria_fails(self, target):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(PatchrightRuntime, "_aria_snapshot", aria_fails)
    monkeypatch.setattr(Locator, "inner_text", fail)
    open_page(
        runtime,
        site,
        '<p>Fallback marker</p><input type="password" aria-label="PwHtml" value="PREFILLED">',
    )
    snapshot = snapshot_of(runtime)
    assert "Fallback marker" in snapshot
    assert "PREFILLED" not in snapshot


LEAK = "S3cretLeak99"
PW = '<input id="pw" type="password" aria-label="Password">'
# Each shape lets the value reach an ancestor's content-derived name, because the
# field or an ancestor has no ref (pointer-events: none) so Playwright keeps the name.
ANCESTOR_NAME_SHAPES = {
    "locked-form-table": '<form style="pointer-events:none;opacity:.6"><table><tr><td>Password</td><td><input id="pw" type="password" style="pointer-events:none"></td></tr></table><button>Log in</button></form>',
    "locked-div-around-table": f'<div style="pointer-events:none"><table><tr><td>Password</td><td>{PW}</td></tr></table></div>',
    "locked-link": f'<a href="#x" style="pointer-events:none">{PW}</a>',
    "locked-role-button": f'<div role="button" style="pointer-events:none">{PW}</div>',
    "ref-field-locked-td": f'<table><tr><td>Password</td><td style="pointer-events:none">{PW}</td></tr></table>',
}


@pytest.mark.parametrize("html", ANCESTOR_NAME_SHAPES.values(), ids=ANCESTOR_NAME_SHAPES.keys())
def test_password_value_does_not_leak_through_ancestor_names(runtime, site, html):
    open_page(runtime, site, html)
    runtime.call("eval", {"code": f"() => {{ pw.value = {js(LEAK)}; }}"})
    snapshot = snapshot_of(runtime)
    assert LEAK not in snapshot, snapshot


def test_authored_names_survive_around_a_filled_password(runtime, site):
    html = (
        f'<form aria-label="Sign in form"><table aria-label="Layout"><tr><td>Password</td><td>{PW}</td></tr></table>'
        f'<dialog open aria-label="Sign in"></dialog><section aria-label="Creds"><button>Go</button></section></form>'
    )
    open_page(runtime, site, html)
    runtime.call("fill", {"uid": "#pw", "text": "S3cr3t-authored"})
    snapshot = snapshot_of(runtime)
    for expected in ('form "Sign in form"', 'table "Layout"', 'dialog "Sign in"', 'region "Creds"', 'cell "Password"'):
        assert expected in snapshot, f"{expected} lost in\n{snapshot}"


def test_text_fields_in_shadow_root_and_iframe_keep_their_values(runtime, site):
    html = (
        "<div id=host></div>"
        "<script>host.attachShadow({mode:'open'}).innerHTML='<input type=text aria-label=TextShadow>'</script>"
        "<iframe id=frame srcdoc='<input type=text aria-label=TextFrame>'></iframe>"
    )
    open_page(runtime, site, html)
    runtime.call(
        "eval",
        {
            "code": "() => { host.shadowRoot.querySelector('input').value = 'visible-shadow';"
            " frame.contentDocument.querySelector('input').value = 'visible-frame'; }"
        },
    )
    snapshot = snapshot_of(runtime)
    assert "visible-shadow" in field_block(snapshot, "TextShadow")
    assert "visible-frame" in field_block(snapshot, "TextFrame")


@pytest.fixture
def forced_html_fallback(monkeypatch):
    async def aria_fails(self, target):
        raise RuntimeError("forced failure")

    def inner_text_fails(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(PatchrightRuntime, "_aria_snapshot", aria_fails)
    monkeypatch.setattr(Locator, "inner_text", inner_text_fails)


def test_html_fallback_does_not_run_page_code(runtime, site, forced_html_fallback):
    html = (
        "<script>document.documentElement.dataset.constructed = 0;"
        "customElements.define('x-count', class extends HTMLElement { constructor() { super(); document.documentElement.dataset.constructed++; } });"
        "</script><p>Marker</p>"
    )
    open_page(runtime, site, html)
    runtime.call("eval", {"code": "() => document.body.append(document.createElement('x-count'))"})
    before = runtime.call("eval", {"code": "() => document.documentElement.dataset.constructed"})
    assert "Marker" in snapshot_of(runtime)
    assert runtime.call("eval", {"code": "() => document.documentElement.dataset.constructed"}) == before


def test_html_fallback_survives_a_document_without_root_element(runtime, site, forced_html_fallback):
    open_page(runtime, site, "<p>Gone soon</p>")
    runtime.call("eval", {"code": "() => document.documentElement.remove()"})
    runtime.call("text", {})
    runtime.call("snapshot", {})
