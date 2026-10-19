"""Tests of the command-line checks of the entry point."""
import pytest

from main import main


def test_backfill_rejects_the_feeds_collector(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("COLLECTORS", raising=False)
    assert main(["--backfill", "--collectors", "feeds", "--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "state").exists()


def test_unknown_collector_in_the_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COLLECTORS", "feeds,unknown")
    assert main(["--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "state").exists()
