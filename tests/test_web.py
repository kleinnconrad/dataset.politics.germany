"""Tests of the HTTP client and its fetch rules."""
import pytest

from web import BlockedError, FetchError, NotFoundError, RobotsDisallowedError, is_challenge, parse_robots

BUNDESRAT_ROBOTS = """User-agent: *
Disallow: /SiteGlobals/
Disallow: /DE/Service/
Allow: /SiteGlobals/Functions/RSSFeed
Crawl-delay: 30
"""


def test_longest_matching_rule_wins() -> None:
    rules = parse_robots(BUNDESRAT_ROBOTS)
    assert rules.allows("/SiteGlobals/Functions/RSSFeed/RSSGenerator_Announcement.xml")
    assert not rules.allows("/SiteGlobals/Forms/Suche/suche.html")
    assert not rules.allows("/DE/Service/kontakt.html")
    assert rules.allows("/DE/plenum/plenum-node.html")
    assert rules.crawl_delay == 30


def test_robots_rules_of_equal_length_prefer_allow_and_support_wildcards() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\nDisallow: /*.pdf$\nDisallow:\n")
    assert rules.allows("/page")
    assert not rules.allows("/files/report.pdf")
    assert rules.allows("/files/report.pdf?download=1")


def test_robots_group_of_the_agent_replaces_the_default_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: OtherBot\nUser-agent: germanpoliticsdataset\nDisallow: /intern/\n"
    rules = parse_robots(text)
    assert rules.allows("/politik/")
    assert not rules.allows("/intern/x")

ARTICLE = "https://news.example/politik/artikel-1"


def test_robots_txt_disallow_is_respected(fake_web) -> None:
    fake_web.add("https://news.example/robots.txt", "User-agent: *\nDisallow: /politik/\n", content_type="text/plain")
    fake_web.add(ARTICLE, "<html></html>")
    client = fake_web.client()

    with pytest.raises(RobotsDisallowedError):
        client.get(ARTICLE)
    assert ARTICLE not in fake_web.requested


def test_robots_txt_group_for_the_pipeline_agent_wins(fake_web) -> None:
    fake_web.add("https://news.example/robots.txt",
                 "User-agent: *\nDisallow: /\n\nUser-agent: GermanPoliticsDataset\nAllow: /\n", content_type="text/plain")
    fake_web.add(ARTICLE, "<html></html>")

    assert fake_web.client().get(ARTICLE).status == 200


def test_missing_robots_txt_allows_everything(fake_web) -> None:
    fake_web.add(ARTICLE, "<html><p>Text</p></html>")

    response = fake_web.client().get(ARTICLE)

    assert response.status == 200
    assert "Text" in response.text


def test_robots_check_can_be_skipped_for_documented_apis(fake_web) -> None:
    fake_web.add("https://api.example/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain")
    fake_web.add("https://api.example/data.json", {"ok": True})

    assert fake_web.client().get("https://api.example/data.json", respect_robots=False).json() == {"ok": True}


@pytest.mark.parametrize("status", [401, 403])
def test_refusals_raise_blocked(fake_web, status: int) -> None:
    fake_web.add(ARTICLE, "Forbidden", status=status)

    with pytest.raises(BlockedError):
        fake_web.client().get(ARTICLE)


def test_cloudflare_challenge_raises_blocked(fake_web) -> None:
    fake_web.add(ARTICLE, "<html><title>Just a moment...</title></html>", status=503,
                 headers={"cf-mitigated": "challenge"})

    with pytest.raises(BlockedError):
        fake_web.client().get(ARTICLE)


def test_long_article_that_mentions_a_captcha_is_not_blocked() -> None:
    body = ("<html><body>" + "<p>Ein langer Artikel über Captchas und g-recaptcha.</p>" * 1000 + "</body></html>").encode()
    assert not is_challenge(200, {"content-type": "text/html"}, body)


def test_not_found_raises_not_found(fake_web) -> None:
    with pytest.raises(NotFoundError):
        fake_web.client().get(ARTICLE)


def test_server_errors_are_retried(fake_web) -> None:
    fake_web.add(ARTICLE, "error", status=502)
    client = fake_web.client()

    with pytest.raises(FetchError) as excinfo:
        client.get(ARTICLE)

    assert excinfo.value.status == 502
    assert fake_web.requested.count(ARTICLE) == 3


def test_network_errors_become_fetch_errors(fake_web) -> None:
    fake_web.fail(ARTICLE)

    with pytest.raises(FetchError):
        fake_web.client().get(ARTICLE)


def test_unreachable_robots_txt_is_requested_once_per_run(fake_web) -> None:
    fake_web.fail("https://news.example/robots.txt")
    client = fake_web.client()

    for url in (ARTICLE, "https://news.example/politik/artikel-2"):
        with pytest.raises(FetchError):
            client.get(url)

    assert fake_web.requested.count("https://news.example/robots.txt") == 1
    assert ARTICLE not in fake_web.requested


def test_tdmrep_rules_are_matched_by_path(fake_web) -> None:
    fake_web.add("https://news.example/.well-known/tdmrep.json",
                 [{"location": "/politik/*", "tdm-reservation": 1}, {"location": "/", "tdm-reservation": 0}])
    client = fake_web.client()

    assert client.tdm_reserved(ARTICLE)
    assert not client.tdm_reserved("https://news.example/sport/artikel-2")


def test_tdm_reservation_header_is_respected(fake_web) -> None:
    fake_web.add(ARTICLE, "<html></html>", headers={"tdm-reservation": "1"})
    client = fake_web.client()

    response = client.get(ARTICLE)

    assert client.tdm_reserved(ARTICLE, response)


def test_host_with_an_excessive_crawl_delay_is_not_fetched(fake_web) -> None:
    fake_web.add("https://news.example/robots.txt", "User-agent: *\nCrawl-delay: 180\n", content_type="text/plain")
    fake_web.add(ARTICLE, "<html></html>")

    with pytest.raises(RobotsDisallowedError):
        fake_web.client().get(ARTICLE)
    assert ARTICLE not in fake_web.requested


def test_crawl_delay_extends_the_host_interval(fake_web) -> None:
    fake_web.add("https://news.example/robots.txt", "User-agent: *\nCrawl-delay: 5\n", content_type="text/plain")
    fake_web.add(ARTICLE, "<html></html>")
    waits: list[float] = []
    client = fake_web.client()
    client.sleep = waits.append
    client.clock = lambda: 100.0

    client.get(ARTICLE)
    client.get(ARTICLE)

    assert waits and max(waits) == pytest.approx(5.0)
