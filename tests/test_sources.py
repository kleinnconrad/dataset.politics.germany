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
    assert not config.excluded[0].rechecked


def test_exclusion_with_a_temporary_reason_and_attributes_is_rechecked(tmp_path) -> None:
    text = ENTRY + ("\n[[excluded]]\nid = \"state_bw\"\nname = \"Staatsministerium\"\n"
                    "homepage = \"https://stm.example/\"\nkind = \"press_source\"\nactor_type = \"state_government\"\n"
                    "jurisdiction = \"DE-BW\"\nfeeds = [\"https://stm.example/rss\"]\nstatus = \"no_feed\"\n"
                    "reason = \"no feed\"\nchecked = 2026-10-19\n\n[[excluded]]\nid = \"blog\"\nname = \"Blog\"\n"
                    "homepage = \"https://blog.example/\"\nstatus = \"no_feed\"\nreason = \"no feed\"\n"
                    "checked = 2026-10-19\n")

    with_attributes, without_kind = load_config(write(tmp_path, text)).excluded

    assert with_attributes.rechecked and with_attributes.feeds == ("https://stm.example/rss",)
    assert with_attributes.actor_type == "state_government" and with_attributes.jurisdiction == "DE-BW"
    assert not without_kind.rechecked
    with pytest.raises(ValueError, match="actor_type"):
        load_config(write(tmp_path, text.replace('"state_government"', '"ministry"')))


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
