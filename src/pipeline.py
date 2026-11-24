"""
Executes one pipeline run.

The collectors run one after another in COLLECTOR_ORDER: the politician
registry first, because the annotation of news resolves names against it,
then the feeds, because news items leave the feeds after a few days, and
then the collectors whose data can still be fetched in a later run. Each
collector runs in isolation: a failure is logged and recorded in
``state/collectors.json`` and the run log, and the other collectors continue.
The data that a failing collector has already emitted stays written.

The run has a deadline (RUN_DEADLINE_MINUTES): the collectors start no new
work after it, and a collector that has not started by then is skipped, so
that the workflow commits the data before its timeout. Collectors with
``min_days_between_runs`` run only when that many days have passed since
their last complete run. A backfill run imports the history of the
structured collectors without the per-run caps and ignores these intervals.

A collector with a ``source_id`` whose source refuses automated requests
(``BlockedError``) fails once and sets the source to ``blocked`` in
``state/sources.json``; later runs do not run it until the entry is reset by
hand, and report it with the status ``blocked``.

Collectors in OPT_IN_COLLECTORS run only when they are selected explicitly
(``COLLECTORS``), not in the scheduled runs and not in the default backfill.
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
from collectors.bundestag_votes import BundestagVotesCollector
from collectors.feeds import FeedsCollector
from collectors.pageviews import PageviewsCollector
from collectors.parliament import PoliticiansCollector
from collectors.polls import PollsCollector
from collectors.procedures import ProceduresCollector
from settings import Settings
from sources import SourceHealthStore
from state import CollectorStates, append_run_log
from web import BlockedError, WebClient

logger = logging.getLogger(__name__)

COLLECTOR_ORDER = ("politicians", "feeds", "bundestag_votes", "parliamentary_procedures", "polls",
                   "wikipedia_pageviews")
# Collectors that run only when selected explicitly; empty while every collector runs in the scheduled runs
OPT_IN_COLLECTORS: frozenset[str] = frozenset()
# Collectors of a backfill run: all except the feeds, whose items are not available for the past, and the
# opt-in collectors
BACKFILL_COLLECTORS = tuple(name for name in COLLECTOR_ORDER if name != "feeds" and name not in OPT_IN_COLLECTORS)
# Request intervals per host: abgeordnetenwatch allows 30 requests per minute;
# the Wikimedia REST API allows far more than the default of one per second.
HOST_INTERVALS = {"www.abgeordnetenwatch.de": 2.1, "wikimedia.org": 0.1}
# Outcomes that make the run exit with code 1
FAILED_STATUSES = frozenset({"failed", "skipped"})


@dataclass
class CollectorOutcome:
    """Result of one collector in a run.

    Attributes:
        status (str): ok; partial if the collector stopped at the run deadline;
            not_due if it is not due in this run; blocked if its source refused
            automated requests in an earlier run; failed or skipped.
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
        mode (str): normal, backfill or dry-run.
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
        return [name for name, outcome in self.collectors.items() if outcome.status in FAILED_STATUSES]

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
            PoliticiansCollector(), BundestagVotesCollector(), ProceduresCollector(), PollsCollector(),
            PageviewsCollector(), FeedsCollector(annotator),
        )
    }
    return [collectors[name] for name in COLLECTOR_ORDER]


def run_pipeline(
    settings: Settings,
    collectors: Sequence[Collector],
    *,
    dry_run: bool = False,
    backfill: bool = False,
    web: Optional[WebClient] = None,
    clock: Callable[[], datetime] = utc_now,
) -> RunSummary:
    """Runs the selected collectors once.

    Args:
        settings: The run settings; ``settings.collectors`` restricts the collectors.
            Explicitly selected collectors run even if they are not due.
        collectors: The available collectors, in the order of execution.
        dry_run: If True, nothing is written.
        backfill: If True, the collectors import the history without the per-run caps.
        web: HTTP client; created with HOST_INTERVALS if omitted.
        clock: Returns the current UTC time.

    Returns:
        RunSummary: Outcome per collector.
    """
    started = clock()
    own_client = web is None
    client = web or WebClient(host_intervals=dict(HOST_INTERVALS))
    states = CollectorStates.load(settings.state_dir)
    ctx = RunContext(settings, client, started, dry_run=dry_run, clock=clock, backfill=backfill)
    mode = "dry-run" if dry_run else ("backfill" if backfill else "normal")
    summary = RunSummary(started=started, mode=mode)
    try:
        for collector in collectors:
            if settings.collectors and collector.name not in settings.collectors:
                continue
            if not settings.collectors and collector.name in OPT_IN_COLLECTORS:
                logger.info("Collector %s runs only when selected (COLLECTORS).", collector.name)
                continue
            blocked = _blocked_source(ctx, collector)
            if blocked is not None:
                summary.collectors[collector.name] = CollectorOutcome(status="blocked", error=blocked)
                continue
            if _not_due(ctx, states, collector):
                summary.collectors[collector.name] = CollectorOutcome(status="not_due")
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


def _not_due(ctx: RunContext, states: CollectorStates, collector: Collector) -> bool:
    """Tells whether a collector skips this run because its last complete run is too recent.

    Backfill runs and explicitly selected collectors are always due.
    """
    if collector.min_days_between_runs <= 0 or ctx.backfill or ctx.settings.collectors:
        return False
    last_complete = states.entry(collector.name, ctx.today).last_complete
    if last_complete is None or (ctx.today - last_complete.date()).days >= collector.min_days_between_runs:
        return False
    logger.info("Collector %s is not due: last complete run on %s, runs every %d days.",
                collector.name, last_complete.date().isoformat(), collector.min_days_between_runs)
    return True


def _blocked_source(ctx: RunContext, collector: Collector) -> Optional[str]:
    """Tells whether the source of a collector is blocked.

    Args:
        ctx: The run context.
        collector: The collector.

    Returns:
        Optional[str]: A message if the collector's source is blocked, else None.
    """
    if collector.source_id is None:
        return None
    health = SourceHealthStore.load(ctx.settings.state_dir).get(collector.source_id)
    if health.status != "blocked":
        return None
    message = f"source {collector.source_id} blocked since {health.last_attempt}: {health.last_error}"
    logger.warning("Collector %s not run: %s. Reset the entry %s in state/sources.json to fetch it again.",
                   collector.name, message, collector.source_id)
    return message


def _set_blocked(ctx: RunContext, source_id: str, error: BlockedError) -> None:
    """Sets a source to blocked in the source health file (not in dry-run mode).

    Args:
        ctx: The run context.
        source_id: The source.
        error: The refusal.
    """
    if ctx.dry_run:
        return
    health = SourceHealthStore.load(ctx.settings.state_dir)
    health.record_failure(source_id, ctx.today, str(error), status="blocked")
    health.save()


def _run_collector(ctx: RunContext, states: CollectorStates, collector: Collector, started: datetime) -> CollectorOutcome:
    """Runs one collector and records its outcome in the collector states."""
    entry = states.entry(collector.name, ctx.today)
    before = Counter(ctx.written)
    if ctx.past_deadline():
        message = f"run deadline of {ctx.settings.run_deadline_minutes} minutes reached before the collector started"
        logger.error("Collector %s skipped: %s", collector.name, message)
        states.record_failure(collector.name, started, f"skipped: {message}")
        return CollectorOutcome(status="skipped", error=message)
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
        if isinstance(e, BlockedError) and collector.source_id is not None:
            _set_blocked(ctx, collector.source_id, e)
        states.record_failure(collector.name, started, f"{e.__class__.__name__}: {e}")
        written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
        return CollectorOutcome(status="failed", records=written, error=f"{e.__class__.__name__}: {e}")
    written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
    states.record_success(collector.name, started, result.cursor, complete=result.complete)
    if result.error:
        states.record_failure(collector.name, started, result.error)
        logger.error("Collector %s finished with an error: %s", collector.name, result.error)
        return CollectorOutcome(status="failed", records=written, counters=result.counters, error=result.error)
    if not result.complete:
        logger.warning("Collector %s stopped at the run deadline: %s records; the rest follows in the next run.",
                       collector.name, written or "no new")
        return CollectorOutcome(status="partial", records=written, counters=result.counters)
    logger.info("Collector %s finished: %s records.", collector.name, written or "no new")
    return CollectorOutcome(status="ok", records=written, counters=result.counters)
