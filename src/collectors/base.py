"""
Common interface of the collectors and the context of a run.

A collector reads one source family and emits records of one or more
datasets. It receives a ``RunContext`` with the settings, the HTTP client,
the seen stores and a clock, and its own ``CollectorEntry`` with the first
run date and the cursor of the previous run. Records are written through
``RunContext.emit``, which also saves the seen store of the dataset, so that
an interrupted run keeps what it has written.
"""
import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, ClassVar, Optional

from schema import RecordBase
from settings import Settings
from state import CollectorEntry, SeenStore
from storage import write_records
from web import WebClient

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Returns the current UTC time without microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


class CollectorSkipped(Exception):
    """A collector cannot run because a prerequisite is missing, e.g. an API key."""


@dataclass
class CollectorResult:
    """Outcome of one collector run.

    Attributes:
        cursor (Optional[dict]): New cursor to store; None keeps the previous one.
        counters (dict): Counters for the run log.
        error (Optional[str]): Set if the collector wrote its data but a part of
            the work failed (e.g. the annotation stopped); the run then exits with code 1.
    """
    cursor: Optional[dict[str, Any]] = None
    counters: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class RunContext:
    """Shared resources of one pipeline run.

    Attributes:
        settings (Settings): The run settings.
        web (WebClient): The HTTP client.
        started (datetime): Start of the run (UTC).
        dry_run (bool): If True, nothing is written.
        written (Counter): Number of records emitted per dataset.
    """

    def __init__(
        self,
        settings: Settings,
        web: WebClient,
        started: datetime,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Initializes the context.

        Args:
            settings: The run settings.
            web: The HTTP client.
            started: Start of the run (UTC).
            dry_run: If True, records are counted but not written, and no state is saved.
            clock: Returns the current UTC time; replaceable in tests.
        """
        self.settings = settings
        self.web = web
        self.started = started
        self.dry_run = dry_run
        self._clock = clock
        self._seen: dict[str, SeenStore] = {}
        self.written: Counter[str] = Counter()

    @property
    def today(self) -> date:
        """UTC day of the run; the partition of all records of the run."""
        return self.started.date()

    def now(self) -> datetime:
        """Current UTC time, used as observed_at of new records."""
        return self._clock()

    def seen(self, dataset: str) -> SeenStore:
        """Returns the seen store of a dataset, loading it on first use.

        Args:
            dataset: Name of the dataset.

        Returns:
            SeenStore: The store.
        """
        if dataset not in self._seen:
            self._seen[dataset] = SeenStore.load(self.settings.state_dir, dataset)
        return self._seen[dataset]

    def envelope(
        self,
        dataset: str,
        record_id: str,
        *,
        revision: int,
        source_id: str,
        source_url: Optional[str],
        license: str,
        jurisdiction: str,
        backfilled: bool,
    ) -> dict[str, Any]:
        """Returns the common fields of a new record.

        Args:
            dataset: Name of the dataset.
            record_id: Stable id of the item.
            revision: Revision from the seen store.
            source_id: Id of the source.
            source_url: URL of the item or API resource.
            license: License or terms of the source.
            jurisdiction: DE or an ISO 3166-2 state code.
            backfilled: Whether the item predates the start of collection.

        Returns:
            dict: Keyword arguments for a ``RecordBase`` subclass.
        """
        return {
            "record_id": record_id, "revision": revision, "dataset": dataset, "observed_at": self.now(),
            "source_id": source_id, "source_url": source_url, "license": license,
            "jurisdiction": jurisdiction, "backfilled": backfilled,
        }

    def emit(self, dataset: str, records: Sequence[RecordBase]) -> None:
        """Writes records to the partition of the run day and saves the seen store of the dataset.

        Args:
            dataset: Name of the dataset.
            records: The records; all must belong to ``dataset``.

        Raises:
            ValueError: If a record belongs to another dataset.
        """
        if not records:
            return
        for record in records:
            if record.dataset != dataset:
                raise ValueError(f"Record {record.record_id} belongs to {record.dataset}, not {dataset}")
        self.written[dataset] += len(records)
        if self.dry_run:
            return
        write_records(self.settings.output_dir, dataset, self.today, records)
        if dataset in self._seen:
            self._seen[dataset].save()

    def save_seen(self) -> None:
        """Saves all loaded seen stores (not in dry-run mode)."""
        if self.dry_run:
            return
        for store in self._seen.values():
            store.save()


class Collector(ABC):
    """A source family that produces records of one or more datasets.

    Class attributes:
        name: Name used in settings, state and the run log.
        datasets: Datasets the collector writes.
    """
    name: ClassVar[str]
    datasets: ClassVar[tuple[str, ...]]

    @abstractmethod
    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Collects new and changed items and emits them through ``ctx.emit``.

        Args:
            ctx: The run context.
            entry: Run history of this collector (first run date, previous cursor).

        Returns:
            CollectorResult: New cursor and counters.

        Raises:
            CollectorSkipped: If a prerequisite such as an API key is missing.
        """
