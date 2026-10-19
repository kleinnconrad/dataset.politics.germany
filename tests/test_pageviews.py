"""Tests of the Wikipedia pageviews collector."""
from datetime import date

import pytest

import collectors.pageviews as pageviews
from collectors.pageviews import PageviewsCollector, resolve_wikipedia_titles
from conftest import read_dataset
from entities import Party, Politician, save_politicians
from state import CollectorEntry


def views_response(days: list[tuple[str, int]]) -> dict:
    return {"items": [{"timestamp": f"{day.replace('-', '')}00", "views": views} for day, views in days]}


@pytest.fixture
def one_party(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restricts the party table to one party with a redirected candidate title."""
    monkeypatch.setattr(pageviews, "PARTIES", (Party("LINKE", "Die Linke", wikipedia_titles=("Die Linke (Partei)",)),))


def wiki_query(fake_web, titles_redirect: dict[str, str]) -> None:
    fake_web.add(pageviews.WIKIPEDIA_API_URL, {"query": {
        "redirects": [{"from": source, "to": target} for source, target in titles_redirect.items()],
        "pages": [{"title": target} for target in titles_redirect.values()],
    }})


def test_resolve_wikipedia_titles_follows_redirects_and_drops_missing(fake_web, make_ctx) -> None:
    fake_web.add(pageviews.WIKIPEDIA_API_URL, {"query": {
        "normalized": [{"from": "die Linke", "to": "Die linke"}],
        "redirects": [{"from": "Die linke", "to": "Die Linke"}],
        "pages": [{"title": "Die Linke"}, {"title": "Gibt es nicht", "missing": True}],
    }})

    resolved = resolve_wikipedia_titles(make_ctx(), ["die Linke", "Gibt es nicht"])

    assert resolved == {"die Linke": "Die Linke"}


def test_first_run_fetches_history_and_later_runs_continue(fake_web, make_ctx, one_party) -> None:
    wiki_query(fake_web, {"Die Linke (Partei)": "Die Linke"})
    history_url = pageviews._article_url("Die Linke", pageviews.PARTY_HISTORY_START, date(2026, 10, 19))
    fake_web.add(history_url, views_response([("2015-07-01", 10), ("2026-10-18", 500), ("2026-10-19", 700)]))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    PageviewsCollector().collect(ctx, entry)

    records = read_dataset(ctx.settings.output_dir, "wikipedia_pageviews")
    assert len(records) == 1
    record = records[0]
    assert record["entity_id"] == "LINKE" and record["wikipedia_title"] == "Die Linke"
    assert record["daily_views"] == {"2015-07-01": 10, "2026-10-18": 500, "2026-10-19": 700}
    assert record["backfilled"] is True
    assert record["record_id"] == "pageviews:LINKE:2015-07-01:2026-10-19"
    assert ctx.seen("wikipedia_pageviews").get("LINKE").data == {"last_day": "2026-10-19"}


def test_nothing_to_fetch_when_up_to_date(fake_web, make_ctx, one_party) -> None:
    wiki_query(fake_web, {"Die Linke (Partei)": "Die Linke"})
    ctx = make_ctx()
    ctx.seen("wikipedia_pageviews").mark("LINKE", "Die Linke", ctx.today, data={"last_day": "2026-10-19"})

    result = PageviewsCollector().collect(ctx, CollectorEntry(first_run=date(2026, 1, 1)))

    assert result.counters["records"] == 0
    assert not any("pageviews/per-article" in url for url in fake_web.requested)


def test_politicians_are_resolved_through_wikidata(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(pageviews, "PARTIES", ())
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, {"77": Politician(name="Erika Beispiel", wikidata_qid="Q42")})
    fake_web.add(pageviews.WIKIDATA_API_URL, {"entities": {"Q42": {"sitelinks": {"dewiki": {"title": "Erika Beispiel"}}}}})
    start = date(2026, 10, 20).fromordinal(date(2026, 10, 20).toordinal() - pageviews.POLITICIAN_HISTORY_DAYS)
    fake_web.add(pageviews._article_url("Erika Beispiel", start, date(2026, 10, 19)),
                 views_response([("2026-10-19", 42)]))

    PageviewsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    record = read_dataset(ctx.settings.output_dir, "wikipedia_pageviews")[0]
    assert record["entity_type"] == "politician" and record["wikidata_qid"] == "Q42"
    assert record["daily_views"] == {"2026-10-19": 42} and record["backfilled"] is False
