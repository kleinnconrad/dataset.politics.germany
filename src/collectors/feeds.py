"""
Collects news articles and press releases and labels them with Gemini.

For every due source of ``config/sources.toml`` the collector reads the RSS
or Atom feeds and, for outlets, the news sitemap. New items published within
NEWS_LOOKBACK_HOURS are processed in turns across the sources: the article
page is fetched where robots.txt and the text-and-data-mining reservation
permit (metadata and lead text), the item is labelled by Gemini in batches,
and the metadata record and the annotation record are written. Texts are
not stored. Sources that refuse automated requests are set to ``blocked``.
"""
import calendar
import logging
import re
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from typing import Any, Optional, Union

import feedparser

from annotation import BATCH_SIZE, AnnotationInput, GeminiAnnotator, RunStopError, TransientAnnotationError
from collectors.base import Collector, CollectorResult, CollectorSkipped, RunContext
from content import PageInfo, clean_text, parse_datetime, parse_page
from entities import PoliticianIndex, load_politicians
from schema import ItemAnnotation, NewsItemRecord, PressReleaseRecord, TextAnnotationRecord
from sources import CONFIG_PATH, Outlet, PressSource, SourceConfig, SourceHealthStore, load_config
from state import CollectorEntry, is_backfilled
from urls import domain_id, normalize_url, path_tokens, url_id
from web import BlockedError, FetchError, NotFoundError, RobotsDisallowedError

logger = logging.getLogger(__name__)

NEWS = "news_items"
PRESS = "press_releases"
ANNOTATIONS = "text_annotations"
PUBLISHER_LICENSE = "Publisher rights; metadata only"
ANNOTATION_LICENSE = "Derived data (ODbL-1.0)"
SEEN_RETENTION_DAYS = 30
EMIT_EVERY = 100
MAX_TEASER_CHARS = 600
MAX_CHILD_SITEMAPS = 3
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
              "news": "http://www.google.com/schemas/sitemap-news/0.9"}
# URL path words of politics sections; items of feeds or sitemaps without them must match POLITICS_TERMS
POLITICS_PATH_TOKENS = frozenset({
    "politik", "inland", "ausland", "deutschland", "innenpolitik", "aussenpolitik", "bundestag", "landtag",
    "bundestagswahl", "landtagswahl", "wahl", "wahlen", "europawahl", "kommunalwahl", "parteien", "regierung",
    "landespolitik", "landtagswahl2026", "bundesregierung",
})
POLITICS_TERMS = re.compile(
    r"(?<!\w)(bundestag|landtag|bundesrat|bundesregierung|landesregierung|regierung|minister|kanzler|koalition|"
    r"opposition|fraktion|partei|wahl|umfrage|abgeordnet|gesetz|haushalt|verfassung|cdu|csu|spd|grüne|fdp|afd|"
    r"linke|bsw|freie wähler|bürgermeister|landrat|senat|staatskanzlei|bundeswehr|migration|asyl|rente|"
    r"bürgergeld|schuldenbremse|nato|europäische union|eu-kommission|politik|politisch)",
    re.IGNORECASE,
)
# Zone abbreviations that feedparser does not know; "CEST" would otherwise be read as UTC
ZONE_OFFSETS = {"CEST": "+0200", "MESZ": "+0200", "CET": "+0100", "MEZ": "+0100"}
ZONE_ABBREVIATION = re.compile(r"\b(CEST|MESZ|CET|MEZ)\b", re.IGNORECASE)
# Publication times further in the future than this are treated as unknown (wrong time zones in some feeds)
FUTURE_TOLERANCE = timedelta(minutes=15)
OUTLET_TYPE_LABELS = {
    "public_broadcaster_national": "national public broadcaster",
    "public_broadcaster_regional": "regional public broadcaster",
    "national_daily": "national daily newspaper",
    "weekly_magazine": "weekly newspaper or magazine",
    "regional_daily": "regional daily newspaper",
    "tabloid": "tabloid",
    "online_only": "online news site",
    "business": "business newspaper",
}

Source = Union[Outlet, PressSource]


class SourceFailure(Exception):
    """No feed of a source could be read in this run."""


class TdmReservedError(Exception):
    """The rights holder reserved text and data mining for a page of the source."""


@dataclass
class Candidate:
    """An item found in a feed or sitemap.

    Attributes:
        source (Source): The outlet or press source.
        url (str): URL of the item.
        key (str): Normalized URL, the key of the seen store.
        title (str): Title.
        teaser (str): Summary from the feed; may be empty.
        published_at (Optional[datetime]): Publication time (UTC).
        channel (str): rss or news_sitemap.
        categories (list[str]): Feed categories.
        keywords (list[str]): Sitemap keywords.
        from_politics_feed (bool): Whether the feed or sitemap URL names a politics section.
    """
    source: Source
    url: str
    key: str
    title: str
    teaser: str
    published_at: Optional[datetime]
    channel: str
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    from_politics_feed: bool = False

    @property
    def is_press(self) -> bool:
        """True for press releases."""
        return isinstance(self.source, PressSource)


@dataclass
class Prepared:
    """An item ready for annotation and writing.

    Attributes:
        candidate (Candidate): The item.
        record (Union[NewsItemRecord, PressReleaseRecord]): The metadata record.
        annotation_input (AnnotationInput): Text sent to Gemini.
        extra_keys (list[str]): Further seen keys (e.g. the canonical URL).
    """
    candidate: Candidate
    record: Union[NewsItemRecord, PressReleaseRecord]
    annotation_input: AnnotationInput
    extra_keys: list[str] = field(default_factory=list)


def _entry_time(entry: Any) -> Optional[datetime]:
    """Returns the publication time of a feed entry in UTC, or None.

    feedparser reads unknown zone abbreviations such as "CEST" as UTC, so the
    raw date is parsed first when it carries a German or Central European abbreviation.
    """
    for raw_key, parsed_key in (("published", "published_parsed"), ("updated", "updated_parsed")):
        raw = entry.get(raw_key)
        if isinstance(raw, str):
            match = ZONE_ABBREVIATION.search(raw)
            if match:
                fixed = parse_datetime(raw[: match.start()] + ZONE_OFFSETS[match.group(1).upper()] + raw[match.end():])
                if fixed is not None:
                    return fixed
        value = entry.get(parsed_key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def parse_feed(content: bytes, source: Source, feed_url: str) -> list[Candidate]:
    """Parses an RSS or Atom feed.

    Args:
        content: The feed document.
        source: The source of the feed.
        feed_url: URL of the feed.

    Returns:
        list[Candidate]: The entries with a link and a title.

    Raises:
        ValueError: If the document is not a feed.
    """
    parsed = feedparser.parse(content)
    if not parsed.entries and parsed.get("bozo"):
        raise ValueError(f"not a valid feed: {parsed.get('bozo_exception')!r}")
    politics_feed = bool(path_tokens(feed_url) & POLITICS_PATH_TOKENS)
    candidates = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = clean_text(entry.get("title"))
        if not link or not title:
            continue
        try:
            key = normalize_url(link)
        except ValueError:
            continue
        teaser = clean_text(entry.get("summary") or entry.get("description"), MAX_TEASER_CHARS)
        categories = [clean_text(tag.get("term"), 80) for tag in entry.get("tags", []) if tag.get("term")]
        candidates.append(Candidate(
            source=source, url=link, key=key, title=title, teaser=teaser if teaser != title else "",
            published_at=_entry_time(entry), channel="rss", categories=[c for c in categories if c],
            from_politics_feed=politics_feed,
        ))
    return candidates


def parse_news_sitemap(content: bytes, source: Outlet, sitemap_url: str) -> tuple[list[Candidate], list[str]]:
    """Parses a Google News sitemap or a sitemap index.

    Args:
        content: The XML document.
        source: The outlet.
        sitemap_url: URL of the sitemap.

    Returns:
        tuple[list[Candidate], list[str]]: The articles with a news title, and
        the child sitemap URLs if the document is a sitemap index.

    Raises:
        ValueError: If the document is not valid XML.
    """
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as e:
        raise ValueError(f"invalid sitemap XML: {e}") from e
    children = [loc.text.strip() for loc in root.findall("sm:sitemap/sm:loc", SITEMAP_NS) if loc.text]
    politics_sitemap = bool(path_tokens(sitemap_url) & POLITICS_PATH_TOKENS)
    candidates = []
    for url_element in root.findall("sm:url", SITEMAP_NS):
        loc = url_element.findtext("sm:loc", default="", namespaces=SITEMAP_NS).strip()
        news = url_element.find("news:news", SITEMAP_NS)
        if not loc or news is None:
            continue
        title = clean_text(news.findtext("news:title", default="", namespaces=SITEMAP_NS))
        if not title:
            continue
        try:
            key = normalize_url(loc)
        except ValueError:
            continue
        keywords_text = news.findtext("news:keywords", default="", namespaces=SITEMAP_NS)
        keywords = [part.strip() for part in keywords_text.split(",") if part.strip()][:30]
        candidates.append(Candidate(
            source=source, url=loc, key=key, title=title, teaser="",
            published_at=parse_datetime(news.findtext("news:publication_date", default="", namespaces=SITEMAP_NS)),
            channel="news_sitemap", keywords=keywords, from_politics_feed=politics_sitemap,
        ))
    return candidates, children


def looks_political(candidate: Candidate) -> bool:
    """Prefilter for outlet items: politics feed, politics section in the URL, or a political term.

    Args:
        candidate: The item.

    Returns:
        bool: True if the item is likely political; press releases always pass.
    """
    if candidate.is_press or candidate.from_politics_feed:
        return True
    if path_tokens(candidate.url) & POLITICS_PATH_TOKENS:
        return True
    text = " ".join([candidate.title, candidate.teaser, *candidate.categories, *candidate.keywords])
    return bool(POLITICS_TERMS.search(text))


def is_home_source(candidate: Candidate) -> bool:
    """Tells whether an item's URL is on the domain of its source.

    Outlets of a network (e.g. the ARD broadcasters) link to each other's
    articles; an article found in several sources is attributed to the source
    that publishes it on its own domain.

    Args:
        candidate: The item.

    Returns:
        bool: True if the URL host is the source's domain or one of its subdomains.
    """
    try:
        item_domain = domain_id(candidate.url)
        source_domain = domain_id(candidate.source.homepage)
    except ValueError:
        return False
    return item_domain == source_domain or item_domain.endswith("." + source_domain)


def interleave(groups: Sequence[Sequence[Candidate]]) -> Iterator[Candidate]:
    """Yields the items of several sources in turns, so that the run cap is shared fairly.

    Args:
        groups: Items per source, each in the order of processing.

    Yields:
        Candidate: The items, one per source in turn.
    """
    for row in zip_longest(*groups):
        for candidate in row:
            if candidate is not None:
                yield candidate


class FeedsCollector(Collector):
    """Writes news items, press releases and their Gemini annotations."""
    name = "feeds"
    datasets = (NEWS, PRESS, ANNOTATIONS)

    def __init__(self, annotator: Optional[GeminiAnnotator], config_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            annotator: The Gemini annotator; None is allowed only in dry-run mode.
            config_path: Path of the source configuration.
        """
        self._annotator = annotator
        self._config_path = config_path

    # -- reading sources ----------------------------------------------------

    def _read_source(self, ctx: RunContext, source: Source) -> list[Candidate]:
        """Reads all feeds and the news sitemap of a source.

        Raises:
            BlockedError: If the host refuses automated requests.
            SourceFailure: If no feed could be read.
        """
        candidates: list[Candidate] = []
        errors: list[str] = []
        successes = 0
        for feed_url in source.feeds:
            try:
                response = ctx.web.get(feed_url)
                candidates.extend(parse_feed(response.content, source, feed_url))
                successes += 1
            except (RobotsDisallowedError, NotFoundError, ValueError) as e:
                errors.append(str(e))
            except BlockedError:
                raise
            except FetchError as e:
                errors.append(str(e))
        sitemap_url = source.news_sitemap if isinstance(source, Outlet) else None
        if sitemap_url:
            try:
                candidates.extend(self._read_sitemap(ctx, source, sitemap_url))
                successes += 1
            except BlockedError:
                raise
            except (FetchError, ValueError) as e:
                errors.append(str(e))
        if successes == 0:
            raise SourceFailure("; ".join(errors) or "no feeds configured")
        return candidates

    def _read_sitemap(self, ctx: RunContext, source: Outlet, sitemap_url: str) -> list[Candidate]:
        """Reads a news sitemap; for a sitemap index, reads the first child sitemaps."""
        candidates, children = parse_news_sitemap(ctx.web.get(sitemap_url).content, source, sitemap_url)
        for child in children[:MAX_CHILD_SITEMAPS]:
            child_candidates, _ = parse_news_sitemap(ctx.web.get(child).content, source, child)
            candidates.extend(child_candidates)
        return candidates

    def _new_candidates(self, ctx: RunContext, candidates: list[Candidate], cutoff: datetime) -> list[Candidate]:
        """Removes duplicates, known items, old items and non-political outlet items; newest first.

        Duplicates are items with the same normalized URL, and items of the same
        source with the same title (outlets list one article in several feeds
        under different URLs).
        """
        unique: dict[str, Candidate] = {}
        titles: set[str] = set()
        for candidate in candidates:
            store = ctx.seen(PRESS if candidate.is_press else NEWS)
            title_key = " ".join(candidate.title.casefold().split())
            if candidate.key in store or candidate.key in unique or title_key in titles:
                continue
            if candidate.published_at is not None and candidate.published_at < cutoff:
                continue
            if not looks_political(candidate):
                continue
            unique[candidate.key] = candidate
            titles.add(title_key)
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(unique.values(), key=lambda c: c.published_at or oldest, reverse=True)

    @staticmethod
    def _groups_by_source(candidates: Iterable[Candidate], config: SourceConfig) -> list[list[Candidate]]:
        """Groups the items by source in the order of the configuration, newest first within a source."""
        by_source: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            by_source[candidate.source.id].append(candidate)
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        return [
            sorted(by_source[source.id], key=lambda c: c.published_at or oldest, reverse=True)
            for source in (*config.outlets, *config.press_sources)
            if by_source.get(source.id)
        ]

    # -- pages --------------------------------------------------------------

    def _fetch_page(self, ctx: RunContext, candidate: Candidate) -> Optional[PageInfo]:
        """Fetches and parses the page of an item where permitted.

        Returns:
            Optional[PageInfo]: The page data, or None if the page is not fetched or fails.

        Raises:
            TdmReservedError: If the page or its host declares a TDM reservation.
            BlockedError: If the host refuses automated requests for the page.
        """
        if not (ctx.settings.fetch_pages and candidate.source.fetch_pages):
            return None
        if ctx.web.tdm_reserved(candidate.url):
            raise TdmReservedError(candidate.url)
        try:
            response = ctx.web.get(candidate.url)
        except (RobotsDisallowedError, NotFoundError):
            return None
        except BlockedError:
            raise
        except FetchError as e:
            logger.info("Page of %s failed: %s", candidate.url, e)
            return None
        if ctx.web.tdm_reserved(response.url, response):
            raise TdmReservedError(candidate.url)
        if "html" not in response.content_type:
            return None
        info = parse_page(response.text)
        if info.tdm_reserved:
            raise TdmReservedError(candidate.url)
        return info

    # -- records ------------------------------------------------------------

    def _prepare(self, ctx: RunContext, candidate: Candidate, page: Optional[PageInfo],
                 first_run: date) -> Prepared:
        """Builds the metadata record and the annotation input of an item."""
        source = candidate.source
        latest_plausible = ctx.now() + FUTURE_TOLERANCE
        times = [candidate.published_at, page.published_at if page else None]
        published_at = next((time for time in times if time is not None and time <= latest_plausible), None)
        teaser = candidate.teaser or (page.description if page and page.description else "")
        lead = page.lead_text if page else ""
        basis = "lead_text" if lead else ("title_teaser" if teaser else "title_only")
        canonical = page.canonical_url if page else None
        extra_keys = []
        if canonical:
            try:
                canonical_key = normalize_url(canonical)
                if canonical_key != candidate.key:
                    extra_keys.append(canonical_key)
            except ValueError:
                canonical = None
        dataset = PRESS if candidate.is_press else NEWS
        envelope = ctx.envelope(
            dataset, f"{'press' if candidate.is_press else 'news'}:{url_id(candidate.url)}", revision=0,
            source_id=source.id, source_url=candidate.url, license=PUBLISHER_LICENSE,
            jurisdiction=source.jurisdiction,
            backfilled=is_backfilled(published_at.date() if published_at else None, first_run),
        )
        record: Union[NewsItemRecord, PressReleaseRecord]
        if isinstance(source, PressSource):
            record = PressReleaseRecord(
                **envelope, press_source_id=source.id, actor_type=source.actor_type, party=source.party,
                url=candidate.url, canonical_url=canonical, title=candidate.title, published_at=published_at,
                feed_categories=candidate.categories, content_basis=basis,
            )
            kind, label = "press release", f"{source.name} ({source.actor_type.replace('_', ' ')})"
        else:
            record = NewsItemRecord(
                **envelope, outlet_id=source.id, outlet_type=source.outlet_type, owner_group=source.owner_group,
                home_state=source.home_state, channel=candidate.channel, url=candidate.url, canonical_url=canonical,
                title=candidate.title, published_at=published_at, updated_at=page.updated_at if page else None,
                section=(page.section if page and page.section else (candidate.categories or [None])[0]),
                feed_categories=candidate.categories,
                keywords=(page.keywords if page and page.keywords else candidate.keywords),
                jsonld_type=page.jsonld_type if page else None,
                is_agency_copy=page.is_agency_copy if page and page.has_article_data else None,
                author_count=page.author_count if page and page.has_article_data else None,
                word_count=page.word_count if page else None,
                is_paywalled=page.is_paywalled if page else None,
                has_video=page.has_video if page else None,
                content_basis=basis,
            )
            kind, label = "news article", f"{source.name} ({OUTLET_TYPE_LABELS.get(source.outlet_type, '')})"
        annotation_input = AnnotationInput(
            kind=kind, source=label, published=published_at.date().isoformat() if published_at else None,
            title=candidate.title, teaser=teaser, lead_text=lead,
        )
        return Prepared(candidate, record, annotation_input, extra_keys)

    def _annotation_record(self, ctx: RunContext, prepared: Prepared, label: ItemAnnotation,
                           index: PoliticianIndex) -> TextAnnotationRecord:
        """Builds the annotation record of an item from the cleaned labels."""
        assert self._annotator is not None
        item = prepared.record
        politician_tone: dict[str, str] = {}
        unresolved = 0
        for person in label.politicians:
            politician_id = index.resolve(person.name, person.party)
            if politician_id is None:
                unresolved += 1
            else:
                politician_tone.setdefault(politician_id, person.tone)
        return TextAnnotationRecord(
            **ctx.envelope(
                ANNOTATIONS, f"annotation:{item.record_id}:{self._annotator.extraction_hash}", revision=0,
                source_id=item.source_id, source_url=item.source_url, license=ANNOTATION_LICENSE,
                jurisdiction=item.jurisdiction, backfilled=item.backfilled,
            ),
            item_dataset=item.dataset, item_id=item.record_id, model=self._annotator.model,
            extraction_hash=self._annotator.extraction_hash, content_basis=item.content_basis,
            is_political=label.is_political, political_level=label.political_level,
            states_concerned=label.states_concerned, cap_topic=label.cap_topic,
            cap_topics_secondary=label.cap_topics_secondary, issue_detail=label.issue_detail,
            article_type=label.article_type, event_types=label.event_types, event_status=label.event_status,
            government_involved=label.government_involved,
            mentioned_parties=[mention.party for mention in label.parties],
            party_roles={mention.party: mention.role for mention in label.parties},
            party_tone={mention.party: mention.tone for mention in label.parties},
            quoted_parties=[mention.party for mention in label.parties if mention.quoted],
            mentioned_politicians=list(politician_tone), politician_tone=politician_tone,
            unresolved_person_count=unresolved, overall_tone=label.overall_tone,
            government_tone=label.government_tone, frame=label.frame,
            conflict_pairs=[[pair.party_a, pair.party_b] for pair in label.conflict_pairs],
            reports_poll=label.reports_poll, poll_institute=label.poll_institute,
            upcoming_events=[{"event_type": event.event_type, "date": event.date} for event in label.upcoming_events],
            communication_type=label.communication_type, populism_markers=label.populism_markers,
        )

    # -- run ----------------------------------------------------------------

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the due sources, processes the new items and writes records and annotations.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters, and an error if the annotation stopped early.

        Raises:
            CollectorSkipped: If no annotator is available outside dry-run mode.
        """
        if self._annotator is None and not ctx.dry_run:
            raise CollectorSkipped("GEMINI_API_KEY is not set")
        config = load_config(self._config_path)
        health = SourceHealthStore.load(ctx.settings.state_dir)
        cutoff = ctx.started - timedelta(hours=ctx.settings.news_lookback_hours)
        counters: dict[str, Any] = defaultdict(int)
        by_key: dict[str, Candidate] = {}
        for source in (*config.outlets, *config.press_sources):
            if not health.is_due(source.id, ctx.today):
                counters["sources_skipped"] += 1
                continue
            try:
                found = self._read_source(ctx, source)
            except BlockedError as e:
                health.record_failure(source.id, ctx.today, str(e), status="blocked")
                counters["sources_blocked"] += 1
                continue
            except SourceFailure as e:
                logger.warning("Source %s failed: %s", source.id, e)
                health.record_failure(source.id, ctx.today, str(e))
                counters["sources_failed"] += 1
                continue
            new = self._new_candidates(ctx, found, cutoff)
            health.record_success(source.id, ctx.today, len(new))
            counters["sources_ok"] += 1
            counters["items_found"] += len(found)
            for candidate in new:
                existing = by_key.get(candidate.key)
                if existing is not None:
                    counters["duplicates_across_sources"] += 1
                    if is_home_source(existing) or not is_home_source(candidate):
                        continue
                by_key[candidate.key] = candidate
        groups = self._groups_by_source(by_key.values(), config)
        selected = list(interleave(groups))[: ctx.settings.max_annotations_per_run]
        counters["items_new"] = len(by_key)
        counters["items_selected"] = len(selected)
        if ctx.dry_run:
            logger.info("Dry run: %d new items from %d sources.", len(selected), counters["sources_ok"])
            return CollectorResult(counters=dict(counters))
        error = self._process(ctx, selected, health, entry, counters)
        for dataset in (NEWS, PRESS):
            counters[f"seen_pruned_{dataset}"] = ctx.seen(dataset).prune(ctx.today, SEEN_RETENTION_DAYS)
        health.save()
        ctx.save_seen()
        counters["source_status"] = health.status_counts([s.id for s in (*config.outlets, *config.press_sources)])
        if self._annotator is not None:
            counters["gemini_usage"] = vars(self._annotator.usage).copy()
        return CollectorResult(counters=dict(counters), error=error)

    def _process(self, ctx: RunContext, selected: list[Candidate], health: SourceHealthStore,
                 entry: CollectorEntry, counters: dict[str, Any]) -> Optional[str]:
        """Fetches pages, annotates in batches and writes the records.

        Returns:
            Optional[str]: The reason if the annotation stopped early because of a quota or API error.
        """
        index = PoliticianIndex(load_politicians(ctx.settings.state_dir))
        excluded_sources: set[str] = set()
        pages_blocked: set[str] = set()
        # Canonical URLs per source in this run; an outlet can list one article under several URLs
        run_canonical_keys: set[tuple[str, str]] = set()
        stop_reason: Optional[str] = None
        error: Optional[str] = None
        pending: list[Prepared] = []
        buffers: dict[str, list[Any]] = {NEWS: [], PRESS: [], ANNOTATIONS: []}

        def flush() -> None:
            for dataset, records in buffers.items():
                ctx.emit(dataset, records)
                records.clear()

        def annotate_pending() -> None:
            nonlocal stop_reason, error
            if not pending:
                return
            labels: list[Optional[ItemAnnotation]] = [None] * len(pending)
            if stop_reason is None and self._annotator is not None:
                try:
                    labels = self._annotator.annotate([p.annotation_input for p in pending])
                except TransientAnnotationError as e:
                    logger.warning("Annotation of a batch failed: %s", e)
                    counters["annotation_batches_failed"] += 1
                except RunStopError as e:
                    stop_reason = str(e)
                    logger.error("Annotation stopped: %s", e)
                    if "MAX_GEMINI_CALLS_PER_RUN" not in stop_reason:
                        error = f"annotation stopped: {stop_reason}"
            for prepared, label in zip(pending, labels):
                dataset = prepared.record.dataset
                buffers[dataset].append(prepared.record)
                store = ctx.seen(dataset)
                for key in (prepared.candidate.key, *prepared.extra_keys):
                    store.mark(key, prepared.candidate.key, ctx.today)
                counters[f"written_{dataset}"] += 1
                if label is None:
                    counters["not_annotated"] += 1
                else:
                    buffers[ANNOTATIONS].append(self._annotation_record(ctx, prepared, label, index))
                    counters["annotated"] += 1
            pending.clear()
            if sum(len(records) for records in buffers.values()) >= EMIT_EVERY:
                flush()

        for candidate in selected:
            source = candidate.source
            if source.id in excluded_sources:
                continue
            store = ctx.seen(PRESS if candidate.is_press else NEWS)
            if candidate.key in store:
                continue
            page: Optional[PageInfo] = None
            if source.id not in pages_blocked:
                try:
                    page = self._fetch_page(ctx, candidate)
                except TdmReservedError:
                    health.record_failure(source.id, ctx.today, "text and data mining reserved", status="tdm_reserved")
                    excluded_sources.add(source.id)
                    pending[:] = [p for p in pending if p.candidate.source.id != source.id]
                    counters["sources_tdm_reserved"] += 1
                    continue
                except BlockedError as e:
                    logger.warning("Pages of %s are blocked (%s); continuing without pages.", source.id, e)
                    pages_blocked.add(source.id)
                    counters["sources_pages_blocked"] += 1
            counters["pages_fetched" if page else "pages_missing"] += 1
            prepared = self._prepare(ctx, candidate, page, entry.first_run)
            canonical_keys = {(source.id, key) for key in prepared.extra_keys}
            if any(key in store for key in prepared.extra_keys) or canonical_keys & run_canonical_keys:
                counters["duplicates_canonical"] += 1
                continue
            run_canonical_keys.update(canonical_keys)
            pending.append(prepared)
            if len(pending) >= BATCH_SIZE:
                annotate_pending()
        annotate_pending()
        flush()
        return error
