"""Tests of the README overview generator."""
from datetime import datetime, timezone

import pytest

from conftest import FIXTURES_DIR
from generate_dataset_overview import END_MARKER, START_MARKER, build_overview, update_readme
from state import append_run_log
from storage import dumps_records, partition_file

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)


def write(data_dir, dataset: str, records: list[dict]) -> None:
    path = partition_file(data_dir, dataset, NOW.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_records(records), encoding="utf-8")


def test_build_overview_counts_records_and_annotations(tmp_path) -> None:
    data_dir = tmp_path / "data"
    write(data_dir, "polls", [{"record_id": "dawum:1", "observed_at": "2026-10-20T01:00:00Z"},
                              {"record_id": "dawum:1", "revision": 1, "observed_at": "2026-10-20T01:00:00Z"}])
    write(data_dir, "news_items", [{"record_id": "news:a", "observed_at": "2026-10-20T01:00:00Z",
                                    "source_id": "news_example", "published_at": "2026-10-19T22:00:00Z",
                                    "content_basis": "lead_text", "keywords": ["Haushalt", "SPD"]},
                                   {"record_id": "news:b", "observed_at": "2026-10-20T01:00:00Z",
                                    "source_id": "news_example", "content_basis": "title_only", "keywords": []}])
    write(data_dir, "text_annotations", [{"record_id": "annotation:news:a:x", "item_id": "news:a",
                                          "observed_at": "2026-10-20T01:00:00Z", "is_political": True,
                                          "cap_topic": "Health"}])
    append_run_log(data_dir / "state", {"started": "2026-10-20T00:07:00+00:00", "mode": "normal",
                                        "collectors": {"polls": {"status": "ok", "records": {"polls": 2}}}})

    overview = build_overview(data_dir, FIXTURES_DIR / "feeds" / "sources.toml", now=NOW)

    assert "| `polls` | 2 | 1 | 1 | 2026-10-20 to 2026-10-20 |" in overview
    assert "**Annotated items:** 1 of 2 (50.0%)" in overview
    assert "| Health | 1 | 100.0% |" in overview
    assert "- **News outlets:** 2 configured (2 active)" in overview
    assert "`polls`: ok (polls 2)" in overview
    assert ("| News Example | national daily | active | 2 | 2 | 50.0% (1) | 50.0% (1) | 2026-10-19 |"
            in overview)
    assert "<summary><code>news_items</code>:" in overview
    assert "| `keywords` | Keywords from the article page or the news sitemap. | 50.0% (1) | 2 |" in overview
    assert "<summary><code>roll_call_votes</code>" not in overview


def test_update_readme_replaces_only_the_marked_section(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n{START_MARKER}\nold\n{END_MARKER}\nrest\n", encoding="utf-8")

    assert update_readme(readme, "new") is True
    assert readme.read_text(encoding="utf-8") == f"# Title\n{START_MARKER}\nnew\n{END_MARKER}\nrest\n"
    assert update_readme(readme, "new") is False


def test_update_readme_requires_markers(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_readme(readme, "x")


def test_removed_sources_are_counted_as_removed(tmp_path) -> None:
    config = (FIXTURES_DIR / "feeds" / "sources.toml").read_text(encoding="utf-8").replace(
        'id = "regional_example"', 'id = "regional_example"\nremoved = 2026-10-01')
    config_path = tmp_path / "sources.toml"
    config_path.write_text(config, encoding="utf-8")

    overview = build_overview(tmp_path / "data", config_path, now=NOW)

    assert "- **News outlets:** 2 configured (1 active, 1 removed)" in overview
    assert "| Regional Example | public broadcaster regional | removed | 0 |" in overview


def test_overview_without_data(tmp_path) -> None:
    overview = build_overview(tmp_path / "data", FIXTURES_DIR / "feeds" / "sources.toml", now=NOW)
    assert "- No run yet." in overview
    assert "| `news_items` | 0 | 0 | 0 | none |" in overview
