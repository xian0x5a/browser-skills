from __future__ import annotations

import pytest

from surf_google_search.urls import clean_destination_url

UNCHANGED_QUERIES = {
    "gbk-percent-encoding": "https://example.com/s?wd=%C4%E3%BA%C3",
    "parameter-without-equals": "https://example.com/x?flag",
    "semicolon-separator": "https://example.com/x?a=1;b=2",
    "unescaped-slashes": "https://example.com/login?next=/a/b",
    "encoded-space": "https://example.com/s?q=a%20b",
    "encoded-tilde": "https://example.com/x?v=%7E",
    "names-and-values-containing-srsltid": "https://example.com/x?srsltid_ref=1&ref=srsltid",
    "empty-segment": "https://example.com/x?a=1&&b=2",
}

SRSLTID_REMOVALS = {
    "leading": ("https://example.com/x?srsltid=abc&a=1", "https://example.com/x?a=1"),
    "trailing": ("https://example.com/x?a=1&srsltid=abc", "https://example.com/x?a=1"),
    "middle-mixed-case": (
        "https://example.com/x?a=1&SRSLTID=abc&b=%C4%E3",
        "https://example.com/x?a=1&b=%C4%E3",
    ),
    "only-parameter": ("https://example.com/x?srsltid=abc", "https://example.com/x"),
    "valueless": ("https://example.com/x?a=1&srsltid", "https://example.com/x?a=1"),
}

CASES = {
    **{name: (url, url) for name, url in UNCHANGED_QUERIES.items()},
    **{f"srsltid-{name}": pair for name, pair in SRSLTID_REMOVALS.items()},
}


@pytest.mark.parametrize(
    ("url", "expected"), CASES.values(), ids=CASES.keys()
)
def test_destination_query_is_preserved_except_srsltid(url: str, expected: str) -> None:
    assert clean_destination_url(url) == expected
