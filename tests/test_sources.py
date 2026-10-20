"""Tests of the source configuration and its lifecycle keys."""
from datetime import date
from pathlib import Path

import pytest

from sources import CONFIG_PATH, SourceHealth, SourceHealthStore, load_config

ENTRY = """\
[[outlet]]
id = "news_example"
name = "News Example"
homepage = "https://news.example/"
outlet_type = "national_daily"
owner_group = "Example Group"
paywall_model = "mixed"
feeds = ["https://news.example/rss"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_lifecycle_keys_and_exclusions_are_read(tmp_path) -> None:
    text = (ENTRY + "added = 2026-10-19\nremoved = 2027-02-01\nremoved_reason = \"blocked\"\nkeep = true\n\n"
            "[[excluded]]\nid = \"faz\"\nname = \"FAZ\"\nhomepage = \"https://www.faz.net/\"\n"
            "status = \"tdm_reserved\"\nreason = \"TDMRep\"\nchecked = 2026-10-19\n")

    config = load_config(write(tmp_path, text))

    outlet = config.outlets[0]
    assert outlet.added == date(2026, 10, 19) and outlet.removed == date(2027, 2, 1)
    assert outlet.removed_reason == "blocked" and outlet.keep is True
    assert not outlet.active and config.active_sources == ()
    assert config.excluded[0].status == "tdm_reserved"
    assert config.excluded[0].is_permanent_or_recent(date(2030, 1, 1))


def test_expiring_exclusion_is_checked_again_after_a_year(tmp_path) -> None:
    text = ENTRY + ("\n[[excluded]]\nid = \"blog\"\nname = \"Blog\"\nhomepage = \"https://blog.example/\"\n"
                    "status = \"no_feed\"\nreason = \"no feed\"\nchecked = 2026-10-19\n")

    entry = load_config(write(tmp_path, text)).excluded[0]

    assert entry.is_permanent_or_recent(date(2027, 10, 18))
    assert not entry.is_permanent_or_recent(date(2027, 10, 19))


@pytest.mark.parametrize("line", ['added = "2026-10-19"', "added = 2026-10-19T10:00:00Z"])
def test_lifecycle_dates_must_be_dates(tmp_path, line: str) -> None:
    with pytest.raises(ValueError, match="must be a date"):
        load_config(write(tmp_path, ENTRY + line + "\n"))


def test_unknown_exclusion_status_and_duplicate_ids_are_rejected(tmp_path) -> None:
    excluded = ("\n[[excluded]]\nid = \"{id}\"\nname = \"X\"\nhomepage = \"https://x.example/\"\n"
                "status = \"{status}\"\nreason = \"r\"\nchecked = 2026-10-19\n")
    with pytest.raises(ValueError, match="status"):
        load_config(write(tmp_path, ENTRY + excluded.format(id="x", status="unknown")))
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(write(tmp_path, ENTRY + excluded.format(id="news_example", status="blocked")))


def test_removed_sources_have_the_status_removed(tmp_path) -> None:
    config = load_config(write(tmp_path, ENTRY + "removed = 2027-02-01\n"))
    health = SourceHealthStore(tmp_path / "sources.json", {"news_example": SourceHealth(status="blocked")})

    assert health.status_of(config.outlets[0]) == "removed"


def test_repository_configuration_has_an_added_date_for_every_source() -> None:
    config = load_config(CONFIG_PATH)

    assert all(source.added is not None for source in config.sources)
    assert {entry.id for entry in config.excluded} >= {"faz", "zeit"}
