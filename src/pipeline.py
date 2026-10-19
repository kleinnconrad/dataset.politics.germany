"""
Executes one pipeline run.

The collectors run one after another in COLLECTOR_ORDER: the politician
registry first, because the annotation of news resolves names against it,
and the feeds last, because they take the longest. Each collector runs in
isolation: a failure is logged and recorded in ``state/collectors.json`` and
the run log, and the other collectors continue. The data that a failing
collector has already emitted stays written.
"""
import logging
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from annotation import GeminiAnnotator
from collectors.base import Collector, CollectorSkipped, RunContext, utc_now
from collectors.feeds import FeedsCollector
from collectors.pageviews import PageviewsCollector
from collectors.parliament import PoliticiansCollector, RollCallVotesCollector
from collectors.polls import PollsCollector
from collectors.procedures import ProceduresCollector
from settings import Settings
from state import CollectorStates, append_run_log
from web import WebClient

logger = logging.getLogger(__name__)

COLLECTOR_ORDER = ("politicians", "roll_call_votes", "parliamentary_procedures", "polls", "wikipedia_pageviews",
                   "feeds")
# Request intervals per host: abgeordnetenwatch allows 30 requests per minute;
# the Wikimedia REST API allows far more than the default of one per second.
HOST_INTERVALS = {"www.abgeordnetenwatch.de": 2.1, "wikimedia.org": 0.1}


@dataclass
class CollectorOutcome:
    """Result of one collector in a run.

    Attributes:
        status (str): ok, failed or skipped.
        records (dict[str, int]): Records written per dataset.
        counters (dict): Counters reported by the collector.
        error (Optional[str]): Error message for failed and skipped collectors.
    """
    status: str
    records: dict[str, int] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class RunSummary:
    """Summary of a run, written to the run log.

    Attributes:
        started (datetime): Start of the run (UTC).
        finished (Optional[datetime]): End of the run (UTC).
        mode (str): normal or dry-run.
        collectors (dict[str, CollectorOutcome]): Outcome per collector.
        requests (int): HTTP requests sent.
    """
    started: datetime
    finished: Optional[datetime] = None
    mode: str = "normal"
    collectors: dict[str, CollectorOutcome] = field(default_factory=dict)
    requests: int = 0

    @property
    def failed(self) -> list[str]:
        """Names of the collectors that failed or were skipped."""
        return [name for name, outcome in self.collectors.items() if outcome.status != "ok"]

    def to_log_entry(self) -> dict[str, Any]:
        """Returns the JSON-compatible run log line."""
        return {
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "mode": self.mode,
            "requests": self.requests,
            "collectors": {
                name: {key: value for key, value in vars(outcome).items() if value not in (None, {}, [])}
                for name, outcome in self.collectors.items()
            },
        }


def build_collectors(annotator: Optional[GeminiAnnotator]) -> list[Collector]:
    """Creates all collectors in COLLECTOR_ORDER.

    Args:
        annotator: The Gemini annotator for the feeds collector; None in dry-run mode
            or when no API key is set.

    Returns:
        list[Collector]: The collectors.
    """
    collectors: dict[str, Collector] = {
        collector.name: collector for collector in (
            PoliticiansCollector(), RollCallVotesCollector(), ProceduresCollector(), PollsCollector(),
            PageviewsCollector(), FeedsCollector(annotator),
        )
    }
    return [collectors[name] for name in COLLECTOR_ORDER]


def run_pipeline(
    settings: Settings,
    collectors: Sequence[Collector],
    *,
    dry_run: bool = False,
    web: Optional[WebClient] = None,
    clock: Callable[[], datetime] = utc_now,
) -> RunSummary:
    """Runs the selected collectors once.

    Args:
        settings: The run settings; ``settings.collectors`` restricts the collectors.
        collectors: The available collectors, in the order of execution.
        dry_run: If True, nothing is written.
        web: HTTP client; created with HOST_INTERVALS if omitted.
        clock: Returns the current UTC time.

    Returns:
        RunSummary: Outcome per collector.
    """
    started = clock()
    own_client = web is None
    client = web or WebClient(host_intervals=dict(HOST_INTERVALS))
    states = CollectorStates.load(settings.state_dir)
    ctx = RunContext(settings, client, started, dry_run=dry_run, clock=clock)
    summary = RunSummary(started=started, mode="dry-run" if dry_run else "normal")
    try:
        for collector in collectors:
            if settings.collectors and collector.name not in settings.collectors:
                continue
            summary.collectors[collector.name] = _run_collector(ctx, states, collector, started)
            ctx.save_seen()
            if not dry_run:
                states.save()
    finally:
        if own_client:
            client.close()
    summary.finished = clock()
    summary.requests = client.request_count
    if not dry_run:
        append_run_log(settings.state_dir, summary.to_log_entry())
    return summary


def _run_collector(ctx: RunContext, states: CollectorStates, collector: Collector, started: datetime) -> CollectorOutcome:
    """Runs one collector and records its outcome in the collector states."""
    entry = states.entry(collector.name, ctx.today)
    before = Counter(ctx.written)
    logger.info("Collector %s started.", collector.name)
    try:
        result = collector.collect(ctx, entry)
    except CollectorSkipped as e:
        logger.error("Collector %s skipped: %s", collector.name, e)
        states.record_failure(collector.name, started, f"skipped: {e}")
        return CollectorOutcome(status="skipped", error=str(e))
    # Isolation boundary: an unexpected error of one collector must not stop the others.
    except Exception as e:
        logger.error("Collector %s failed: %s\n%s", collector.name, e, traceback.format_exc())
        states.record_failure(collector.name, started, f"{e.__class__.__name__}: {e}")
        written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
        return CollectorOutcome(status="failed", records=written, error=f"{e.__class__.__name__}: {e}")
    written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
    states.record_success(collector.name, started, result.cursor)
    if result.error:
        states.record_failure(collector.name, started, result.error)
        logger.error("Collector %s finished with an error: %s", collector.name, result.error)
        return CollectorOutcome(status="failed", records=written, counters=result.counters, error=result.error)
    logger.info("Collector %s finished: %s records.", collector.name, written or "no new")
    return CollectorOutcome(status="ok", records=written, counters=result.counters)
