"""
The curated news outlets and press-release sources and their health status.

The sources are listed by hand in ``config/sources.toml``. Their health is
kept in ``state/sources.json`` and follows fixed rules:

- ``blocked``: the host refused automated requests (HTTP 401/403, challenge
  page, CAPTCHA). The source is not fetched again until the entry is reset by hand.
- ``tdm_reserved``: the host declares a text-and-data-mining reservation
  (TDMRep). The source is not fetched again until the entry is reset by hand.
- ``broken``: the feeds failed on BROKEN_AFTER_FAILURES consecutive runs. The
  source is retried once every BROKEN_RETRY_DAYS days and becomes active
  again when a feed works.
- ``active``: all other sources.
"""
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional, get_args

from pydantic import BaseModel

from schema import ActorType, OutletType, PaywallModel, StateCode
from state import MAX_ERROR_CHARS, atomic_write_text, dump_entries, load_entries

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sources.toml"
SOURCES_STATE_FILE = "sources.json"
BROKEN_AFTER_FAILURES = 7
BROKEN_RETRY_DAYS = 7

SourceStatus = Literal["active", "broken", "blocked", "tdm_reserved"]


@dataclass(frozen=True)
class Outlet:
    """A news outlet.

    Attributes:
        id (str): Stable id.
        name (str): Display name.
        homepage (str): Homepage URL.
        outlet_type (str): Type of the outlet (see ``schema.OutletType``).
        owner_group (str): Publisher group or "public".
        home_state (Optional[str]): ISO 3166-2 state of a regional outlet.
        paywall_model (str): free, mixed or hard.
        feeds (tuple[str, ...]): RSS or Atom feed URLs.
        news_sitemap (Optional[str]): URL of the news sitemap.
        fetch_pages (bool): Whether article pages may be fetched.
    """
    id: str
    name: str
    homepage: str
    outlet_type: str
    owner_group: str
    paywall_model: str
    feeds: tuple[str, ...]
    home_state: Optional[str] = None
    news_sitemap: Optional[str] = None
    fetch_pages: bool = False

    @property
    def jurisdiction(self) -> str:
        """The state of a regional outlet, DE otherwise."""
        return self.home_state or "DE"


@dataclass(frozen=True)
class PressSource:
    """An issuer of press releases.

    Attributes:
        id (str): Stable id.
        name (str): Display name.
        homepage (str): Homepage URL.
        actor_type (str): Type of the actor (see ``schema.ActorType``).
        party (Optional[str]): Party code of a party or parliamentary group.
        jurisdiction (str): DE or the ISO 3166-2 code of a state.
        feeds (tuple[str, ...]): RSS or Atom feed URLs.
        fetch_pages (bool): Whether press release pages may be fetched.
    """
    id: str
    name: str
    homepage: str
    actor_type: str
    jurisdiction: str
    feeds: tuple[str, ...]
    party: Optional[str] = None
    fetch_pages: bool = False


@dataclass(frozen=True)
class SourceConfig:
    """All curated sources.

    Attributes:
        outlets (tuple[Outlet, ...]): News outlets.
        press_sources (tuple[PressSource, ...]): Press-release sources.
    """
    outlets: tuple[Outlet, ...] = field(default_factory=tuple)
    press_sources: tuple[PressSource, ...] = field(default_factory=tuple)


def _check_value(source_id: str, key: str, value: Optional[str], allowed: tuple[str, ...]) -> None:
    """Raises ValueError if a configured value is not in its vocabulary."""
    if value is not None and value not in allowed:
        raise ValueError(f"Source {source_id}: {key} {value!r} is not one of {', '.join(allowed)}")


def load_config(path: Path = CONFIG_PATH) -> SourceConfig:
    """Reads and validates ``config/sources.toml``.

    Args:
        path: The configuration file.

    Returns:
        SourceConfig: The outlets and press sources.

    Raises:
        ValueError: If an entry misses a key, uses an unknown value or repeats an id.
        FileNotFoundError: If the file does not exist.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    outlets = []
    for item in data.get("outlet", []):
        try:
            outlet = Outlet(
                id=item["id"], name=item["name"], homepage=item["homepage"], outlet_type=item["outlet_type"],
                owner_group=item["owner_group"], paywall_model=item["paywall_model"],
                feeds=tuple(item.get("feeds", [])), home_state=item.get("home_state"),
                news_sitemap=item.get("news_sitemap"), fetch_pages=bool(item.get("fetch_pages", False)),
            )
        except KeyError as e:
            raise ValueError(f"Outlet {item.get('id', '?')} misses the key {e}") from e
        _check_value(outlet.id, "outlet_type", outlet.outlet_type, get_args(OutletType))
        _check_value(outlet.id, "paywall_model", outlet.paywall_model, get_args(PaywallModel))
        _check_value(outlet.id, "home_state", outlet.home_state, get_args(StateCode))
        outlets.append(outlet)
    press_sources = []
    for item in data.get("press_source", []):
        try:
            source = PressSource(
                id=item["id"], name=item["name"], homepage=item["homepage"], actor_type=item["actor_type"],
                jurisdiction=item.get("jurisdiction", "DE"), feeds=tuple(item.get("feeds", [])),
                party=item.get("party"), fetch_pages=bool(item.get("fetch_pages", False)),
            )
        except KeyError as e:
            raise ValueError(f"Press source {item.get('id', '?')} misses the key {e}") from e
        _check_value(source.id, "actor_type", source.actor_type, get_args(ActorType))
        _check_value(source.id, "jurisdiction", source.jurisdiction, ("DE", *get_args(StateCode)))
        press_sources.append(source)
    ids = [source.id for source in (*outlets, *press_sources)]
    duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate source ids in {path.name}: {', '.join(duplicates)}")
    return SourceConfig(outlets=tuple(outlets), press_sources=tuple(press_sources))


class SourceHealth(BaseModel):
    """Health of one source.

    Attributes:
        status (str): active, broken, blocked or tdm_reserved.
        consecutive_failures (int): Failed runs since the last success.
        last_attempt (Optional[date]): Day of the latest attempt.
        last_success (Optional[date]): Day of the latest successful attempt.
        last_error (Optional[str]): Error of the latest failed attempt.
        items_last_run (int): Items read in the latest successful attempt.
    """
    status: SourceStatus = "active"
    consecutive_failures: int = 0
    last_attempt: Optional[date] = None
    last_success: Optional[date] = None
    last_error: Optional[str] = None
    items_last_run: int = 0


class SourceHealthStore:
    """Health of all sources (``state/sources.json``)."""

    def __init__(self, path: Path, entries: dict[str, SourceHealth]) -> None:
        """Initializes the store.

        Args:
            path: The JSON file.
            entries: Health by source id.
        """
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, state_dir: Path) -> "SourceHealthStore":
        """Reads the health file; a missing file means all sources are active.

        Args:
            state_dir: Folder of the state files.

        Returns:
            SourceHealthStore: The loaded store.
        """
        path = state_dir / SOURCES_STATE_FILE
        entries = {key: SourceHealth.model_validate(value) for key, value in load_entries(path, "sources").items()}
        return cls(path, entries)

    def save(self) -> None:
        """Writes the file atomically."""
        atomic_write_text(
            self._path,
            dump_entries("sources", {key: value.model_dump(mode="json") for key, value in self._entries.items()}),
        )

    def get(self, source_id: str) -> SourceHealth:
        """Returns the health of a source; unknown sources are active."""
        return self._entries.get(source_id, SourceHealth())

    def is_due(self, source_id: str, today: date) -> bool:
        """Tells whether a source is fetched in this run.

        Args:
            source_id: The source.
            today: Date of the run.

        Returns:
            bool: True for active sources and for broken sources whose retry is due.
        """
        health = self.get(source_id)
        if health.status == "active":
            return True
        if health.status == "broken":
            return health.last_attempt is None or health.last_attempt <= today - timedelta(days=BROKEN_RETRY_DAYS)
        return False

    def record_success(self, source_id: str, today: date, items: int) -> None:
        """Records that at least one feed of a source worked.

        Args:
            source_id: The source.
            today: Date of the run.
            items: Number of items read.
        """
        self._entries[source_id] = self.get(source_id).model_copy(update={
            "status": "active", "consecutive_failures": 0, "last_attempt": today, "last_success": today,
            "last_error": None, "items_last_run": items,
        })

    def record_failure(self, source_id: str, today: date, error: str,
                       status: Optional[SourceStatus] = None) -> SourceHealth:
        """Records a failed attempt and applies the status rules.

        Args:
            source_id: The source.
            today: Date of the run.
            error: Description of the failure.
            status: blocked or tdm_reserved to set that status directly; None
                applies the broken rule.

        Returns:
            SourceHealth: The updated health.
        """
        health = self.get(source_id)
        failures = health.consecutive_failures + 1
        new_status: SourceStatus = status or ("broken" if failures >= BROKEN_AFTER_FAILURES else health.status)
        if new_status != health.status:
            logger.warning("Source %s is now %s: %s", source_id, new_status, error)
        updated = health.model_copy(update={
            "status": new_status, "consecutive_failures": failures, "last_attempt": today,
            "last_error": error[:MAX_ERROR_CHARS],
        })
        self._entries[source_id] = updated
        return updated

    def status_counts(self, source_ids: list[str]) -> dict[str, int]:
        """Counts the sources per status.

        Args:
            source_ids: All configured source ids.

        Returns:
            dict[str, int]: Number of sources per status.
        """
        counts: dict[str, int] = {}
        for source_id in source_ids:
            status = self.get(source_id).status
            counts[status] = counts.get(status, 0) + 1
        return counts
