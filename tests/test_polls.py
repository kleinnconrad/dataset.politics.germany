"""Tests of the DAWUM polls collector."""
import json
from datetime import date

from collectors.polls import DATABASE_URL, LAST_UPDATE_URL, PollsCollector, parse_survey
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry


def _database() -> dict:
    return json.loads((FIXTURES_DIR / "dawum" / "database.json").read_text(encoding="utf-8"))


def test_parse_survey_maps_parties_parliament_and_method() -> None:
    data = _database()
    fields = parse_survey("4352", data["Surveys"]["4352"], data)
    assert fields is not None
    assert fields["parliament"] == "Bundestag"
    assert fields["jurisdiction"] == "DE"
    assert fields["institute"] == "INSA"
    assert fields["method"] == "Telephone and online"
    assert fields["sample_size"] == 1206
    assert fields["fieldwork_end"] == date(2026, 10, 16)
    assert fields["results"]["AFD"] == 29
    assert fields["results"]["CDU_CSU"] == 18
    assert fields["results"]["BSW"] == 4
    assert "OTHER" not in fields["results"]
    assert fields["others_share"] == 7


def test_parse_survey_state_and_european_parliament() -> None:
    data = _database()
    state = parse_survey("4189", data["Surveys"]["4189"], data)
    europe = parse_survey("3480", data["Surveys"]["3480"], data)
    assert state is not None and state["parliament"] == "Landtag" and state["jurisdiction"] == "DE-SN"
    assert europe is not None and europe["parliament"] == "European Parliament" and europe["jurisdiction"] == "DE"


def test_parse_survey_rejects_unknown_parliament() -> None:
    data = _database()
    survey = dict(data["Surveys"]["4352"], Parliament_ID="99")
    assert parse_survey("1", survey, data) is None


def test_collect_writes_all_polls_once_and_marks_old_ones_backfilled(fake_web, make_ctx) -> None:
    fake_web.add(LAST_UPDATE_URL, "2026-10-17T16:08:32+02:00", content_type="text/plain")
    fake_web.add_fixture(DATABASE_URL, "dawum/database.json")
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    result = PollsCollector().collect(ctx, entry)

    records = read_dataset(ctx.settings.output_dir, "polls")
    assert result.counters["new"] == 4
    assert len(records) == 4
    by_id = {record["poll_id"]: record for record in records}
    assert by_id["365"]["backfilled"] is True
    assert by_id["4352"]["backfilled"] is False
    assert by_id["4352"]["license"] == "ODbL-1.0"
    assert by_id["4352"]["record_id"] == "dawum:4352"
    assert result.cursor == {"last_update": "2026-10-17T16:08:32+02:00"}


def test_collect_skips_download_when_database_unchanged(fake_web, make_ctx) -> None:
    fake_web.add(LAST_UPDATE_URL, "2026-10-17T16:08:32+02:00", content_type="text/plain")
    ctx = make_ctx()
    entry = CollectorEntry(first_run=date(2026, 10, 1), cursor={"last_update": "2026-10-17T16:08:32+02:00"})

    result = PollsCollector().collect(ctx, entry)

    assert result.counters == {"database_changed": False}
    assert DATABASE_URL not in fake_web.requested


def test_collect_writes_new_revision_for_corrected_poll(fake_web, make_ctx) -> None:
    fake_web.add(LAST_UPDATE_URL, "first", content_type="text/plain")
    fake_web.add_fixture(DATABASE_URL, "dawum/database.json")
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    PollsCollector().collect(ctx, entry)

    corrected = _database()
    corrected["Surveys"]["4352"]["Results"]["7"] = 28
    fake_web.add(LAST_UPDATE_URL, "second", content_type="text/plain")
    fake_web.add(DATABASE_URL, corrected)
    ctx2 = make_ctx()
    result = PollsCollector().collect(ctx2, entry.model_copy(update={"cursor": {"last_update": "first"}}))

    assert result.counters["new"] == 0
    assert result.counters["changed"] == 1
    revisions = sorted((r["revision"], r["results"]["AFD"]) for r in read_dataset(ctx.settings.output_dir, "polls")
                       if r["poll_id"] == "4352")
    assert revisions == [(0, 29.0), (1, 28.0)]
