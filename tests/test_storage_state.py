"""Tests of the partitioned storage and the state files."""
import json
from datetime import date, datetime, timezone

from schema import PollRecord
from state import CollectorStates, SeenStore, content_hash, is_backfilled
from storage import dumps_records, iter_records, partition_file, write_records

DAY = date(2026, 10, 20)


def _poll(poll_id: str, revision: int = 0, afd: float = 25.0) -> PollRecord:
    return PollRecord(
        record_id=f"dawum:{poll_id}", revision=revision, dataset="polls",
        observed_at=datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc), source_id="dawum", source_url=None,
        license="ODbL-1.0", jurisdiction="DE", backfilled=False, poll_id=poll_id, parliament="Bundestag",
        institute="INSA", tasker=None, method="Online", fieldwork_start=None, fieldwork_end=None,
        published_date=DAY, sample_size=1000, results={"AFD": afd}, others_share=5.0,
    )


def test_partition_file_uses_hive_style_folders(tmp_path) -> None:
    path = partition_file(tmp_path, "polls", DAY)
    assert path == tmp_path / "polls" / "year=2026" / "month=10" / "polls_2026-10-20.json"


def test_dumps_records_writes_one_record_per_line() -> None:
    text = dumps_records([{"a": 1}, {"b": "ä"}])
    assert text == '[\n{"a":1},\n{"b":"ä"}\n]\n'
    assert json.loads(text) == [{"a": 1}, {"b": "ä"}]


def test_write_records_merges_by_record_id_and_revision(tmp_path) -> None:
    write_records(tmp_path, "polls", DAY, [_poll("1"), _poll("2")])
    write_records(tmp_path, "polls", DAY, [_poll("1", afd=26.0), _poll("1", revision=1, afd=27.0)])

    records = list(iter_records(tmp_path, "polls"))
    keys = sorted((r["poll_id"], r["revision"], r["results"]["AFD"]) for r in records)
    assert keys == [("1", 0, 26.0), ("1", 1, 27.0), ("2", 0, 25.0)]


def test_write_records_without_records_writes_nothing(tmp_path) -> None:
    assert write_records(tmp_path, "polls", DAY, []) is None
    assert not (tmp_path / "polls").exists()


def test_seen_store_classifies_and_counts_revisions(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "polls")
    assert store.classify("1", "a") == "new"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "a") == "unchanged"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "b") == "changed"
    assert store.mark("1", "b", DAY, data={"status": "x"}) == 1
    store.save()

    reloaded = SeenStore.load(tmp_path, "polls")
    entry = reloaded.get("1")
    assert entry is not None and entry.revision == 1 and entry.data == {"status": "x"}


def test_seen_store_prunes_old_entries(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "news_items")
    store.mark("old", "h", date(2026, 8, 1))
    store.mark("new", "h", date(2026, 10, 19))

    assert store.prune(DAY, retention_days=30) == 1
    assert "old" not in store and "new" in store


def test_collector_states_track_failures_and_cursor(tmp_path) -> None:
    states = CollectorStates.load(tmp_path)
    started = datetime(2026, 10, 20, 0, 7, tzinfo=timezone.utc)
    assert states.entry("polls", DAY).first_run == DAY
    states.record_failure("polls", started, "boom")
    states.record_failure("polls", started, "boom")
    assert states.entry("polls", DAY).consecutive_failures == 2
    states.record_success("polls", started, {"last_update": "x"})
    states.save()

    entry = CollectorStates.load(tmp_path).entry("polls", date(2027, 1, 1))
    assert entry.consecutive_failures == 0
    assert entry.cursor == {"last_update": "x"}
    assert entry.first_run == DAY


def test_is_backfilled_uses_a_grace_period() -> None:
    assert is_backfilled(date(2026, 10, 1), first_run=DAY)
    assert not is_backfilled(date(2026, 10, 15), first_run=DAY)
    assert not is_backfilled(None, first_run=DAY)


def test_content_hash_ignores_key_order() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
