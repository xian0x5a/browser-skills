from __future__ import annotations

import html
import shlex
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from patchright.sync_api import Browser, sync_playwright

from surf_agent.runtime import find_chrome_bin
from surf_google_search.browser_port import GOOGLE_PAGE_OBSERVATION_SCRIPT
from surf_google_search.urls import clean_destination_url


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    command = find_chrome_bin()
    assert command is not None, "Google DOM contract tests require a Chrome-family browser"
    executable = shlex.split(command)[0]
    with sync_playwright() as playwright:
        # Headless fixture pages do not need the production profile's Chromium sandbox.
        launched = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            chromium_sandbox=False,
        )
        yield launched
        launched.close()


STALLED_SCRIPT = "https://www.google.com/stalled.js"


def observe_fixture(browser: Browser, body: str, *, still_loading: bool = False) -> dict[str, object]:
    page = browser.new_page()
    if still_loading:
        # A parser-blocking script that never arrives keeps the document loading,
        # as Google's streamed response does before its final chunk.
        body += f"<script src='{STALLED_SCRIPT}'></script>"
        page.route(STALLED_SCRIPT, lambda route: None)
    page.route(
        "https://www.google.com/search**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=f"<textarea name='q'></textarea>{body}",
        ),
    )
    page.goto(
        "https://www.google.com/search?q=fixture&start=0&num=10",
        wait_until="commit" if still_loading else "load",
    )
    try:
        if still_loading:
            page.wait_for_selector("a", state="attached")
        return page.evaluate(GOOGLE_PAGE_OBSERVATION_SCRIPT)
    finally:
        page.close()


def organic_card(
    position: int,
    url: str,
    title: str,
    *,
    title_tag: str = "h3",
    cite: bool = True,
    data_attributes: bool = True,
    snippet: str = "A description long enough to read as result prose.",
) -> str:
    """A result card shaped like live Google, with optional fingerprints removed."""
    host = url.split("/")[2]
    heading = (
        f"<{title_tag}>{title}</{title_tag}>" if title_tag == "h3"
        else f"<div role='heading' aria-level='3'>{title}</div>"
    )
    displayed_url = f"<cite>https://{host}</cite>" if cite else f"<span>https://{host}</span>"
    slot = f' data-rpos="{position}"' if data_attributes else ""
    metadata = " data-snf" if data_attributes else ""
    snippet_marker = ' data-sncf="1"' if data_attributes else ""
    return (
        f"<div{slot}><div{metadata}><a href='{url}'>{heading}<br>{displayed_url}</a></div>"
        f"<div{snippet_marker}>{snippet}</div></div>"
    )


PAGER = """
<div role="navigation"><table role="presentation"><tr><td>
  <a id="pnnext" href="https://www.google.com/search?q=fixture&amp;start=10">Next</a>
</td></tr></table></div>
"""
ORGANIC_RESULT = organic_card(1, "https://example.com/result", "Fixture result")


def test_captcha_structure_requires_human_intervention(browser: Browser) -> None:
    observation = observe_fixture(browser, "<form id='captcha-form'></form>")

    assert observation == {
        "kind": "human_intervention",
        "results": [],
        "next_url": None,
    }


def test_unrelated_topstuff_content_does_not_affirm_no_results(browser: Browser) -> None:
    observation = observe_fixture(
        browser,
        "<div id='topstuff'>Showing results for a corrected spelling.</div>",
    )

    assert observation == {"kind": "unknown", "results": [], "next_url": None}


def test_explicit_no_results_structure_is_exhausted(browser: Browser) -> None:
    observation = observe_fixture(
        browser,
        """
        <div id="botstuff">
          <div class="mnr-c"><div class="JPMJ2c">
            <p role="heading">The query did not match any documents.</p>
          </div></div>
        </div>
        """,
    )

    assert observation == {"kind": "exhausted", "results": [], "next_url": None}


def test_primary_organic_extraction_excludes_answer_sources(browser: Browser) -> None:
    observation = observe_fixture(
        browser,
        """
        <div data-rpos="1">
          <div data-snf><a href="https://example.com/organic"><h3> Organic   title </h3><cite>https://example.com</cite></a></div>
          <div data-sncf="1"><span><span>Jun 23, 2026</span> —</span> Useful description. Read more</div>
        </div>
        <div data-rpos="3">
          <div data-snf data-sncf><a href="https://ai.example/source"><h3>AI source</h3></a></div>
        </div>
        <a href="https://answer.example/source"><h3>Featured answer source</h3></a>
        <div data-rpos="4">
          <a href="https://module.example/one"><h3>Module card one</h3></a>
          <a href="https://module.example/two"><h3>Module card two</h3></a>
        </div>
        <div role="navigation"><table role="presentation"><tr><td>
          <a id="pnnext" href="https://www.google.com/search?q=fixture&amp;start=10">Next</a>
        </td></tr></table></div>
        """,
    )

    assert observation == {
        "kind": "results",
        "results": [
            {
                "title": "Organic title",
                "url": "https://example.com/organic",
                "snippet": "Useful description.",
                "displayed_date": "Jun 23, 2026",
            }
        ],
        "next_url": "https://www.google.com/search?q=fixture&start=10",
    }


def test_visible_top_level_rich_result_excludes_nested_answer_sources(
    browser: Browser,
) -> None:
    observation = observe_fixture(
        browser,
        """
        <div data-rpos="10">
          <div data-q="Does Elon Musk have children?">
            <a style="display:none" href="https://www.instyle.com/hidden-source">
              <h3>Hidden answer source</h3>
            </a>
            <a href="https://nchstats.com/nested-source">
              <h3>Nested answer source</h3>
            </a>
          </div>
        </div>
        <div data-rpos="16">
          <div><a href="https://www.youtube.com/watch?v=cnb-uNTo28w">
            <h3>Elon Musk Family Tree</h3><cite>https://www.youtube.com › watch</cite>
          </a></div>
        </div>
        <div role="navigation"><table role="presentation"><tr><td>
          <a id="pnnext" href="https://www.google.com/search?q=fixture&amp;start=10">Next</a>
        </td></tr></table></div>
        """,
    )

    assert observation == {
        "kind": "results",
        "results": [
            {
                "title": "Elon Musk Family Tree",
                "url": "https://www.youtube.com/watch?v=cnb-uNTo28w",
                "snippet": None,
                "displayed_date": None,
            }
        ],
        "next_url": "https://www.google.com/search?q=fixture&start=10",
    }


def test_terminal_pagination_affirms_exhaustion_after_results(browser: Browser) -> None:
    observation = observe_fixture(
        browser,
        ORGANIC_RESULT
        + """
          <div role="navigation"><table role="presentation"><tr><td>
            <a id="pnprev" href="https://www.google.com/search?q=fixture&amp;start=0">Previous</a>
          </td></tr></table></div>
        """,
    )

    assert observation["kind"] == "results"
    assert observation["next_url"] is None
    assert len(observation["results"]) == 1


def test_most_likely_title_in_one_card_wins(browser: Browser) -> None:
    observation = observe_fixture(
        browser,
        """
        <div data-rpos="1">
          <div data-snf><a href="https://example.com/main"><h3>Main</h3><cite>https://example.com</cite></a></div>
          <div data-sncf="1">Main result description long enough to read as prose.</div>
          <div data-snf><a href="https://example.com/section"><h3>Section link</h3></a></div>
        </div>
        """ + PAGER,
    )

    assert observation["kind"] == "results"
    assert [result["url"] for result in observation["results"]] == ["https://example.com/main"]


def test_page_preserves_more_than_ten_organic_results(browser: Browser) -> None:
    results = "".join(
        organic_card(position, f"https://example.com/{position}", f"Result {position}")
        for position in range(1, 12)
    )
    observation = observe_fixture(
        browser,
        results
        + """
          <div role="navigation"><table role="presentation"><tr><td>
            <a id="pnnext" href="https://www.google.com/search?q=fixture&amp;start=10">Next</a>
          </td></tr></table></div>
        """,
    )

    assert observation["kind"] == "results"
    assert len(observation["results"]) == 11
    assert observation["next_url"] == "https://www.google.com/search?q=fixture&start=10"


def test_results_still_streaming_without_pagination_are_not_final(browser: Browser) -> None:
    observation = observe_fixture(browser, ORGANIC_RESULT, still_loading=True)

    assert observation == {"kind": "unknown", "results": [], "next_url": None}


def test_fully_loaded_last_page_without_pagination_is_final(browser: Browser) -> None:
    observation = observe_fixture(browser, ORGANIC_RESULT)

    assert observation["kind"] == "results"
    assert observation["next_url"] is None


@pytest.mark.parametrize(
    "fingerprints",
    [
        pytest.param({"title_tag": "heading", "cite": False}, id="title-tag-and-cite-changed"),
        pytest.param({"data_attributes": False}, id="data-attributes-renamed"),
        pytest.param({"data_attributes": False, "cite": False}, id="data-attributes-and-cite-changed"),
    ],
)
def test_organic_result_survives_partial_markup_changes(browser: Browser, fingerprints) -> None:
    card = organic_card(1, "https://example.com/result", "Fixture result", **fingerprints)

    observation = observe_fixture(browser, card + PAGER)

    assert observation["kind"] == "results"
    assert [result["url"] for result in observation["results"]] == ["https://example.com/result"]
    assert observation["results"][0]["title"] == "Fixture result"


def test_organic_result_with_most_fingerprints_gone_is_not_guessed(browser: Browser) -> None:
    card = organic_card(
        1, "https://example.com/result", "Fixture result",
        title_tag="heading", cite=False, data_attributes=False,
    )

    observation = observe_fixture(browser, card + PAGER)

    assert observation == {"kind": "unknown", "results": [], "next_url": None}


def test_media_and_news_cards_are_not_organic_results(browser: Browser) -> None:
    video = """
    <div data-rpos="2">
      <a href="https://www.youtube.com/watch?v=abc"><div role="heading" aria-level="3">Video title</div></a>
      <span>YouTube · Channel</span>
    </div>
    """

    observation = observe_fixture(browser, ORGANIC_RESULT + video + PAGER)

    assert [result["url"] for result in observation["results"]] == ["https://example.com/result"]


AD_REGION = "<div id='tads'>{}</div>"
AD_REDIRECT = "data-rw='https://www.google.com/aclk?sa=L'"


def test_ad_with_a_full_result_card_is_a_result(browser: Browser) -> None:
    # Ads are dampened, not excluded: a paid card with a full result's title,
    # displayed URL and snippet still qualifies.
    ad = (
        f"<div data-text-ad='1'><a href='https://quality-ad.example/guide' {AD_REDIRECT}>"
        "<h3>Complete buyer guide</h3><cite>https://quality-ad.example</cite></a>"
        "<div>A thorough description long enough to read as result prose.</div></div>"
    )

    observation = observe_fixture(browser, AD_REGION.format(ad) + f"<div id='rso'>{ORGANIC_RESULT}</div>{PAGER}")

    assert [result["url"] for result in observation["results"]] == [
        "https://quality-ad.example/guide",
        "https://example.com/result",
    ]


@pytest.mark.parametrize(
    ("region", "link_attributes"),
    [
        pytest.param(AD_REGION, "", id="ad-region"),
        pytest.param("<div>{}</div>", AD_REDIRECT, id="click-redirect"),
    ],
)
def test_ad_needs_more_result_fingerprints_than_an_organic_card(
    browser: Browser, region, link_attributes
) -> None:
    # This card shape (h3 title, host shown, snippet prose, no cite or result
    # metadata) passes as organic but falls short once it is an ad.
    def card(url: str, attributes: str = "") -> str:
        host = url.split("/")[2]
        return (
            f"<div><a href='{url}' {attributes}><h3>Offer from {host}</h3></a>"
            f"<span>{host}</span><div>A description long enough to read as result prose.</div></div>"
        )

    ad = region.format(card("https://ad.example/offer", link_attributes))
    organic = card("https://organic.example/page")

    observation = observe_fixture(browser, f"{ad}{organic}{PAGER}")

    assert [result["url"] for result in observation["results"]] == ["https://organic.example/page"]


def test_next_page_is_found_by_its_search_address(browser: Browser) -> None:
    pager = """
    <div><a href="https://www.google.com/search?q=fixture&amp;start=0">1</a>
    <a href="https://www.google.com/search?q=fixture&amp;start=10">2</a>
    <a href="https://www.google.com/search?q=fixture&amp;start=20">3</a></div>
    """

    observation = observe_fixture(browser, ORGANIC_RESULT + pager)

    assert observation["next_url"] == "https://www.google.com/search?q=fixture&start=10"


def test_news_module_headlines_are_not_organic_results(browser: Browser) -> None:
    # Top stories labels each headline with its source, which can match the host.
    headlines = "".join(
        f"<a href='https://{source}/story'><div role='heading' aria-level='3'>"
        f"Headline from {source}</div></a><span>{source}</span><span>1 hour ago</span>"
        for source in ("cbc.ca", "news.yahoo.com", "people.com")
    )
    module = f"<div data-rpos='2'><span>Top stories</span><span>Customize</span>{headlines}</div>"

    observation = observe_fixture(browser, f"<div id='rso'>{ORGANIC_RESULT}{module}</div>{PAGER}")

    assert [result["url"] for result in observation["results"]] == ["https://example.com/result"]


PLACEHOLDER_URL = "https://placeholder.test/x"


def card_linking_to(href: str, *, shown_host: str = "example.com", **fingerprints) -> str:
    """A result card whose link points at `href` while the card text shows `shown_host`."""
    card = organic_card(2, PLACEHOLDER_URL, "Wrapped result", **fingerprints)
    return card.replace(f"href='{PLACEHOLDER_URL}'", f"href='{html.escape(href)}'").replace(
        "placeholder.test", shown_host
    )


def google_redirect(
    destination: str,
    *,
    host: str = "www.google.com",
    path: str = "/url",
    parameter: str = "q",
) -> str:
    return f"https://{host}{path}?sa=t&{parameter}={quote(destination, safe='')}&ved=abc"


def observed_urls_with_one_ordinary_result(browser: Browser, href: str, **fingerprints) -> list[str]:
    body = ORGANIC_RESULT + card_linking_to(href, **fingerprints) + PAGER
    return [result["url"] for result in observe_fixture(browser, body)["results"]]


MARKUP_WITHOUT_FINGERPRINTS = {"cite": False, "data_attributes": False}
DESTINATION = "https://example.com/result?srsltid=tracking#section:~:text=highlight"
NESTED_ONCE = google_redirect(DESTINATION)


@pytest.mark.parametrize(
    "href",
    [
        pytest.param(google_redirect(DESTINATION), id="url-path-q-parameter"),
        pytest.param(google_redirect(DESTINATION, path="/goto", parameter="url"), id="goto-path-url-parameter"),
        pytest.param(f"https://www.google.com/url?q=&url={quote(DESTINATION, safe='')}", id="blank-q-falls-back-to-url"),
        pytest.param(google_redirect(DESTINATION, host="google.com"), id="bare-google-host"),
        pytest.param(google_redirect(NESTED_ONCE), id="two-wrapper-levels"),
    ],
)
def test_wrapped_result_is_emitted_as_its_destination(browser: Browser, href: str) -> None:
    urls = observed_urls_with_one_ordinary_result(browser, href, **MARKUP_WITHOUT_FINGERPRINTS)

    assert urls == ["https://example.com/result", DESTINATION]
    assert clean_destination_url(urls[1]) == "https://example.com/result#section"


@pytest.mark.parametrize(
    "href",
    [
        pytest.param("https://www.google.com/search?q=other", id="search-navigation"),
        pytest.param("https://www.google.com/search?q=https%3A%2F%2Fexample.com%2Fx", id="search-for-a-url"),
        pytest.param("https://www.google.com/aclk?sa=L&adurl=https%3A%2F%2Fexample.com%2Fad", id="ad-redirect"),
        pytest.param("https://www.google.com/url?sa=t&ved=abc", id="url-without-destination"),
        pytest.param("https://www.google.com/goto?url=opaque-token", id="goto-with-opaque-token"),
        pytest.param(google_redirect("javascript:alert(1)"), id="wrapped-javascript"),
        pytest.param(google_redirect("file:///etc/passwd"), id="wrapped-file"),
        pytest.param(google_redirect("/relative/path"), id="wrapped-relative"),
        pytest.param(google_redirect("https://user:password@example.com/report"), id="wrapped-credentials"),
        pytest.param(google_redirect("https://www.google.com/search?q=other"), id="wrapped-google-search"),
        pytest.param(google_redirect("https://example.com:99999/x"), id="wrapped-invalid-port"),
        pytest.param(google_redirect(google_redirect(NESTED_ONCE)), id="three-wrapper-levels"),
        pytest.param("https://user:password@example.com/report", id="direct-credentials"),
    ],
)
def test_unusable_links_are_not_results(browser: Browser, href) -> None:
    observation = observe_fixture(browser, ORGANIC_RESULT + card_linking_to(href) + PAGER)

    assert observation["kind"] == "results"
    assert [result["url"] for result in observation["results"]] == ["https://example.com/result"]


GOOGLE_OWNED_PAGE = "https://cloud.google.com/run/docs"


@pytest.mark.parametrize(
    ("href", "url"),
    [
        pytest.param(GOOGLE_OWNED_PAGE, GOOGLE_OWNED_PAGE, id="cloud-subdomain"),
        pytest.param("https://support.google.com/mail/answer/1", "https://support.google.com/mail/answer/1", id="support-subdomain"),
        pytest.param("https://google.github.io/styleguide/", "https://google.github.io/styleguide/", id="google-owned-github-pages"),
        pytest.param(google_redirect(GOOGLE_OWNED_PAGE), GOOGLE_OWNED_PAGE, id="wrapped-google-owned-site"),
    ],
)
def test_google_owned_sites_are_ordinary_results(browser: Browser, href: str, url: str) -> None:
    urls = observed_urls_with_one_ordinary_result(browser, href)

    assert urls == ["https://example.com/result", url]
    assert clean_destination_url(url) == url


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        pytest.param("https://@example.com/x", "https://example.com/x", id="empty-userinfo"),
        pytest.param("https:example.com/x", "https://example.com/x", id="no-slashes"),
        pytest.param("https://example.com\\@evil.com/x", "https://example.com/@evil.com/x", id="backslash-before-at"),
        pytest.param(
            "https://www.google.com/./url?q=https%3A%2F%2Fexample.com%2Fx", "https://example.com/x", id="nested-dot-segment-wrapper"
        ),
    ],
)
def test_non_canonical_destinations_are_emitted_canonically(browser: Browser, inner, expected) -> None:
    urls = observed_urls_with_one_ordinary_result(browser, google_redirect(inner), **MARKUP_WITHOUT_FINGERPRINTS)

    assert urls == ["https://example.com/result", expected]
    assert clean_destination_url(urls[1]) == expected
