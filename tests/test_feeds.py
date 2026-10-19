"""Tests of the feeds collector with a fake web and a fake annotator."""
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Optional
from xml.sax.saxutils import escape

import httpx
import pytest

from annotation import AnnotationInput, CallBudgetExceededError, clean_annotation
from collectors.base import CollectorSkipped, RunContext
from collectors.feeds import Candidate, FeedsCollector, interleave, looks_political, parse_feed, parse_news_sitemap
from conftest import FIXTURES_DIR, RUN_STARTED, read_dataset
from entities import Mandate, Politician, save_politicians
from schema import ItemAnnotation
from settings import Settings
from sources import SourceHealthStore, load_config
from state import CollectorEntry
from test_annotation import label

SOURCES = FIXTURES_DIR / "feeds" / "sources.toml"
RECENT = RUN_STARTED - timedelta(hours=5)


def rss(items: list[tuple[str, str, Optional[datetime], str]]) -> str:
    """Builds an RSS feed from (title, link, published, description) tuples."""
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
        + (f"<pubDate>{format_datetime(published)}</pubDate>" if published else "")
        + f"<description>{escape(description)}</description></item>"
        for title, link, published, description in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


def news_sitemap(items: list[tuple[str, str, datetime, str]]) -> str:
    """Builds a Google News sitemap from (title, link, published, keywords) tuples."""
    urls = "".join(
        f"<url><loc>{escape(link)}</loc><news:news><news:publication><news:name>N</news:name>"
        f"<news:language>de</news:language></news:publication>"
        f"<news:publication_date>{published.isoformat()}</news:publication_date>"
        f"<news:title>{escape(title)}</news:title><news:keywords>{escape(keywords)}</news:keywords></news:news></url>"
        for title, link, published, keywords in items
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">' + urls + "</urlset>")


class FakeAnnotator:
    """Annotator double that labels every item and records the inputs."""
    model = "fake-model"
    extraction_hash = "0123456789abcdef"

    def __init__(self, stop_after: Optional[int] = None) -> None:
        self.inputs: list[AnnotationInput] = []
        self.usage = type("Usage", (), {"calls": 0})()
        self._stop_after = stop_after

    def annotate(self, items: list[AnnotationInput]) -> list[Optional[ItemAnnotation]]:
        if self._stop_after is not None and len(self.inputs) >= self._stop_after:
            raise CallBudgetExceededError("MAX_GEMINI_CALLS_PER_RUN (1) reached")
        self.inputs.extend(items)
        return [clean_annotation(ItemAnnotation.model_validate(label(n)), item.kind, item.published)
                for n, item in enumerate(items, start=1)]


def setup_sources(fake_web: Any) -> None:
    fake_web.add("https://news.example/politik/index.rss", rss([
        ("Koalition streitet über den Haushalt", "https://news.example/politik/haushalt-1.html?utm_source=rss",
         RECENT, "Union und SPD ringen um den Haushalt."),
        ("Alter Artikel", "https://news.example/politik/alt.html", RUN_STARTED - timedelta(days=10), "alt"),
    ]), content_type="application/rss+xml")
    fake_web.add("https://news.example/sitemap-news.xml", news_sitemap([
        ("Koalition streitet über den Haushalt", "https://news.example/politik/haushalt-1.html", RECENT, "Haushalt"),
        ("Bundesliga: Spieltag", "https://news.example/sport/bundesliga.html", RECENT, "Fußball"),
        ("Ministerin stellt Gesetz vor", "https://news.example/panorama/gesetz.html", RECENT, "Haushalt, SPD"),
    ]), content_type="application/xml")
    fake_web.add_fixture("https://news.example/politik/haushalt-1.html", "pages/news_article.html")
    fake_web.add("https://news.example/panorama/gesetz.html", "<html><body><article><p>"
                 + "Die Ministerin stellte am Montag einen Gesetzentwurf vor. " * 3 + "</p></article></body></html>")
    fake_web.add("https://regional.example/nachrichten/rss", rss([
        ("Landtag beschließt Schulgesetz", "https://regional.example/nachrichten/schulgesetz-100.html", RECENT, ""),
        ("Wetter: Sonne am Wochenende", "https://regional.example/nachrichten/wetter-100.html", RECENT, ""),
    ]), content_type="application/rss+xml")
    fake_web.add("https://party.example/presse.rss", rss([
        ("SPD fordert stabile Renten", "https://party.example/presse/renten", RECENT, "Die SPD fordert ..."),
    ]), content_type="application/rss+xml")


def test_parse_feed_and_sitemap() -> None:
    config = load_config(SOURCES)
    outlet = config.outlets[0]
    feed = parse_feed(rss([("Titel", "https://news.example/politik/a.html", RECENT, "<p>Teaser</p>")]).encode(),
                      outlet, "https://news.example/politik/index.rss")
    assert feed[0].title == "Titel" and feed[0].teaser == "Teaser" and feed[0].from_politics_feed
    assert feed[0].published_at == RECENT

    sitemap, children = parse_news_sitemap(news_sitemap([("T", "https://news.example/x.html", RECENT, "Haushalt, SPD")]).encode(),
                                           outlet, "https://news.example/sitemap-news.xml")
    assert children == [] and sitemap[0].keywords == ["Haushalt", "SPD"] and sitemap[0].channel == "news_sitemap"


def test_central_european_zone_abbreviations_are_not_read_as_utc() -> None:
    outlet = load_config(SOURCES).outlets[0]
    feed = ('<rss version="2.0"><channel><item><title>T</title><link>https://news.example/politik/a</link>'
            "<pubDate>Mon, 19 Oct 2026 10:00:00 CEST</pubDate></item></channel></rss>")

    candidate = parse_feed(feed.encode(), outlet, "https://news.example/politik/index.rss")[0]

    assert candidate.published_at == datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)


def test_looks_political_filters_outlet_items_only() -> None:
    config = load_config(SOURCES)
    outlet, press = config.outlets[0], config.press_sources[0]

    def candidate(source: Any, url: str, title: str) -> Candidate:
        return Candidate(source=source, url=url, key=url, title=title, teaser="", published_at=None, channel="rss")

    assert looks_political(candidate(outlet, "https://news.example/politik/x.html", "Irgendwas"))
    assert looks_political(candidate(outlet, "https://news.example/panorama/x.html", "Neues Gesetz zur Rente"))
    assert not looks_political(candidate(outlet, "https://news.example/sport/x.html", "Bundesliga: Spieltag"))
    assert looks_political(candidate(press, "https://party.example/presse/x", "Sommerfest"))


def test_same_title_in_two_feeds_of_a_source_is_kept_once(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://regional.example/nachrichten/rss", rss([
        ("Landtag beschließt Schulgesetz", "https://regional.example/nachrichten/schulgesetz-100.html", RECENT, ""),
        ("Landtag beschließt  Schulgesetz", "https://regional.example/sachsen/schulgesetz-102.html", RECENT, ""),
    ]), content_type="application/rss+xml")
    ctx = make_ctx()

    FeedsCollector(FakeAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    regional = [item for item in read_dataset(ctx.settings.output_dir, "news_items")
                if item["outlet_id"] == "regional_example"]
    assert len(regional) == 1


def test_article_in_feeds_of_two_outlets_is_attributed_to_its_home_outlet(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://regional.example/nachrichten/rss", rss([
        ("Koalition streitet über den Haushalt", "https://news.example/politik/haushalt-1.html", RECENT, ""),
        ("Landtag beschließt Schulgesetz", "https://regional.example/nachrichten/schulgesetz-100.html", RECENT, ""),
    ]), content_type="application/rss+xml")
    ctx = make_ctx()

    result = FeedsCollector(FakeAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    news = read_dataset(ctx.settings.output_dir, "news_items")
    haushalt = [item for item in news if item["title"].startswith("Koalition")]
    assert [item["outlet_id"] for item in haushalt] == ["news_example"]
    assert len({item["record_id"] for item in news}) == len(news)
    assert result.counters["duplicates_across_sources"] >= 1


def test_interleave_takes_turns() -> None:
    assert list(interleave([["a1", "a2", "a3"], ["b1"], ["c1", "c2"]])) == ["a1", "b1", "c1", "a2", "c2", "a3"]


def test_collect_writes_items_annotations_and_resolves_politicians(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, {"123": Politician(
        name="Max Muster", first_name="Max", last_name="Muster", party="SPD",
        mandates=[Mandate(parliament="Bundestag", period="Bundestag 2025 - 2029", faction="SPD")])})
    annotator = FakeAnnotator()

    result = FeedsCollector(annotator, config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    news = read_dataset(ctx.settings.output_dir, "news_items")
    press = read_dataset(ctx.settings.output_dir, "press_releases")
    annotations = read_dataset(ctx.settings.output_dir, "text_annotations")
    titles = sorted(item["title"] for item in news)
    assert titles == ["Koalition streitet über den Haushalt", "Landtag beschließt Schulgesetz",
                      "Ministerin stellt Gesetz vor"]
    assert [item["title"] for item in press] == ["SPD fordert stabile Renten"]
    assert len(annotations) == 4
    haushalt = next(item for item in news if item["title"].startswith("Koalition"))
    assert haushalt["channel"] == "rss"
    assert haushalt["content_basis"] == "lead_text"
    assert haushalt["jsonld_type"] == "NewsArticle" and haushalt["is_paywalled"] is True
    assert haushalt["canonical_url"] == "https://news.example/politik/koalition-haushalt-100.html"
    regional = next(item for item in news if item["title"].startswith("Landtag"))
    assert regional["jurisdiction"] == "DE-SN" and regional["content_basis"] == "title_only"
    assert regional["jsonld_type"] is None
    annotation = next(a for a in annotations if a["item_id"] == haushalt["record_id"])
    assert annotation["mentioned_politicians"] == ["123"] and annotation["unresolved_person_count"] == 0
    assert annotation["party_tone"] == {"CDU_CSU": "Neutral", "SPD": "Negative"}
    assert annotation["quoted_parties"] == ["CDU_CSU"]
    assert annotation["conflict_pairs"] == [["CDU_CSU", "SPD"]]
    assert annotation["license"] == "Derived data (ODbL-1.0)"
    assert result.counters["annotated"] == 4 and result.error is None
    ministerin = next(item for item in news if item["title"].startswith("Ministerin"))
    assert ministerin["channel"] == "news_sitemap" and ministerin["keywords"] == ["Haushalt", "SPD"]
    assert ministerin["content_basis"] == "lead_text" and ministerin["is_agency_copy"] is None
    press_input = next(item for item in annotator.inputs if item.kind == "press release")
    assert press_input.source == "SPD (party)"


def test_second_run_skips_known_items(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()
    FeedsCollector(FakeAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))
    annotator = FakeAnnotator()

    result = FeedsCollector(annotator, config_path=SOURCES).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert annotator.inputs == []
    assert result.counters["items_selected"] == 0


def test_blocked_source_is_marked_and_skipped(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://party.example/presse.rss", "Forbidden", status=403)
    ctx = make_ctx()

    FeedsCollector(FakeAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    health = SourceHealthStore.load(ctx.settings.state_dir)
    assert health.get("spd_example").status == "blocked"
    assert not health.is_due("spd_example", ctx.today + timedelta(days=30))
    assert read_dataset(ctx.settings.output_dir, "press_releases") == []


def test_tdm_reservation_excludes_the_source(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://news.example/.well-known/tdmrep.json", [{"location": "/", "tdm-reservation": 1}])
    ctx = make_ctx()

    FeedsCollector(FakeAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    health = SourceHealthStore.load(ctx.settings.state_dir)
    assert health.get("news_example").status == "tdm_reserved"
    news = read_dataset(ctx.settings.output_dir, "news_items")
    assert all(item["outlet_id"] != "news_example" for item in news)


def test_budget_stop_writes_items_without_annotations(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()

    result = FeedsCollector(FakeAnnotator(stop_after=0), config_path=SOURCES).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert read_dataset(ctx.settings.output_dir, "text_annotations") == []
    assert result.counters["not_annotated"] == 4
    assert result.error is None


def test_cap_annotates_press_releases_first_and_stores_the_rest_without_labels(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx(max_annotations_per_run=2)
    annotator = FakeAnnotator()

    result = FeedsCollector(annotator, config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert [item.title for item in annotator.inputs] == ["Koalition streitet über den Haushalt",
                                                         "SPD fordert stabile Renten"]
    news = read_dataset(ctx.settings.output_dir, "news_items")
    annotations = read_dataset(ctx.settings.output_dir, "text_annotations")
    assert len(news) == 3 and len(read_dataset(ctx.settings.output_dir, "press_releases")) == 1
    assert len(annotations) == 2
    assert result.counters["items_selected"] == 2 and result.counters["beyond_cap"] == 2
    ministerin = next(item for item in news if item["title"].startswith("Ministerin"))
    assert ministerin["content_basis"] == "title_only" and ministerin["jsonld_type"] is None
    # Pages of items beyond the cap are not fetched
    assert "https://news.example/panorama/gesetz.html" not in fake_web.requested


def test_items_after_the_run_deadline_are_stored_without_labels(fake_web, tmp_path, monkeypatch) -> None:
    setup_sources(fake_web)
    now = [RUN_STARTED]
    serve = fake_web.handler

    def slow_page(request: httpx.Request) -> httpx.Response:
        # The first article page takes longer than the run deadline
        if request.url.path == "/politik/haushalt-1.html":
            now[0] += timedelta(minutes=30)
        return serve(request)

    monkeypatch.setattr(fake_web, "handler", slow_page)
    settings = replace(Settings(), output_dir=tmp_path / "data")
    ctx = RunContext(settings, fake_web.client(), RUN_STARTED, clock=lambda: now[0])
    annotator = FakeAnnotator()

    result = FeedsCollector(annotator, config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete
    assert [item.title for item in annotator.inputs] == ["Koalition streitet über den Haushalt"]
    assert result.counters["annotated"] == 1 and result.counters["not_annotated"] == 3
    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert len(read_dataset(ctx.settings.output_dir, "press_releases")) == 1
    assert "https://news.example/panorama/gesetz.html" not in fake_web.requested


class ParallelAnnotator(FakeAnnotator):
    """Annotator double whose first two calls wait for each other, so that they must run at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self._barrier = threading.Barrier(2, timeout=5)
        self._lock = threading.Lock()
        self._calls = 0

    def annotate(self, items: list[AnnotationInput]) -> list[Optional[ItemAnnotation]]:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call <= 2:
            self._barrier.wait()
        return super().annotate(items)


def test_batches_are_annotated_in_parallel_and_written_in_order(fake_web, make_ctx) -> None:
    titles = [f"Bundestag berät Gesetz {n}" for n in range(25)]
    fake_web.add("https://party.example/presse.rss", rss([
        (title, f"https://party.example/presse/{n}", RECENT - timedelta(minutes=n), "Text")
        for n, title in enumerate(titles)
    ]), content_type="application/rss+xml")
    ctx = make_ctx(gemini_concurrency=3)

    result = FeedsCollector(ParallelAnnotator(), config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["annotated"] == 25
    assert [item["title"] for item in read_dataset(ctx.settings.output_dir, "press_releases")] == titles
    assert len(read_dataset(ctx.settings.output_dir, "text_annotations")) == 25


def test_dry_run_reads_sources_without_writing(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx(dry_run=True)

    result = FeedsCollector(None, config_path=SOURCES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["items_selected"] == 4
    assert not (ctx.settings.output_dir / "news_items").exists()
    assert "https://news.example/politik/haushalt-1.html" not in fake_web.requested


def test_missing_annotator_skips_the_collector(make_ctx) -> None:
    with pytest.raises(CollectorSkipped):
        FeedsCollector(None, config_path=SOURCES).collect(make_ctx(), CollectorEntry(first_run=RUN_STARTED.date()))
