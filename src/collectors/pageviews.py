"""
Collects the daily views of the German Wikipedia articles of parties and politicians.

Article titles are resolved at run time: the candidate titles of the parties
through the MediaWiki API (following redirects), and the articles of the
politicians of the registry through their Wikidata ids. For each entity the
collector fetches the days since the last stored day from the Wikimedia
Pageviews API (CC0). A new entity gets a history: parties from July 2015,
politicians for the last POLITICIAN_HISTORY_DAYS days.
"""
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext
from entities import PARTIES, load_politicians
from schema import PageviewRecord
from state import CollectorEntry, is_backfilled
from web import FetchError, NotFoundError

logger = logging.getLogger(__name__)

DATASET = "wikipedia_pageviews"
SOURCE_ID = "wikimedia_pageviews"
LICENSE = "CC0-1.0"
PAGEVIEWS_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/de.wikipedia.org/"
                 "all-access/user/{title}/daily/{start}/{end}")
WIKIPEDIA_API_URL = "https://de.wikipedia.org/w/api.php"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
# The first day with pageview data in the Wikimedia API
PARTY_HISTORY_START = date(2015, 7, 1)
POLITICIAN_HISTORY_DAYS = 365
# Titles or ids per MediaWiki or Wikidata API request
API_BATCH_SIZE = 50


@dataclass(frozen=True)
class Article:
    """The Wikipedia article of an entity.

    Attributes:
        entity_id (str): Party code or abgeordnetenwatch politician id.
        entity_type (str): party or politician.
        title (str): Current article title.
        wikidata_qid (Optional[str]): Wikidata id, if known.
        history_start (date): First day to fetch for a new entity.
    """
    entity_id: str
    entity_type: str
    title: str
    wikidata_qid: Optional[str]
    history_start: date


def _batches(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yields consecutive slices of at most ``size`` values."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


def resolve_wikipedia_titles(ctx: RunContext, titles: Sequence[str]) -> dict[str, str]:
    """Resolves candidate titles to the current titles of existing articles.

    Args:
        ctx: The run context.
        titles: Candidate titles.

    Returns:
        dict[str, str]: Current title by candidate title, for existing articles only.
    """
    resolved: dict[str, str] = {}
    for batch in _batches(list(dict.fromkeys(titles)), API_BATCH_SIZE):
        data = ctx.web.get(WIKIPEDIA_API_URL, respect_robots=False, params={
            "action": "query", "titles": "|".join(batch), "redirects": "1", "format": "json", "formatversion": "2",
        }).json()
        query = data.get("query", {})
        mapping = {title: title for title in batch}
        for step in ("normalized", "redirects"):
            renames = {item["from"]: item["to"] for item in query.get(step, []) if "from" in item and "to" in item}
            mapping = {original: renames.get(current, current) for original, current in mapping.items()}
        existing = {page["title"] for page in query.get("pages", []) if not page.get("missing") and "title" in page}
        resolved.update({original: current for original, current in mapping.items() if current in existing})
    return resolved


def wikipedia_titles_for_qids(ctx: RunContext, qids: Sequence[str]) -> dict[str, str]:
    """Returns the German Wikipedia titles of Wikidata items.

    Args:
        ctx: The run context.
        qids: Wikidata ids.

    Returns:
        dict[str, str]: Title by Wikidata id, for items with a German article.
    """
    titles: dict[str, str] = {}
    for batch in _batches(sorted(set(qids)), API_BATCH_SIZE):
        data = ctx.web.get(WIKIDATA_API_URL, respect_robots=False, params={
            "action": "wbgetentities", "ids": "|".join(batch), "props": "sitelinks", "sitefilter": "dewiki",
            "format": "json",
        }).json()
        for qid, item in (data.get("entities") or {}).items():
            title = ((item.get("sitelinks") or {}).get("dewiki") or {}).get("title")
            if title:
                titles[qid] = title
    return titles


def _article_url(title: str, start: date, end: date) -> str:
    """Builds the Pageviews API URL of an article and a day range."""
    encoded = quote(title.replace(" ", "_"), safe="")
    return PAGEVIEWS_URL.format(title=encoded, start=f"{start:%Y%m%d}", end=f"{end:%Y%m%d}")


def fetch_daily_views(ctx: RunContext, title: str, start: date, end: date) -> dict[date, int]:
    """Fetches the daily views of an article.

    Args:
        ctx: The run context.
        title: Article title.
        start: First day.
        end: Last day.

    Returns:
        dict[date, int]: Views by day; days without data are missing.
    """
    try:
        data = ctx.web.get(_article_url(title, start, end), respect_robots=False).json()
    except NotFoundError:
        return {}
    views: dict[date, int] = {}
    for item in data.get("items", []):
        timestamp = str(item.get("timestamp", ""))
        try:
            day = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if isinstance(item.get("views"), int):
            views[day] = item["views"]
    return views


class PageviewsCollector(Collector):
    """Writes the new daily views of the articles of parties and politicians."""
    name = "wikipedia_pageviews"
    datasets = (DATASET,)

    def _articles(self, ctx: RunContext) -> list[Article]:
        """Resolves the articles of the parties and of the politicians of the registry."""
        candidates = [title for party in PARTIES for title in party.wikipedia_titles]
        resolved = resolve_wikipedia_titles(ctx, candidates) if candidates else {}
        articles: list[Article] = []
        for party in PARTIES:
            title = next((resolved[t] for t in party.wikipedia_titles if t in resolved), None)
            if party.wikipedia_titles and title is None:
                logger.warning("No Wikipedia article found for party %s.", party.code)
            if title:
                articles.append(Article(party.code, "party", title, None, PARTY_HISTORY_START))
        politicians = load_politicians(ctx.settings.state_dir)
        qids = {pid: p.wikidata_qid for pid, p in politicians.items() if p.wikidata_qid}
        titles = wikipedia_titles_for_qids(ctx, list(qids.values())) if qids else {}
        politician_start = ctx.today - timedelta(days=POLITICIAN_HISTORY_DAYS)
        for politician_id, qid in sorted(qids.items()):
            if qid in titles:
                articles.append(Article(politician_id, "politician", titles[qid], qid, politician_start))
        return articles

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the days since the last stored day for every article and emits one record per article.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per article is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        end = ctx.today - timedelta(days=1)
        records: list[PageviewRecord] = []
        counters: dict[str, Any] = {"articles": 0, "records": 0, "days": 0, "failed": 0}
        articles = self._articles(ctx)
        counters["articles"] = len(articles)
        for article in articles:
            previous = seen.get(article.entity_id)
            last_day = previous.data.get("last_day") if previous else None
            start = date.fromisoformat(last_day) + timedelta(days=1) if last_day else article.history_start
            if start > end:
                continue
            try:
                views = fetch_daily_views(ctx, article.title, start, end)
            except FetchError as e:
                logger.warning("Pageviews of %s failed: %s", article.title, e)
                counters["failed"] += 1
                continue
            if not views:
                continue
            first, last = min(views), max(views)
            revision = seen.mark(article.entity_id, article.title, ctx.today, data={"last_day": last.isoformat()})
            records.append(PageviewRecord(
                **ctx.envelope(
                    DATASET, f"pageviews:{article.entity_id}:{first.isoformat()}:{last.isoformat()}",
                    revision=revision, source_id=SOURCE_ID, source_url=_article_url(article.title, first, last),
                    license=LICENSE, jurisdiction="DE", backfilled=is_backfilled(first, entry.first_run),
                ),
                entity_id=article.entity_id,
                entity_type=article.entity_type,
                wikipedia_title=article.title,
                wikidata_qid=article.wikidata_qid,
                daily_views=dict(sorted(views.items())),
            ))
            counters["days"] += len(views)
            if len(records) >= 200:
                ctx.emit(DATASET, records)
                counters["records"] += len(records)
                records = []
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        logger.info("Pageviews: %d records with %d days for %d articles.", counters["records"], counters["days"],
                    len(articles))
        return CollectorResult(counters=counters)
