"""Tests of the run summary of the notification workflow."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_summary.py"
_spec = importlib.util.spec_from_file_location("run_summary", SCRIPT)
assert _spec is not None and _spec.loader is not None
run_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_summary)

ENTRY = {
    "started": "2026-10-20T00:07:00+00:00", "finished": "2026-10-20T00:28:00+00:00", "mode": "normal",
    "requests": 1520,
    "collectors": {
        "politicians": {"status": "not_due"},
        "feeds": {"status": "ok", "records": {"news_items": 1200, "press_releases": 110, "text_annotations": 400},
                  "counters": {"items_new": 1310, "sources_ok": 63, "annotated": 400, "beyond_cap": 905,
                               "not_annotated": 5, "gemini_model": "gemini-2.5-flash",
                               "gemini_usage": {"calls": 40, "prompt_tokens": 100_000, "output_tokens": 92_000,
                                                "thinking_tokens": 0, "total_tokens": 192_000}}},
        "roll_call_votes": {"status": "failed", "error": "FetchError: HTTP 500"},
    },
}


def write_log(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "run_log.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def test_gemini_cost_uses_the_list_prices_of_the_model() -> None:
    usage = {"prompt_tokens": 1_000_000, "output_tokens": 600_000, "thinking_tokens": 400_000}
    assert run_summary.gemini_cost("gemini-2.5-flash", usage) == pytest.approx(0.30 + 2.50)
    assert run_summary.gemini_cost("other-model", usage) is None


def test_summary_lists_collectors_feeds_and_gemini_cost() -> None:
    text = run_summary.build_summary(ENTRY, "failure", "https://github.com/run/1", "owner")

    assert text.startswith("@owner German politics pipeline: **failure**")
    assert "duration 21 min" in text
    assert "| `politicians` | not_due |  |" in text
    assert "| `feeds` | ok | news_items 1,200, press_releases 110, text_annotations 400 |" in text
    assert "| `roll_call_votes` | failed (FetchError: HTTP 500) |  |" in text
    assert "400 annotated, 910 stored without labels" in text
    assert "estimated cost $0.26 at the list prices of gemini-2.5-flash" in text


def test_entry_of_an_earlier_run_is_ignored(tmp_path: Path) -> None:
    log = write_log(tmp_path, ENTRY)

    assert run_summary.last_entry(log) == ENTRY
    assert run_summary.last_entry(log, datetime(2026, 10, 21, tzinfo=timezone.utc)) is None
    assert run_summary.last_entry(tmp_path / "missing.jsonl") is None
    assert "wrote no run log entry" in run_summary.build_summary(None, "failure", None, None)


def test_main_writes_the_summary_file(tmp_path: Path) -> None:
    log = write_log(tmp_path, {"started": "2026-10-16T00:07:00+00:00", "mode": "normal", "collectors": {}}, ENTRY)
    output = tmp_path / "summary.md"

    assert run_summary.main(["--run-log", str(log), "--output", str(output), "--conclusion", "success",
                             "--not-before", "2026-10-20T00:06:30Z"]) == 0
    assert "German politics pipeline: **success**" in output.read_text(encoding="utf-8")
