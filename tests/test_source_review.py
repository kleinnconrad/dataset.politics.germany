"""Tests of the monthly source review with a fake web and a fake search model."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import source_review
from conftest import FakeWeb
from entities import Mandate, Politician, save_politicians
from settings import Settings
from source_review import (
    Change,
    Gaps,
    NewEntry,
    SearchError,
    SearchModel,
    apply_changes,
    build_report,
    check_feed,
    count_items,
    feed_links,
    find_gaps,
    parse_json_array,
    run_review,
    toml_value,
)
from sources import SourceHealth, SourceHealthStore, load_config
from storage import dumps_records, partition_file

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(days=1)

CONFIG = """\
# Test sources of the source review.

# feeds: 10 h
[[outlet]]
id = "news_example"
name = "News Example"
homepage = "https://news.example/"
outlet_type = "national_daily"
owner_group = "Example Group"
paywall_model = "mixed"
feeds = ["https://news.example/politik/index.rss", "https://news.example/old.rss", "https://news.example/gone.rss"]
news_sitemap = "https://news.example/sitemap-news.xml"
fetch_pages = true
added = 2026-01-05

[[outlet]]
id = "daily_example"
name = "Daily Example"
homepage = "https://daily.example/"
outlet_type = "regional_daily"
owner_group = "Example Verlag"
home_state = "DE-SN"
paywall_model = "free"
feeds = ["https://daily.example/rss"]
fetch_pages = true
added = 2026-01-05

[[outlet]]
id = "quiet_example"
name = "Quiet Example"
homepage = "https://quiet.example/"
outlet_type = "regional_daily"
owner_group = "Quiet Verlag"
home_state = "DE-TH"
paywall_model = "free"
feeds = ["https://quiet.example/rss"]
added = 2026-01-05

[[outlet]]
id = "kept_example"
name = "Kept Example"
homepage = "https://kept.example/"
outlet_type = "online_only"
owner_group = "Kept Media"
paywall_model = "free"
feeds = ["https://kept.example/rss"]
added = 2026-01-05
keep = true

[[press_source]]
id = "spd_example"
name = "SPD"
homepage = "https://party.example/"
actor_type = "party"
party = "SPD"
jurisdiction = "DE"
feeds = ["https://party.example/presse.rss"]
fetch_pages = false
added = 2026-01-05

[[press_source]]
id = "blocked_example"
name = "Blocked Ministry"
homepage = "https://blocked.example/"
actor_type = "federal_ministry"
jurisdiction = "DE"
feeds = ["https://blocked.example/rss"]
fetch_pages = true
added = 2026-01-05

# Candidates that were checked and not admitted.
[[excluded]]
id = "excluded_example"
name = "Excluded Example"
homepage = "https://excluded.example/"
status = "blocked"
reason = "blocks automated requests"
checked = 2026-01-05
"""


def rss(items: list[tuple[str, str, datetime]]) -> str:
    """Builds an RSS feed from (title, link, published) tuples."""
    entries = "".join(f"<item><title>{title}</title><link>{link}</link><pubDate>{format_datetime(published)}"
                      f"</pubDate></item>" for title, link, published in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


def political_feed(host: str, count: int = 6, published: datetime = RECENT) -> str:
    return rss([(f"Landtag beschließt Gesetz {n}", f"https://{host}/politik/artikel-{n}.html", published)
                for n in range(count)])


def news_sitemap(host: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>'
            f"<loc>https://{host}/politik/a.html</loc><news:news><news:publication><news:name>N</news:name>"
            f"<news:language>de</news:language></news:publication><news:publication_date>{RECENT.isoformat()}"
            "</news:publication_date><news:title>Koalition streitet</news:title></news:news></url></urlset>")


def serve_sources(fake_web: FakeWeb) -> None:
    """Serves the feeds of the configured test sources."""
    fake_web.add("https://news.example/robots.txt",
                 "# Die Nutzung für Text- und Data-Mining (§ 44b UrhG) ist vorbehalten.\nUser-agent: *\nAllow: /\n",
                 content_type="text/plain")
    fake_web.add("https://news.example/politik/index.rss", political_feed("news.example"),
                 content_type="application/rss+xml")
    fake_web.add("https://news.example/old.rss", political_feed("news.example", 2, NOW - timedelta(days=400)),
                 content_type="application/rss+xml")
    fake_web.add("https://news.example/sitemap-news.xml", news_sitemap("news.example"), content_type="application/xml")
    fake_web.add("https://daily.example/robots.txt", "User-agent: *\nCrawl-delay: 30\n", content_type="text/plain")
    fake_web.add("https://daily.example/rss", "", status=301, headers={"location": "https://daily.example/feed.xml"})
    fake_web.add("https://daily.example/feed.xml", political_feed("daily.example"), content_type="application/rss+xml")
    fake_web.add("https://kept.example/rss", political_feed("kept.example"), content_type="application/rss+xml")
    fake_web.add("https://party.example/presse.rss", political_feed("party.example", 2),
                 content_type="application/rss+xml")


def serve_candidates(fake_web: FakeWeb) -> None:
    """Serves the websites of the candidates that the fake search returns."""
    fake_web.add("https://ministry.example/", "<html><body>Ministerium</body></html>")
    fake_web.add("https://zeitung.example/", "<html><body>Zeitung</body></html>")
    fake_web.add("https://ministry.example/presse.rss", political_feed("ministry.example", 3),
                 content_type="application/rss+xml")
    fake_web.add("https://office.example/", "Forbidden", status=403)
    fake_web.add("https://group.example/", '<html><head><link rel="alternate" type="application/rss+xml" '
                 'href="/presse/feed.xml"></head><body></body></html>')
    fake_web.add("https://group.example/presse/feed.xml", political_feed("group.example", 4),
                 content_type="application/rss+xml")
    fake_web.add("https://zeitung.example/robots.txt", "# Kein Text und Data Mining\nUser-agent: *\nAllow: /\n",
                 content_type="text/plain")
    fake_web.add("https://zeitung.example/politik/rss", political_feed("zeitung.example", 6),
                 content_type="application/rss+xml")
    fake_web.add("https://reserved.example/.well-known/tdmrep.json", [{"location": "/", "tdm-reservation": 1}])


class FakeSearch:
    """Search double that answers each category with a prepared JSON text."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.calls = 0

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        for marker, answer in self.answers.items():
            if marker in prompt:
                return answer if isinstance(answer, str) else json.dumps(answer)
        return "[]"


ANSWERS = {
    "federal government of Germany": [
        {"name": "Bundesministerium für Beispiele", "homepage": "https://ministry.example/",
         "feeds": ["https://ministry.example/presse.rss"], "actor_type": "federal_ministry"},
        {"name": "SPD", "homepage": "https://party.example/", "feeds": [], "actor_type": "federal_ministry"},
        {"name": "Amt für Sperren", "homepage": "https://office.example/", "feeds": [], "actor_type": "federal_ministry"},
        {"name": "Ausgeschlossen", "homepage": "https://excluded.example/", "feeds": [], "actor_type": "federal_ministry"},
    ],
    "parties and parliamentary groups": [
        {"name": "CDU/CSU-Fraktion im Deutschen Bundestag", "homepage": "https://group.example/", "feeds": [],
         "actor_type": "parliamentary_group", "party": "CDU_CSU"},
    ],
    "governments of these German states": "I could not find a list.",
    "regional daily newspapers": "```json\n" + json.dumps([
        {"name": "Neue Zeitung", "homepage": "https://zeitung.example/", "feeds": ["https://zeitung.example/politik/rss"],
         "home_state": "DE-BB", "owner_group": "Zeitungsverlag Beispiel", "paywall_model": "mixed"},
        {"name": "Vorbehalt-Kurier", "homepage": "https://reserved.example/", "feeds": [], "home_state": "DE-BB",
         "owner_group": "Vorbehalt Verlag", "paywall_model": "hard"},
    ]) + "\n```",
}


@pytest.fixture
def review_setup(tmp_path: Path, fake_web: FakeWeb) -> tuple[Settings, Path]:
    """Writes the test configuration, the state and recent records of three sources."""
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    settings = Settings(output_dir=tmp_path / "data")
    health = SourceHealthStore(settings.state_dir / "sources.json", {
        "blocked_example": SourceHealth(status="blocked", last_attempt=NOW.date() - timedelta(days=10),
                                        last_error="blocked (HTTP 403): https://blocked.example/rss"),
    })
    health.save()
    save_politicians(settings.state_dir, {
        "1": Politician(name="Anna Union", party="CDU", mandates=[Mandate(parliament="Bundestag",
                                                                         period="Bundestag 2025 - 2029",
                                                                         faction="CDU_CSU")]),
        "2": Politician(name="Bernd Rot", party="SPD", mandates=[Mandate(parliament="Bundestag",
                                                                        period="Bundestag 2025 - 2029",
                                                                        faction="SPD")]),
        "3": Politician(name="Clara Grün", party="GRUENE", mandates=[Mandate(parliament="Landtag Sachsen",
                                                                            period="Sachsen 2024 - 2029",
                                                                            faction="GRUENE")]),
    })
    for dataset, source_id in (("news_items", "news_example"), ("news_items", "daily_example"),
                               ("press_releases", "spd_example")):
        path = partition_file(settings.output_dir, dataset, (NOW - timedelta(days=10)).date())
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(dumps_records([*existing, {"record_id": f"x:{source_id}", "source_id": source_id}]),
                        encoding="utf-8")
    serve_sources(fake_web)
    serve_candidates(fake_web)
    return settings, config_path


def changes_by(result: Any) -> dict[tuple[str, str], Change]:
    return {(change.action, change.source_id): change for change in result.changes
            if change.action not in ("remove_feed",)}


def test_review_proposes_removals_fixes_additions_and_exclusions(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    search = FakeSearch(ANSWERS)

    text, result = run_review(settings, config_path, fake_web.client(), search, NOW)

    found = changes_by(result)
    assert "no political items" in found[("remove_source", "quiet_example")].reason
    assert found[("remove_source", "blocked_example")].reason.startswith("blocked since")
    assert ("remove_source", "kept_example") not in found
    removed_feeds = {change.old: change.reason for change in result.changes if change.action == "remove_feed"}
    assert removed_feeds["https://news.example/old.rss"].startswith("newest item from")
    assert "404" in removed_feeds["https://news.example/gone.rss"]
    assert "natural language" in found[("set_fetch_pages", "news_example")].reason
    assert found[("replace_feed", "daily_example")].new == "https://daily.example/feed.xml"
    assert "crawl delay 30 s" in found[("set_fetch_pages", "daily_example")].reason
    # Hosts that blocked requests and sources without yield are not requested
    assert not any("blocked.example" in url or "quiet.example" in url for url in fake_web.requested)
    assert not any("excluded.example" in url for url in fake_web.requested)

    assert result.gaps.bundestag_groups == ("CDU_CSU", "SPD")
    assert result.gaps.parties == ("CDU", "GRUENE")
    assert "DE-SN" not in result.gaps.regional_dailies and "DE-TH" in result.gaps.regional_dailies
    assert result.searches == ["federal", "groups", "states", "dailies"]
    assert any("states" in note and "no usable list" in note for note in result.notes)

    added = {change.source_id: change.entry for change in result.changes if change.action == "add_source"}
    assert set(added) == {"ministry", "cdu_csu_fraktion", "zeitung"}
    assert added["cdu_csu_fraktion"].fields["feeds"] == ["https://group.example/presse/feed.xml"]
    assert added["zeitung"].fields["fetch_pages"] is False
    assert added["ministry"].fields["fetch_pages"] is True
    excluded = {change.entry.fields["homepage"]: change.entry.fields for change in result.changes
                if change.action == "exclude"}
    assert excluded["https://office.example/"]["status"] == "blocked"
    assert excluded["https://reserved.example/"]["status"] == "tdm_reserved"
    # Exclusions keep the attributes, so that a later review can check them again
    assert excluded["https://office.example/"]["kind"] == "press_source"
    assert excluded["https://office.example/"]["actor_type"] == "federal_ministry"
    assert excluded["https://reserved.example/"]["home_state"] == "DE-BB"

    config = source_review.validate_config_text(text)
    by_id = {source.id: source for source in config.sources}
    assert by_id["quiet_example"].removed == NOW.date() and not by_id["quiet_example"].active
    assert by_id["news_example"].feeds == ("https://news.example/politik/index.rss",)
    assert by_id["news_example"].fetch_pages is False and by_id["news_example"].news_sitemap is not None
    assert by_id["daily_example"].feeds == ("https://daily.example/feed.xml",)
    assert by_id["cdu_csu_fraktion"].party == "CDU_CSU" and by_id["cdu_csu_fraktion"].added == NOW.date()
    assert by_id["zeitung"].home_state == "DE-BB" and by_id["zeitung"].outlet_type == "regional_daily"
    assert [entry.id for entry in config.excluded] == ["excluded_example", "office", "reserved"]
    # New outlets follow the last outlet, new press sources the last press source
    assert [source.id for source in config.outlets][-1] == "zeitung"
    assert [source.id for source in config.press_sources][-2:] == ["ministry", "cdu_csu_fraktion"]
    assert "# source review 2026-11-01: feed https://news.example/old.rss removed" in text

    report = build_report(result, load_config(config_path), NOW, search=True)
    for heading in ("### Removals", "### Feeds and page fetching", "### Additions", "### Exclusions", "### Gaps"):
        assert heading in report
    assert "| Quiet Example (`quiet_example`) | no political items in the last 90 days |" in report


def test_only_one_source_is_admitted_per_state_government(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    for host in ("stm.example", "landesportal.example"):
        fake_web.add(f"https://{host}/", "<html></html>")
        fake_web.add(f"https://{host}/presse.rss", political_feed(host, 2), content_type="application/rss+xml")
    answer = [{"name": f"Staatsministerium {n}", "homepage": f"https://{host}/", "feeds": [f"https://{host}/presse.rss"],
               "jurisdiction": "DE-BW"} for n, host in enumerate(("stm.example", "landesportal.example"))]

    _, result = run_review(settings, config_path, fake_web.client(), FakeSearch({"governments of these": answer}), NOW)

    added = [change.entry.fields for change in result.changes if change.action == "add_source"]
    assert [(entry["id"], entry["jurisdiction"]) for entry in added] == [("state_bw", "DE-BW")]
    assert "landesportal.example/presse.rss" not in " ".join(fake_web.requested)


def test_candidates_with_a_listed_feed_or_a_redirect_to_a_listed_host_are_duplicates(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    fake_web.add("https://renamed.example/", "<html></html>")
    fake_web.add("https://old-ministry.example/", "", status=301, headers={"location": "https://party.example/"})
    fake_web.add("https://party.example/", "<html></html>")
    answer = [
        {"name": "Umbenanntes Ministerium", "homepage": "https://renamed.example/",
         "feeds": ["https://party.example/presse.rss"], "actor_type": "federal_ministry"},
        {"name": "Altes Ministerium", "homepage": "https://old-ministry.example/", "feeds": [],
         "actor_type": "federal_ministry"},
    ]

    _, result = run_review(settings, config_path, fake_web.client(),
                           FakeSearch({"federal government of Germany": answer}), NOW)

    excluded = {change.entry.fields["homepage"]: change.entry.fields for change in result.changes
                if change.action == "exclude"}
    assert excluded["https://renamed.example/"]["status"] == "duplicate"
    assert excluded["https://renamed.example/"]["reason"] == "same feed as `spd_example`"
    assert excluded["https://old-ministry.example/"]["status"] == "duplicate"
    assert "redirects to party.example" in excluded["https://old-ministry.example/"]["reason"]
    assert not any(change.action == "add_source" for change in result.changes)


def test_excluded_candidates_are_checked_again_by_every_review(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    exclusion = ('\n[[excluded]]\nid = "{id}"\nname = "{name}"\nhomepage = "https://{host}/"\nkind = "press_source"\n'
                 'actor_type = "state_government"\njurisdiction = "{state}"\nstatus = "no_feed"\n'
                 'reason = "no readable feed found"\nchecked = 2026-10-01\n')
    config_path.write_text(CONFIG + "".join(exclusion.format(id=state_id, name=name, host=host, state=state)
                                            for state_id, name, host, state in (
        ("state_bw", "Staatsministerium Baden-Württemberg", "stm.example", "DE-BW"),
        ("state_he", "Hessische Staatskanzlei", "hessen.example", "DE-HE"),
        ("state_hb", "Senatskanzlei Bremen", "bremen.example", "DE-HB"),
    )), encoding="utf-8")
    fake_web.add("https://stm.example/", '<html><head><link rel="alternate" type="application/rss+xml" '
                 'href="/presse.rss"></head></html>')
    fake_web.add("https://stm.example/presse.rss", political_feed("stm.example", 2), content_type="application/rss+xml")
    fake_web.add("https://hessen.example/", "<html><body>Keine Feeds</body></html>")
    fake_web.add("https://bremen.example/", "Forbidden", status=403)

    text, result = run_review(settings, config_path, fake_web.client(), None, NOW)

    assert result.rechecked == 3
    added = [change.entry.fields for change in result.changes if change.action == "add_source"]
    assert [(entry["id"], entry["jurisdiction"]) for entry in added] == [("state_bw", "DE-BW")]
    config = source_review.validate_config_text(text)
    by_id = {entry.id: entry for entry in config.excluded}
    assert "state_bw" not in by_id and config.press_sources[-1].id == "state_bw"
    # An unchanged result leaves the entry as it is; a new reason updates it
    assert by_id["state_he"].checked == datetime(2026, 10, 1).date()
    assert by_id["state_hb"].status == "blocked" and by_id["state_hb"].checked == NOW.date()


def test_review_without_search_changes_only_existing_sources(review_setup, fake_web) -> None:
    settings, config_path = review_setup

    text, result = run_review(settings, config_path, fake_web.client(), None, NOW)

    assert result.searches == [] and result.candidates == 0
    assert not any(change.action in ("add_source", "exclude") for change in result.changes)
    assert "search for new sources was turned off" in build_report(result, load_config(config_path), NOW, False)


def test_unchanged_review_keeps_the_text(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    assert apply_changes(CONFIG, load_config(config_path), [], NOW.date()) == CONFIG


def test_apply_changes_keeps_other_entries_byte_identical(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    changes = [Change("remove_source", "quiet_example", 'no "items"')]

    text = apply_changes(CONFIG, load_config(config_path), changes, NOW.date())

    assert 'removed_reason = "no \\"items\\""' in text
    assert text.replace('removed = 2026-11-01\nremoved_reason = "no \\"items\\""\n', "") == CONFIG
    assert source_review.validate_config_text(text).outlets[2].removed_reason == 'no "items"'


def test_dropped_exclusion_is_removed_from_the_configuration(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG.replace('status = "blocked"', 'status = "no_feed"'), encoding="utf-8")
    config = load_config(config_path)
    entry = NewEntry("press_source", {"id": "excluded_ministry", "name": "Ministerium", "homepage": "https://x.example/",
                                      "actor_type": "federal_ministry", "jurisdiction": "DE",
                                      "feeds": ["https://x.example/rss"], "fetch_pages": True, "added": NOW.date()})
    changes = [Change("add_source", "excluded_ministry", "admitted", entry=entry),
               Change("drop_exclusion", "excluded_example", "admitted")]

    text = apply_changes(config_path.read_text(encoding="utf-8"), config, changes, NOW.date())

    parsed = source_review.validate_config_text(text)
    assert parsed.excluded == ()
    assert parsed.press_sources[-1].id == "excluded_ministry"


def test_find_gaps_counts_sources_that_leave_as_missing(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    config = load_config(config_path)
    politicians = {"2": Politician(name="B", party="SPD", mandates=[Mandate(parliament="Landtag Hessen",
                                                                        period="Hessen", faction="SPD")])}

    assert find_gaps(config, politicians).parties == ()
    gaps = find_gaps(config, politicians, frozenset({"spd_example", "daily_example"}))
    assert gaps.parties == ("SPD",)
    assert "DE-SN" in gaps.regional_dailies


def test_count_items_reads_only_recent_partitions(tmp_path) -> None:
    for day, source_id in ((NOW.date() - timedelta(days=100), "old"), (NOW.date() - timedelta(days=5), "new")):
        path = partition_file(tmp_path, "news_items", day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps_records([{"record_id": "r", "source_id": source_id}]), encoding="utf-8")

    assert count_items(tmp_path, NOW.date() - timedelta(days=90)) == {"new": 1}


def test_check_feed_reports_redirects_and_errors(fake_web) -> None:
    serve_sources(fake_web)
    config = source_review.validate_config_text(CONFIG)
    web = fake_web.client()

    moved = check_feed(web, "https://daily.example/rss", config.outlets[1])
    missing = check_feed(web, "https://news.example/gone.rss", config.outlets[0])

    assert moved.outcome == "ok" and moved.moved_to == "https://daily.example/feed.xml" and len(moved.entries) == 6
    assert missing.outcome == "not_found"


def test_feed_links_and_json_answers() -> None:
    html = ('<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml">'
            '<link rel="stylesheet" href="/a.css"><link rel="alternate" type="application/atom+xml" '
            'href="https://other.example/atom"></head></html>')
    assert feed_links(html, "https://site.example/start") == ["https://site.example/rss.xml",
                                                              "https://other.example/atom"]
    assert parse_json_array('Here: ```json\n[{"a": 1}]\n```') == [{"a": 1}]
    with pytest.raises(ValueError):
        parse_json_array("no list")
    assert toml_value(["a", 'b"c']) == '["a", "b\\"c"]'
    assert toml_value(NOW.date()) == "2026-11-01"


def test_search_model_stops_at_the_budget_and_wraps_errors() -> None:
    answers = iter([SimpleNamespace(text="[]", usage_metadata=None)] * 10)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: next(answers)))
    model = SearchModel(Settings(gemini_api_key="key"), client=client, sleep=lambda seconds: None)

    for _ in range(source_review.MAX_SEARCH_CALLS):
        assert model.ask("prompt") == "[]"
    with pytest.raises(SearchError):
        model.ask("prompt")


def test_report_lists_gaps_and_notes(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    result = source_review.ReviewResult(gaps=Gaps(bundestag_groups=("CDU_CSU",), state_governments=("DE-BW",)),
                                        notes=["`news_example`: feed x: HTTP 500"])

    report = build_report(result, load_config(config_path), NOW, search=True)

    assert "**Bundestag groups without a press source:** CDU/CSU (CDU_CSU)" in report
    assert "**States without a state government source:** Baden-Württemberg" in report
    assert "- `news_example`: feed x: HTTP 500" in report
    many = source_review.ReviewResult(notes=[f"note {n}" for n in range(source_review.MAX_REPORT_NOTES + 3)])
    assert "- 3 further notes are in the workflow log" in build_report(many, load_config(config_path), NOW, True)


def test_main_writes_the_configuration_and_the_report(review_setup, fake_web, monkeypatch) -> None:
    settings, config_path = review_setup
    monkeypatch.setattr(source_review, "WebClient", fake_web.client)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = config_path.parent / "report.md"

    exit_code = source_review.main(["--report", str(report), "--config", str(config_path),
                                    "--output-dir", str(settings.output_dir), "--no-search"])

    assert exit_code == 0
    assert "## Source review" in report.read_text(encoding="utf-8")
    assert "removed = " in config_path.read_text(encoding="utf-8")
