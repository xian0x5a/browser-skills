from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from .backends.base import AgentPage
from .constants import (
    SNAPSHOT_DIFF_MAX_HUNKS,
    SNAPSHOT_DIFF_MAX_RATIO,
    SNAPSHOT_DIFF_MIN_SAVED_CHARS,
)

MASKED_VALUE = "••••••••"

# The roles a secret field can render as. The tree prints no input type, so a
# line can only name a candidate; the browser decides whether it is a secret.
_INPUT_VALUE_LINE = re.compile(
    r"""
    (?P<prefix>
        (?P<indent>\ *)-\ '?                    # YAML quotes a key that needs it
        (?:textbox|searchbox|combobox|spinbutton)
        (?:\ "(?:[^"\\]|\\.)*")?                # accessible name
        (?P<attributes>(?:\ \[[^\]]*\])*)'?:\ ?
    )
    (?P<value>.*)
    """,
    re.VERBOSE,
)
_REF_ATTRIBUTE = re.compile(r" \[ref=([^\]]+)\]")
# These roles build their accessible name from their descendants, which embeds an
# input's value. Other roles keep their names: those come from the page's author.
_NAMED_FROM_CONTENT_LINE = re.compile(
    r"""
    ^(?P<node>\ *-\ '?(?:
        button|cell|checkbox|columnheader|gridcell|heading|link|menuitem|menuitemcheckbox
        |menuitemradio|option|radio|row|rowheader|switch|tab|tooltip|treeitem
    ))
    \ "(?:[^"\\]|\\.)*"
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class SnapshotCapture:
    text: str
    page_id: int | None
    url: str | None
    title: str | None
    origin: str | None
    url_without_fragment: str | None


@dataclass(frozen=True)
class InputValueLine:
    index: int
    prefix: str
    ref: str | None


@dataclass(frozen=True)
class SnapshotDiffDecision:
    output: str
    used_diff: bool
    reason: str


def input_value_lines(snapshot_text: str) -> list[InputValueLine]:
    """Find the lines where a snapshot prints the value of a text-like input."""
    lines = snapshot_text.split("\n")
    found: list[InputValueLine] = []
    for index, line in enumerate(lines):
        node = _INPUT_VALUE_LINE.fullmatch(line)
        if node is None:
            continue
        ref_attribute = _REF_ATTRIBUTE.search(node["attributes"])
        ref = ref_attribute[1] if ref_attribute else None
        if node["value"]:
            found.append(InputValueLine(index, node["prefix"], ref))
            continue
        # Next to a placeholder the value moves to a nested "- text:" line.
        child_indent = node["indent"] + "  "
        text_prefix = child_indent + "- text: "
        for child_index in range(index + 1, len(lines)):
            child = lines[child_index]
            if not child.startswith(child_indent):
                break
            if child.startswith(text_prefix):
                found.append(InputValueLine(child_index, text_prefix, ref))
                break
    return found


def mask_input_values(snapshot_text: str, value_lines: list[InputValueLine]) -> str:
    lines = snapshot_text.split("\n")
    value_prefixes = {value_line.index: value_line.prefix for value_line in value_lines}
    # Bottom-up, a line indented less than the masked lines below it is their ancestor.
    masked_indent = -1
    for index in range(len(lines) - 1, -1, -1):
        indent = len(lines[index]) - len(lines[index].lstrip(" "))
        if index in value_prefixes:
            lines[index] = value_prefixes[index] + MASKED_VALUE
            masked_indent = max(masked_indent, indent)
        elif indent < masked_indent:
            lines[index] = _NAMED_FROM_CONTENT_LINE.sub(rf'\g<node> "{MASKED_VALUE}"', lines[index])
            masked_indent = indent
    return "\n".join(lines)


def snapshot_capture_from_page(*, text: str, page: AgentPage) -> SnapshotCapture:
    return SnapshotCapture(
        text=text,
        page_id=page.page_id,
        url=page.url,
        title=page.title,
        origin=url_origin(page.url),
        url_without_fragment=url_without_fragment(page.url),
    )


def url_origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def url_without_fragment(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def unified_snapshot_diff(
    before: SnapshotCapture,
    after: SnapshotCapture,
    *,
    before_label: str,
    after_label: str,
) -> str:
    return "".join(
        # difflib preserves unterminated source lines but omits GNU diff's marker.
        # Add it so adjacent deletion/addition records cannot concatenate.
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(
            before.text.splitlines(keepends=True),
            after.text.splitlines(keepends=True),
            fromfile=before_label,
            tofile=after_label,
        )
    )


def count_diff_hunks(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith("@@"))


def choose_snapshot_diff(
    before: SnapshotCapture | None,
    after: SnapshotCapture,
    *,
    before_label: str,
    after_label: str,
) -> SnapshotDiffDecision:
    if before is None:
        return SnapshotDiffDecision(
            snapshot_fallback_output(after.text, "no baseline"),
            used_diff=False,
            reason="no baseline",
        )
    if before.page_id != after.page_id:
        return SnapshotDiffDecision(
            snapshot_fallback_output(after.text, "page changed"),
            used_diff=False,
            reason="page changed",
        )
    if before.origin and after.origin and before.origin != after.origin:
        return SnapshotDiffDecision(
            snapshot_fallback_output(after.text, "origin changed"),
            used_diff=False,
            reason="origin changed",
        )

    diff_text = unified_snapshot_diff(
        before, after, before_label=before_label, after_label=after_label
    )
    if not diff_text:
        return SnapshotDiffDecision(
            f"--- {before_label}\n+++ {after_label}\n"
            + format_snapshot_header("diff", "no changes"),
            used_diff=True,
            reason="no changes",
        )

    diff_chars = len(diff_text)
    full_chars = len(after.text)
    saved_chars = full_chars - diff_chars
    hunk_count = count_diff_hunks(diff_text)

    if diff_chars > full_chars * SNAPSHOT_DIFF_MAX_RATIO:
        return SnapshotDiffDecision(
            snapshot_fallback_output(after.text, "diff too large"),
            used_diff=False,
            reason="diff too large",
        )
    if saved_chars < SNAPSHOT_DIFF_MIN_SAVED_CHARS:
        return SnapshotDiffDecision(
            snapshot_fallback_output(
                after.text, f"saved chars < {SNAPSHOT_DIFF_MIN_SAVED_CHARS}"
            ),
            used_diff=False,
            reason="saved chars too small",
        )
    if hunk_count > SNAPSHOT_DIFF_MAX_HUNKS:
        return SnapshotDiffDecision(
            snapshot_fallback_output(after.text, f"hunks > {SNAPSHOT_DIFF_MAX_HUNKS}"),
            used_diff=False,
            reason="too many hunks",
        )
    return SnapshotDiffDecision(
        format_snapshot_header("diff", "") + diff_text, used_diff=True, reason=""
    )


def snapshot_fallback_output(snapshot_text: str, reason: str) -> str:
    return format_snapshot_header("fallback", reason) + snapshot_text


def format_snapshot_header(kind: str, reason: str) -> str:
    if not reason:
        return ""
    label = "snapshot-diff" if kind == "diff" else "snapshot fallback"
    return f"# {label}: {reason}\n"
