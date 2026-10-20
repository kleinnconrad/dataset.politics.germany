"""
Monthly review of the source list in ``config/sources.toml``.

The review
1. checks the active sources: their health in ``state/sources.json``, their
   yield of political items in the last REVIEW_WINDOW_DAYS days, every feed and
   news sitemap (errors, permanent redirects, age of the newest item) and the
   robots.txt of their homepage (crawl delay, a text-and-data-mining
   reservation stated in natural language);
2. finds gaps: parliamentary groups of the Bundestag and parties with seats in
   a parliament without a press source, states without a state government
   source or without a regional daily;
3. asks Gemini with Google Search for the official feeds of the federal
   government and its ministries and for the gaps, and checks every candidate
   with the fetch rules of the pipeline;
4. writes the proposed changes into the configuration (removed dates, feed
   changes, page fetching, new sources, exclusions of rejected candidates) and
   a Markdown report.

The workflow ``source_review.yml`` opens a pull request with the changed
configuration and the report; the changes take effect when it is merged.
Every request goes through ``WebClient`` and its fetch rules. Hosts that
blocked automated requests or reserved text and data mining are not
requested again.

Usage:
    uv run python src/source_review.py --report PATH [--no-search] [--config PATH] [--output-dir PATH]
"""
import argparse
import json
import logging
import re
import sys
import tempfile
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, ValidationError

from annotation import create_client
from collectors.feeds import (
    MAX_CHILD_SITEMAPS,
    POLITICS_PATH_TOKENS,
    Candidate,
    looks_political,
    parse_feed,
    parse_news_sitemap,
)
from content import parse_page
from entities import NO_FACTION, PARTY_BY_CODE, Politician, load_politicians
from schema import PartyCode, PaywallModel, StateCode
from settings import Settings
from sources import (
    CONFIG_PATH,
    ExcludedSource,
    Outlet,
    PressSource,
    Source,
    SourceConfig,
    SourceHealth,
    SourceHealthStore,
    load_config,
)
from urls import domain_id, normalize_url, path_tokens
from web import (
    MAX_CRAWL_DELAY_S,
    BlockedError,
    FetchError,
    NotFoundError,
    RobotsDisallowedError,
    RobotsRules,
    WebClient,
    host_of,
)

logger = logging.getLogger(__name__)

# Sources without any political item in this window are proposed for removal (unless keep = true)
REVIEW_WINDOW_DAYS = 90
# A feed whose newest item is older than this is stale
STALE_FEED_DAYS = 180
# Article pages are fetched only from hosts with at most this crawl delay, so that a run stays short
MAX_PAGE_CRAWL_DELAY_S = 10.0
MAX_FEEDS_PER_SOURCE = 3
MAX_FEEDS_CHECKED_PER_CANDIDATE = 12
# Feed links read from one page (head links and links whose address looks like a feed)
MAX_FEED_LINKS_PER_PAGE = 20
MAX_ADDITIONS_PER_REVIEW = 5
MAX_SEARCH_CALLS = 4
# One search can return a candidate for each of the 16 states
MAX_CANDIDATES_PER_SEARCH = 16
MIN_SEARCH_INTERVAL_S = 2.0
SEARCH_TEMPERATURE = 0.2
SEARCH_TIMEOUT_MS = 120_000
# A new press source needs at least one item in PRESS_RECENT_DAYS days; a new outlet at least
# MIN_OUTLET_POLITICAL_ITEMS political items in OUTLET_RECENT_DAYS days
PRESS_RECENT_DAYS = 60
OUTLET_RECENT_DAYS = 7
MIN_OUTLET_POLITICAL_ITEMS = 5
# Requests stop after this time, so that the workflow ends within its timeout
REVIEW_TIME_BUDGET_S = 20 * 60
# Notes beyond this number are counted, not listed, so that the pull request description stays short
MAX_REPORT_NOTES = 40
ITEM_DATASETS = ("news_items", "press_releases")
FEED_TYPES = ("application/rss+xml", "application/atom+xml")
PRESS_URL_TOKENS = ("presse", "press", "mitteilung", "meldung", "news", "aktuell")
# Link addresses that look like a feed or an overview page of feeds, e.g. /rss.xml, /service/rss-feeds/, /feed/
FEED_LINK_PATTERN = re.compile(r"(?<![a-z])(rss|atom)(?![a-z])|feed(?!back)|\.xml(?:\?|$)", re.IGNORECASE)

STATE_NAMES: dict[str, str] = {
    "DE-BW": "Baden-Württemberg", "DE-BY": "Bayern", "DE-BE": "Berlin", "DE-BB": "Brandenburg", "DE-HB": "Bremen",
    "DE-HH": "Hamburg", "DE-HE": "Hessen", "DE-MV": "Mecklenburg-Vorpommern", "DE-NI": "Niedersachsen",
    "DE-NW": "Nordrhein-Westfalen", "DE-RP": "Rheinland-Pfalz", "DE-SL": "Saarland", "DE-SN": "Sachsen",
    "DE-ST": "Sachsen-Anhalt", "DE-SH": "Schleswig-Holstein", "DE-TH": "Thüringen",
}

SEARCH_RULES = """\
Rules:
- List only feeds on the official website of the institution or newspaper, no aggregators and no social media.
- Give the feed URLs as found in the search results or on the website; do not construct them.
- Do not suggest these domains, which are already known: {known}."""

FEDERAL_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of the federal government of Germany in its current composition: the Bundesregierung (Presse- und Informationsamt), the Federal Chancellery and every federal ministry. Ministries are renamed and restructured after elections; use their current names and websites.

These federal institutions are already in the list; do not suggest them again, also not under an earlier name or domain: {listed}.

{rules}

Return only a JSON array without other text, with one object per institution:
[{{"name": "current official German name", "homepage": "https://...", "feeds": ["https://..."], "actor_type": "federal_government or federal_ministry"}}]"""

GROUPS_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of these German parties and parliamentary groups (federal level):
{actors}

{rules}

Return only a JSON array without other text, with one object per party or parliamentary group:
[{{"name": "official German name", "homepage": "https://...", "feeds": ["https://..."], "actor_type": "party or parliamentary_group", "party": "the code given in the list"}}]"""

STATES_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of the governments of these German states (Staatskanzlei, Senatskanzlei or Landesregierung):
{states}

{rules}

Return only a JSON array without other text, with one object per state:
[{{"name": "official German name, e.g. Staatsministerium Baden-Württemberg", "homepage": "https://...", "feeds": ["https://..."], "jurisdiction": "the code given in the list"}}]"""

DAILIES_PROMPT = """\
Use Google Search to find regional daily newspapers from these German states that publish an RSS or Atom feed of their politics section or a Google News sitemap:
{states}

{rules}

Return only a JSON array without other text, with up to {count} objects:
[{{"name": "name of the newspaper", "homepage": "https://...", "feeds": ["https://..."], "news_sitemap": "https://... or null", "home_state": "the code given in the list", "owner_group": "publisher group", "paywall_model": "free, mixed or hard"}}]"""

ChangeAction = Literal["remove_source", "remove_feed", "replace_feed", "remove_sitemap", "set_fetch_pages",
                       "add_source", "exclude", "drop_exclusion"]
SearchCategory = Literal["federal", "groups", "states", "dailies"]
# Search category of an excluded press source, by actor type, for its recheck
EXCLUSION_CATEGORIES: dict[str, SearchCategory] = {
    "federal_government": "federal", "federal_ministry": "federal", "party": "groups",
    "parliamentary_group": "groups", "state_government": "states",
}


class SearchError(Exception):
    """A search request failed or the search budget is used up."""


@dataclass(frozen=True)
class NewEntry:
    """A new entry of the configuration.

    Attributes:
        table (str): outlet, press_source or excluded.
        fields (dict[str, Any]): Keys and values in the order of the entry; None values are left out.
        comment (Optional[str]): Comment line above the entry.
    """
    table: str
    fields: dict[str, Any]
    comment: Optional[str] = None


@dataclass(frozen=True)
class Change:
    """A proposed change of the configuration.

    Attributes:
        action (str): What to change (see ``ChangeAction``).
        source_id (str): Id of the source or exclusion.
        reason (str): Why; shown in the pull request and in the configuration.
        old (Optional[str]): Feed or sitemap URL that is removed or replaced.
        new (Optional[str]): Feed URL that replaces ``old``.
        entry (Optional[NewEntry]): The new entry of ``add_source`` and ``exclude``.
    """
    action: ChangeAction
    source_id: str
    reason: str
    old: Optional[str] = None
    new: Optional[str] = None
    entry: Optional[NewEntry] = None


@dataclass(frozen=True)
class FeedCheck:
    """Result of fetching a feed or news sitemap.

    Attributes:
        url (str): The URL.
        outcome (str): ok, not_found, disallowed, blocked, error or invalid.
        detail (str): Description of a failure.
        entries (tuple[Candidate, ...]): The items of the feed.
        newest (Optional[datetime]): Publication time of the newest item.
        moved_to (Optional[str]): New URL after permanent redirects.
        links (tuple[str, ...]): Feed links of an HTML page that is not a feed, e.g. an
            overview page of the feeds of a ministry.
    """
    url: str
    outcome: Literal["ok", "not_found", "disallowed", "blocked", "error", "invalid"]
    detail: str = ""
    entries: tuple[Candidate, ...] = ()
    newest: Optional[datetime] = None
    moved_to: Optional[str] = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gaps:
    """Parts of the political landscape without a source.

    Attributes:
        bundestag_groups (tuple[str, ...]): Party codes of Bundestag groups without a press source.
        parties (tuple[str, ...]): Party codes of parties with seats but without a press source.
        state_governments (tuple[str, ...]): States without a state government source.
        regional_dailies (tuple[str, ...]): States without a regional daily.
    """
    bundestag_groups: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    state_governments: tuple[str, ...] = ()
    regional_dailies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    """A candidate that was not admitted.

    Attributes:
        name (str): Name of the candidate.
        homepage (str): Homepage URL.
        reason (str): Why it was not admitted.
        status (Optional[str]): Exclusion status (see ``ExclusionStatus``); None if the
            reason may be temporary, so that the candidate is not excluded.
        kind (Optional[str]): outlet or press_source, if the attributes were valid.
        attributes (dict[str, Any]): The validated attributes, kept with an exclusion for its recheck.
        feeds (tuple[str, ...]): The proposed feed URLs.
    """
    name: str
    homepage: str
    reason: str
    status: Optional[str] = None
    kind: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    feeds: tuple[str, ...] = ()


@dataclass
class ReviewResult:
    """Outcome of a review.

    Attributes:
        changes (list[Change]): Proposed changes of the configuration.
        notes (list[str]): Observations without a change, e.g. temporary errors.
        rejections (list[Rejection]): Candidates that were not admitted.
        gaps (Gaps): Gaps that were searched for.
        searches (list[str]): Search categories that were sent to Gemini.
        checked_sources (int): Active sources that were checked.
        candidates (int): Candidates from the search that were evaluated.
        rechecked (int): Excluded candidates that were checked again.
    """
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    gaps: Gaps = field(default_factory=Gaps)
    searches: list[str] = field(default_factory=list)
    checked_sources: int = 0
    candidates: int = 0
    rechecked: int = 0


class PressCandidate(BaseModel):
    """A press-release source proposed by the search."""
    model_config = ConfigDict(extra="ignore")

    name: str
    homepage: str
    feeds: list[str] = []
    actor_type: Optional[str] = None
    jurisdiction: Optional[str] = None
    party: Optional[str] = None


class OutletCandidate(BaseModel):
    """A news outlet proposed by the search."""
    model_config = ConfigDict(extra="ignore")

    name: str
    homepage: str
    feeds: list[str] = []
    news_sitemap: Optional[str] = None
    home_state: Optional[str] = None
    owner_group: Optional[str] = None
    paywall_model: Optional[str] = None


SearchCandidate = Union[PressCandidate, OutletCandidate]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchModel:
    """Gemini with Google Search grounding, for short JSON answers.

    Attributes:
        calls (int): Requests sent in this review.
    """

    def __init__(self, settings: Settings, client: Optional[Any] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Initializes the model.

        Args:
            settings: The run settings.
            client: A ``genai.Client`` or a compatible test double; created from the settings' API key if omitted.
            sleep: Function used to wait between requests; replaceable in tests.
        """
        self._client = client if client is not None else create_client(settings.gemini_api_key,
                                                                         timeout_ms=SEARCH_TIMEOUT_MS)
        self._model = settings.gemini_model
        self._sleep = sleep
        self.calls = 0

    def ask(self, prompt: str) -> str:
        """Sends a prompt with Google Search enabled.

        Args:
            prompt: The prompt.

        Returns:
            str: The answer text; empty if the model returned none.

        Raises:
            SearchError: If MAX_SEARCH_CALLS requests were sent or the request failed.
        """
        if self.calls >= MAX_SEARCH_CALLS:
            raise SearchError(f"MAX_SEARCH_CALLS ({MAX_SEARCH_CALLS}) reached")
        if self.calls:
            self._sleep(MIN_SEARCH_INTERVAL_S)
        self.calls += 1
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=SEARCH_TEMPERATURE,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self._client.models.generate_content(model=self._model, contents=prompt, config=config)
        except errors.APIError as e:
            raise SearchError(f"Gemini request failed: {e}") from e
        except httpx.TransportError as e:
            raise SearchError(f"Network error after retries: {e!r}") from e
        usage = getattr(response, "usage_metadata", None)
        logger.info("Search %d: prompt %s tokens, output %s, thinking %s.", self.calls,
                    getattr(usage, "prompt_token_count", None), getattr(usage, "candidates_token_count", None),
                    getattr(usage, "thoughts_token_count", None))
        return getattr(response, "text", None) or ""


def parse_json_array(text: str) -> list[Any]:
    """Extracts the JSON array from an answer that may contain code fences or other text.

    Args:
        text: The answer.

    Returns:
        list: The parsed array.

    Raises:
        ValueError: If the answer contains no valid JSON array.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("the answer contains no JSON array")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("the answer is not a JSON array")
    return data


def parse_candidates(text: str, model: type[BaseModel]) -> list[Any]:
    """Parses a JSON array answer into candidates; invalid items are skipped.

    Args:
        text: The answer.
        model: ``PressCandidate`` or ``OutletCandidate``.

    Returns:
        list: The valid candidates, at most MAX_CANDIDATES_PER_SEARCH.

    Raises:
        ValueError: If the answer contains no valid JSON array.
    """
    candidates = []
    for item in parse_json_array(text):
        try:
            candidates.append(model.model_validate(item))
        except ValidationError:
            logger.info("Skipped an invalid candidate: %r", item)
    return candidates[:MAX_CANDIDATES_PER_SEARCH]


# ---------------------------------------------------------------------------
# Checks of the active sources
# ---------------------------------------------------------------------------

def count_items(data_dir: Path, since: date) -> Counter[str]:
    """Counts the stored news items and press releases per source, observed on or after a day.

    Args:
        data_dir: Root folder of the datasets.
        since: First day to count.

    Returns:
        Counter[str]: Records per source id.
    """
    counts: Counter[str] = Counter()
    cutoff = since.isoformat()
    for dataset in ITEM_DATASETS:
        for path in sorted((data_dir / dataset).glob("year=*/month=*/*.json")):
            # Files are partitioned by the day of the observing run: <dataset>_YYYY-MM-DD.json
            if path.stem.rsplit("_", 1)[-1] < cutoff:
                continue
            for record in json.loads(path.read_text(encoding="utf-8")):
                counts[str(record.get("source_id"))] += 1
    return counts


def check_feed(web: WebClient, url: str, source: Source, sitemap: bool = False) -> FeedCheck:
    """Fetches and parses a feed or news sitemap.

    Args:
        web: The HTTP client.
        url: URL of the feed or sitemap.
        source: The source the items belong to.
        sitemap: True for a news sitemap; the first child sitemaps of an index are read as well.

    Returns:
        FeedCheck: The outcome and the items.
    """
    try:
        response = web.get(url)
    except RobotsDisallowedError:
        return FeedCheck(url, "disallowed", "disallowed by robots.txt")
    except NotFoundError as e:
        return FeedCheck(url, "not_found", e.reason)
    except BlockedError as e:
        return FeedCheck(url, "blocked", e.reason)
    except FetchError as e:
        return FeedCheck(url, "error", e.reason)
    try:
        if sitemap and isinstance(source, Outlet):
            entries, children = parse_news_sitemap(response.content, source, url)
            for child in children[:MAX_CHILD_SITEMAPS]:
                try:
                    entries.extend(parse_news_sitemap(web.get(child).content, source, child)[0])
                except (FetchError, ValueError) as e:
                    logger.info("Child sitemap %s failed: %s", child, e)
        else:
            entries = parse_feed(response.content, source, url)
            if not entries and "html" in response.content_type:
                raise ValueError("not a feed (HTML page)")
    except ValueError as e:
        links = tuple(feed_links(response.text, response.url)) if "html" in response.content_type else ()
        return FeedCheck(url, "invalid", str(e)[:160], links=links)
    dates = [entry.published_at for entry in entries if entry.published_at is not None]
    moved_to = response.url if response.moved_permanently and response.url != url else None
    return FeedCheck(url, "ok", entries=tuple(entries), newest=max(dates, default=None), moved_to=moved_to)


def prefetch_robots(web: WebClient, urls: Iterable[str]) -> None:
    """Loads the robots.txt of every host first, so that crawl delays pass while the other hosts are read.

    Args:
        web: The HTTP client.
        urls: URLs of the hosts.
    """
    by_host: dict[str, str] = {}
    for url in urls:
        by_host.setdefault(host_of(url), url)
    for url in by_host.values():
        try:
            web.allowed(url)
        except FetchError:
            continue


def _source_urls(source: Source) -> list[str]:
    """Homepage, feeds and news sitemap of a source."""
    sitemap = source.news_sitemap if isinstance(source, Outlet) else None
    return [source.homepage, *source.feeds, *([sitemap] if sitemap else [])]


def lacks_yield(source: Source, items: int, today: date) -> bool:
    """Tells whether a source is proposed for removal because it yielded no political item.

    Args:
        source: The source.
        items: Its records in the last REVIEW_WINDOW_DAYS days.
        today: Day of the review.

    Returns:
        bool: True if the source was added at least REVIEW_WINDOW_DAYS days ago, has no
        records in that window and is not marked with ``keep = true``.
    """
    if source.keep or source.added is None or items:
        return False
    return (today - source.added).days >= REVIEW_WINDOW_DAYS


def review_source(web: WebClient, source: Source, health: SourceHealth, items: int,
                  now: datetime) -> tuple[list[Change], list[str]]:
    """Checks one active source.

    A source is proposed for removal if it is blocked or TDM reserved, has
    no working feed, is broken and still fails, requires a crawl delay above
    MAX_CRAWL_DELAY_S or yielded no political item in REVIEW_WINDOW_DAYS days.
    Otherwise single feeds are removed or replaced, and page fetching is
    turned off where robots.txt states a TDM reservation or a long crawl delay.

    Args:
        web: The HTTP client.
        source: The source.
        health: Its health from ``state/sources.json``.
        items: Its records in the last REVIEW_WINDOW_DAYS days.
        now: Time of the review (UTC).

    Returns:
        tuple[list[Change], list[str]]: The proposed changes and notes.
    """
    if health.status in ("blocked", "tdm_reserved"):
        # These hosts are not requested again
        reason = f"{health.status.replace('_', ' ')} since {health.last_attempt or 'unknown'}"
        if health.last_error:
            reason += f": {health.last_error}"
        return [Change("remove_source", source.id, reason)], []
    if lacks_yield(source, items, now.date()):
        return [Change("remove_source", source.id, f"no political items in the last {REVIEW_WINDOW_DAYS} days")], []

    changes: list[Change] = []
    notes: list[str] = []
    checks = [check_feed(web, url, source) for url in source.feeds]
    sitemap_check = None
    if isinstance(source, Outlet) and source.news_sitemap:
        sitemap_check = check_feed(web, source.news_sitemap, source, sitemap=True)
    all_checks = [*checks, *([sitemap_check] if sitemap_check else [])]
    blocked = next((check for check in all_checks if check.outcome == "blocked"), None)
    if blocked is not None:
        return [Change("remove_source", source.id, f"blocked: {blocked.detail} ({blocked.url})")], []

    stale_before = now - timedelta(days=STALE_FEED_DAYS)
    dropped: list[str] = []
    for check in all_checks:
        is_sitemap = check is sitemap_check
        label = "news sitemap" if is_sitemap else "feed"
        reason: Optional[str] = None
        if check.outcome in ("not_found", "disallowed"):
            reason = check.detail
        elif check.outcome == "ok" and check.newest is not None and check.newest < stale_before:
            reason = f"newest item from {check.newest.date().isoformat()}"
        if reason is not None:
            changes.append(Change("remove_sitemap" if is_sitemap else "remove_feed", source.id, reason, old=check.url))
            dropped.append(f"{label} {check.url}: {reason}")
        elif check.outcome == "ok" and check.moved_to and not is_sitemap:
            changes.append(Change("replace_feed", source.id, "moved permanently", old=check.url, new=check.moved_to))
        elif check.outcome in ("error", "invalid"):
            notes.append(f"`{source.id}`: {label} {check.url}: {check.detail}")
        elif check.outcome == "ok" and not check.entries:
            notes.append(f"`{source.id}`: {label} {check.url} has no items")
    if len(dropped) == len(all_checks):
        return [Change("remove_source", source.id, "no working feed: " + "; ".join(dropped))], notes
    working = any(check.outcome == "ok" for check in all_checks)
    if health.status == "broken":
        if not working:
            reason = f"broken since {health.last_success or 'the first run'}: {health.last_error or 'feeds fail'}"
            return [Change("remove_source", source.id, reason)], notes
        notes.append(f"`{source.id}`: broken in sources.json, but a feed works again")

    try:
        rules: Optional[RobotsRules] = web.robots_rules(source.homepage)
    except FetchError as e:
        notes.append(f"`{source.id}`: robots.txt of the homepage not checked ({e.reason})")
        rules = None
    delay = rules.crawl_delay if rules else None
    if delay is not None and delay > MAX_CRAWL_DELAY_S:
        return [Change("remove_source", source.id, f"robots.txt crawl delay {delay:g} s, more than "
                                                    f"{MAX_CRAWL_DELAY_S:g} s")], notes
    if source.fetch_pages and rules is not None and rules.tdm_statement:
        changes.append(Change("set_fetch_pages", source.id,
                              "robots.txt states a text-and-data-mining reservation in natural language"))
    elif source.fetch_pages and delay is not None and delay > MAX_PAGE_CRAWL_DELAY_S:
        changes.append(Change("set_fetch_pages", source.id, f"robots.txt crawl delay {delay:g} s"))
    return changes, notes


# ---------------------------------------------------------------------------
# Gaps and candidates
# ---------------------------------------------------------------------------

def find_gaps(config: SourceConfig, politicians: dict[str, Politician], leaving: frozenset[str] = frozenset()) -> Gaps:
    """Finds parliamentary groups, parties and states without a source.

    Args:
        config: The configuration.
        politicians: The politician registry (current mandates).
        leaving: Ids of sources that the review proposes to remove; they count as missing.

    Returns:
        Gaps: The gaps.
    """
    active = [source for source in config.active_sources if source.id not in leaving]
    press = [source for source in active if isinstance(source, PressSource)]
    with_group = {source.party for source in press if source.actor_type == "parliamentary_group"}
    with_party = {source.party for source in press if source.actor_type == "party"}
    governments = {source.jurisdiction for source in press if source.actor_type == "state_government"}
    dailies = {source.home_state for source in active
               if isinstance(source, Outlet) and source.outlet_type == "regional_daily"}
    not_a_party = {NO_FACTION, "OTHER", "CDU_CSU", None}
    groups = Counter(mandate.faction for politician in politicians.values() for mandate in politician.mandates
                     if mandate.parliament == "Bundestag")
    seats = Counter(politician.party for politician in politicians.values() if politician.mandates)
    return Gaps(
        bundestag_groups=tuple(sorted(code for code in groups if code not in (NO_FACTION, "OTHER", None)
                                      and code not in with_group)),
        parties=tuple(sorted(code for code in seats if code not in not_a_party and code not in with_party)),
        state_governments=tuple(code for code in STATE_NAMES if code not in governments),
        regional_dailies=tuple(code for code in STATE_NAMES if code not in dailies),
    )


def known_hosts(config: SourceConfig) -> set[str]:
    """Returns the hosts that the search must not propose.

    Args:
        config: The configuration.

    Returns:
        set[str]: Hosts (without ``www.``) of all sources, including removed ones, and of all
        exclusions; the review checks the exclusions with a temporary reason itself.
    """
    hosts: set[str] = set()
    for source in config.sources:
        for url in _source_urls(source):
            try:
                hosts.add(domain_id(url))
            except ValueError:
                continue
    hosts.update(domain_id(entry.homepage) for entry in config.excluded)
    return hosts


def known_feeds(config: SourceConfig) -> dict[str, str]:
    """Returns the listed feeds and news sitemaps, so that a candidate with the same feed is recognized.

    Args:
        config: The configuration.

    Returns:
        dict[str, str]: Source id by normalized URL, for all sources including removed ones.
    """
    feeds: dict[str, str] = {}
    for source in config.sources:
        for url in _source_urls(source)[1:]:
            try:
                feeds.setdefault(normalize_url(url), source.id)
            except ValueError:
                continue
    return feeds


def _actor_lines(gaps: Gaps) -> list[str]:
    """Describes the missing groups and parties for the search prompt."""
    lines = [f"- {PARTY_BY_CODE[code].name if code in PARTY_BY_CODE else code}: parliamentary group in the "
             f"Bundestag (actor_type parliamentary_group, code {code})" for code in gaps.bundestag_groups]
    lines.extend(f"- {PARTY_BY_CODE[code].name if code in PARTY_BY_CODE else code}: party (actor_type party, "
                 f"code {code})" for code in gaps.parties)
    return lines


def _state_lines(codes: Sequence[str]) -> str:
    """Lists states with their codes for a search prompt."""
    return "\n".join(f"- {STATE_NAMES[code]} ({code})" for code in codes)


def search_candidates(model: Any, gaps: Gaps, known: set[str], result: ReviewResult,
                      listed_federal: Sequence[str] = ()) -> list[tuple[SearchCategory, SearchCandidate]]:
    """Asks the search for candidates: the federal government always, the gaps where there are any.

    Args:
        model: A ``SearchModel`` or a compatible test double.
        gaps: The gaps.
        known: Hosts that must not be proposed.
        result: The review result; searches and notes are added.
        listed_federal: Names of the federal institutions in the list.

    Returns:
        list: Category and candidate, in the order of the answers.
    """
    rules = SEARCH_RULES.format(known=", ".join(sorted(known)) or "(none)")
    listed = "; ".join(listed_federal) or "(none)"
    requests: list[tuple[SearchCategory, str, type[BaseModel]]] = [
        ("federal", FEDERAL_PROMPT.format(rules=rules, listed=listed), PressCandidate),
    ]
    actors = _actor_lines(gaps)
    if actors:
        requests.append(("groups", GROUPS_PROMPT.format(actors="\n".join(actors), rules=rules), PressCandidate))
    if gaps.state_governments:
        requests.append(("states", STATES_PROMPT.format(states=_state_lines(gaps.state_governments), rules=rules),
                         PressCandidate))
    if gaps.regional_dailies:
        requests.append(("dailies", DAILIES_PROMPT.format(states=_state_lines(gaps.regional_dailies), rules=rules,
                                                          count=MAX_CANDIDATES_PER_SEARCH), OutletCandidate))
    found: list[tuple[SearchCategory, SearchCandidate]] = []
    for category, prompt, candidate_model in requests:
        try:
            answer = model.ask(prompt)
        except SearchError as e:
            result.notes.append(f"Search `{category}` failed: {e}")
            break
        result.searches.append(category)
        try:
            found.extend((category, candidate) for candidate in parse_candidates(answer, candidate_model))
        except ValueError as e:
            result.notes.append(f"Search `{category}` returned no usable list ({e})")
    return found


def feed_links(html: str, base_url: str) -> list[str]:
    """Returns the feeds that a page links: the RSS and Atom links of its head, then the
    links whose address looks like a feed or an overview page of feeds (FEED_LINK_PATTERN).

    Government websites often list their feeds as normal links on an overview page.

    Args:
        html: The page.
        base_url: URL of the page, for relative links.

    Returns:
        list[str]: Absolute http(s) URLs without duplicates, at most MAX_FEED_LINKS_PER_PAGE.
    """
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        rels = [value.lower() for value in (rel if isinstance(rel, list) else [rel])]
        if "alternate" in rels and str(tag.get("type") or "").lower() in FEED_TYPES:
            links.append(urljoin(base_url, str(tag["href"])))
    for tag in soup.find_all("a", href=True):
        url = urljoin(base_url, str(tag["href"]).strip())
        parts = urlsplit(url)
        target = f"{parts.path}?{parts.query}" if parts.query else parts.path
        if parts.scheme in ("http", "https") and FEED_LINK_PATTERN.search(target):
            links.append(url.split("#", 1)[0])
    return list(dict.fromkeys(links))[:MAX_FEED_LINKS_PER_PAGE]


def _new_id(base: str, taken: set[str]) -> str:
    """Returns a unique id from a base text and records it in ``taken``."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "source"
    candidate, number = cleaned, 2
    while candidate in taken:
        candidate, number = f"{cleaned}_{number}", number + 1
    taken.add(candidate)
    return candidate


def candidate_id(attributes: dict[str, Any], host: str, taken: set[str]) -> str:
    """Builds the id of a new source in the style of the configuration.

    Args:
        attributes: The validated attributes of the candidate.
        host: Host of its homepage.
        taken: Ids in use; the new id is added.

    Returns:
        str: For example ``cdu_csu_fraktion``, ``gruene``, ``state_bw`` or ``lvz``.
    """
    actor_type, party = attributes.get("actor_type"), attributes.get("party")
    if actor_type == "parliamentary_group" and party:
        return _new_id(f"{party}_fraktion", taken)
    if actor_type == "party" and party:
        return _new_id(party, taken)
    if actor_type == "state_government":
        return _new_id(f"state_{attributes['jurisdiction'][-2:]}", taken)
    return _new_id(host.split(".")[0], taken)


def _validated_attributes(category: SearchCategory, candidate: SearchCandidate, gaps: Gaps) -> dict[str, Any]:
    """Checks the attributes of a candidate against the vocabularies and the searched gap.

    Returns:
        dict[str, Any]: The attributes of the new entry.

    Raises:
        ValueError: If an attribute is missing, unknown or does not match the search.
    """
    if isinstance(candidate, OutletCandidate):
        if candidate.home_state not in gaps.regional_dailies:
            raise ValueError(f"home_state {candidate.home_state!r} was not searched for")
        paywall = candidate.paywall_model if candidate.paywall_model in get_args(PaywallModel) else "mixed"
        owner = " ".join((candidate.owner_group or "").split())[:80] or "unknown"
        return {"outlet_type": "regional_daily", "owner_group": owner, "home_state": candidate.home_state,
                "paywall_model": paywall}
    if category == "federal":
        actor_type = candidate.actor_type if candidate.actor_type in ("federal_government", "federal_ministry") \
            else None
        if actor_type is None:
            raise ValueError(f"actor_type {candidate.actor_type!r} is not a federal institution")
        return {"actor_type": actor_type, "party": None, "jurisdiction": "DE"}
    if category == "groups":
        wanted = {"parliamentary_group": gaps.bundestag_groups, "party": gaps.parties}.get(candidate.actor_type or "")
        if wanted is None or candidate.party not in wanted or candidate.party not in get_args(PartyCode):
            raise ValueError(f"{candidate.actor_type!r} with party {candidate.party!r} was not searched for")
        return {"actor_type": candidate.actor_type, "party": candidate.party, "jurisdiction": "DE"}
    if category != "states" or candidate.jurisdiction not in gaps.state_governments \
            or candidate.jurisdiction not in get_args(StateCode):
        raise ValueError(f"jurisdiction {candidate.jurisdiction!r} was not searched for")
    return {"actor_type": "state_government", "party": None, "jurisdiction": candidate.jurisdiction}


def _feed_order(url: str, outlet: bool) -> tuple[int, str]:
    """Sort key that puts politics feeds (outlets) or press feeds (press sources) first."""
    if outlet:
        preferred = bool(path_tokens(url) & POLITICS_PATH_TOKENS)
    else:
        preferred = any(token in url.lower() for token in PRESS_URL_TOKENS)
    return (0 if preferred else 1, url)


def evaluate_candidate(web: WebClient, category: SearchCategory, candidate: SearchCandidate, gaps: Gaps,
                       now: datetime, taken: set[str], known: set[str], known_feeds: dict[str, str],
                       source_id: Optional[str] = None) -> tuple[Optional[NewEntry], Optional[Rejection]]:
    """Checks a candidate with the fetch rules of the pipeline and builds its entry.

    The feeds are the proposed ones and those linked on the homepage; an HTML
    page among them, such as an overview page of the feeds of a ministry, is
    read for further feed links. A candidate is admitted if its robots.txt can
    be read, it declares no TDM reservation (TDMRep, header or meta tag), it
    does not block automated requests, it is not a source of the list under
    another domain (a homepage that redirects to a listed host, or only feeds
    that are listed), and its feeds are recent: a press source needs an item in the last
    PRESS_RECENT_DAYS days, an outlet MIN_OUTLET_POLITICAL_ITEMS political
    items in the last OUTLET_RECENT_DAYS days.

    Args:
        web: The HTTP client.
        category: Search category of the candidate.
        candidate: The candidate.
        gaps: The searched gaps.
        now: Time of the review (UTC).
        taken: Ids in use; the id of an admitted candidate is added.
        known: Hosts of the listed sources.
        known_feeds: Source id by normalized URL of every listed feed and news sitemap.
        source_id: Id of the new entry; built from the attributes if omitted.

    Returns:
        tuple: The new entry, or the rejection.
    """
    homepage = candidate.homepage.strip()
    proposed = tuple(dict.fromkeys(url.strip() for url in candidate.feeds if url.strip()))
    if urlsplit(homepage).scheme not in ("http", "https"):
        return None, Rejection(candidate.name, homepage, "homepage is not an http(s) URL")
    try:
        attributes = _validated_attributes(category, candidate, gaps)
    except ValueError as e:
        return None, Rejection(candidate.name, homepage, f"not used: {e}")
    outlet = isinstance(candidate, OutletCandidate)

    def reject(reason: str, status: Optional[str] = None) -> tuple[None, Rejection]:
        return None, Rejection(candidate.name, homepage, reason, status, "outlet" if outlet else "press_source",
                               attributes, proposed)

    provisional: Source
    if outlet:
        provisional = Outlet(id="candidate", name=candidate.name, homepage=homepage, outlet_type="regional_daily",
                             owner_group=attributes["owner_group"], paywall_model=attributes["paywall_model"],
                             feeds=(), home_state=attributes["home_state"])
    else:
        provisional = PressSource(id="candidate", name=candidate.name, homepage=homepage,
                                  actor_type=attributes["actor_type"], jurisdiction=attributes["jurisdiction"],
                                  feeds=(), party=attributes["party"])
    try:
        rules = web.robots_rules(homepage)
    except FetchError as e:
        return reject(f"robots.txt not readable ({e.reason})")
    delay = rules.crawl_delay if rules else None
    if delay is not None and delay > MAX_CRAWL_DELAY_S:
        return reject(f"robots.txt crawl delay {delay:g} s", "robots")
    if web.tdm_reserved(homepage):
        return reject("TDM reservation (tdmrep.json)", "tdm_reserved")

    host = domain_id(homepage)
    discovered: list[str] = []
    # A homepage that fails does not end the check: the proposed feeds may still work
    page_error: Optional[str] = None
    try:
        page = web.get(homepage)
    except RobotsDisallowedError:
        page = None
    except BlockedError as e:
        return reject(f"homepage {e.reason}", "blocked")
    except NotFoundError as e:
        page, page_error = None, f"homepage not found ({e.reason})"
    except FetchError as e:
        page, page_error = None, f"homepage not reachable ({e.reason})"
    if page is not None:
        final_host = domain_id(page.url)
        if final_host != host and final_host in known:
            return reject(f"homepage redirects to {final_host}, which is in the list", "duplicate")
        if web.tdm_reserved(page.url, page):
            return reject("TDM reservation (header)", "tdm_reserved")
        if "html" in page.content_type:
            if parse_page(page.text).tdm_reserved:
                return reject("TDM reservation (meta tag)", "tdm_reserved")
            discovered = feed_links(page.text, page.url)

    urls = sorted(dict.fromkeys([*proposed, *discovered]), key=lambda url: _feed_order(url, outlet))
    recent_after = now - timedelta(days=OUTLET_RECENT_DAYS if outlet else PRESS_RECENT_DAYS)
    feeds: list[str] = []
    duplicates: set[str] = set()
    recent = 0
    readable = False
    # (URL, whether an HTML page at the URL is read for further feed links); overview pages are read one level deep
    queue: deque[tuple[str, bool]] = deque((url, True) for url in urls)
    queued = set(urls)
    checked = 0
    while queue and len(feeds) < MAX_FEEDS_PER_SOURCE and checked < MAX_FEEDS_CHECKED_PER_CANDIDATE:
        url, expand = queue.popleft()
        listed = _listed_feed(known_feeds, url)
        if listed is not None:
            duplicates.add(listed)
            continue
        checked += 1
        check = check_feed(web, url, provisional)
        if check.outcome == "blocked":
            return reject(f"feed {check.detail}", "blocked")
        if check.outcome == "invalid" and expand and check.links:
            found = sorted((link for link in check.links if link not in queued),
                           key=lambda link: _feed_order(link, outlet))
            queued.update(found)
            queue.extendleft((link, False) for link in reversed(found))
            continue
        if check.outcome != "ok":
            continue
        listed = _listed_feed(known_feeds, check.moved_to) if check.moved_to else None
        if listed is not None:
            duplicates.add(listed)
            continue
        readable = True
        items = [entry for entry in check.entries if entry.published_at and entry.published_at >= recent_after
                 and (not outlet or looks_political(entry))]
        if items:
            feeds.append(check.moved_to or url)
            recent += len(items)
    sitemap: Optional[str] = None
    if outlet:
        sitemap_urls = [candidate.news_sitemap] if candidate.news_sitemap else []
        sitemap_urls.extend(url for url in (rules.sitemaps if rules else ()) if "news" in url.lower())
        for url in dict.fromkeys(sitemap_urls):
            listed = _listed_feed(known_feeds, url)
            if listed is not None:
                duplicates.add(listed)
                continue
            check = check_feed(web, url, provisional, sitemap=True)
            items = [entry for entry in check.entries if entry.published_at and entry.published_at >= recent_after
                     and looks_political(entry)]
            readable = readable or check.outcome == "ok"
            if check.outcome == "ok" and items:
                sitemap = url
                recent = max(recent, len(items))
                break
    if not feeds and sitemap is None:
        if duplicates:
            return reject("same feed as " + ", ".join(f"`{listed}`" for listed in sorted(duplicates)), "duplicate")
        if not readable and page_error is not None:
            return reject(f"{page_error}; no readable feed found")
        if not readable:
            return reject("no readable feed found", "no_feed")
        return reject("no feed with recent items", "inactive")
    minimum = MIN_OUTLET_POLITICAL_ITEMS if outlet else 1
    if recent < minimum:
        return reject(f"{recent} recent items, fewer than {minimum}", "inactive")

    fetch_pages = not (rules is not None and rules.tdm_statement) and not (delay is not None and
                                                                          delay > MAX_PAGE_CRAWL_DELAY_S)
    days = OUTLET_RECENT_DAYS if outlet else PRESS_RECENT_DAYS
    evidence = [f"{recent} {'political ' if outlet else ''}items in the last {days} days"]
    if delay is not None:
        evidence.append(f"robots.txt crawl-delay {delay:g} s")
    if rules is not None and rules.tdm_statement:
        evidence.append("robots.txt states a TDM reservation in natural language, therefore fetch_pages = false")
    comment = f"source review {now.date().isoformat()}: " + "; ".join(evidence)
    source_id = source_id or candidate_id(attributes, host, taken)
    if outlet:
        fields = {"id": source_id, "name": candidate.name, "homepage": homepage,
                  "outlet_type": attributes["outlet_type"], "owner_group": attributes["owner_group"],
                  "home_state": attributes["home_state"], "paywall_model": attributes["paywall_model"],
                  "feeds": feeds, "news_sitemap": sitemap, "fetch_pages": fetch_pages, "added": now.date()}
        return NewEntry("outlet", fields, comment), None
    fields = {"id": source_id, "name": candidate.name, "homepage": homepage, "actor_type": attributes["actor_type"],
              "party": attributes["party"], "jurisdiction": attributes["jurisdiction"], "feeds": feeds,
              "fetch_pages": fetch_pages, "added": now.date()}
    return NewEntry("press_source", fields, comment), None


def _listed_feed(known_feeds: dict[str, str], url: str) -> Optional[str]:
    """Returns the id of the listed source that has a feed or news sitemap with this URL, if any."""
    try:
        return known_feeds.get(normalize_url(url))
    except ValueError:
        return None


def _press_key(actor_type: Optional[str], party: Optional[str], jurisdiction: Optional[str]) -> Optional[str]:
    """Key of a party, parliamentary group or state government, of which one source is enough."""
    if actor_type in ("party", "parliamentary_group") and party:
        return f"{actor_type}:{party}"
    if actor_type == "state_government" and jurisdiction:
        return f"{actor_type}:{jurisdiction}"
    return None


def _candidate_key(category: SearchCategory, candidate: SearchCandidate) -> Optional[str]:
    """Key of the actor a candidate stands for; the state search does not return an actor type."""
    if category == "states" and isinstance(candidate, PressCandidate):
        return _press_key("state_government", None, candidate.jurisdiction)
    if category == "groups" and isinstance(candidate, PressCandidate):
        return _press_key(candidate.actor_type, candidate.party, None)
    return None


def _candidate_from_exclusion(entry: ExcludedSource) -> Optional[tuple[SearchCategory, SearchCandidate]]:
    """Rebuilds the search candidate of an exclusion, so that it can be checked again."""
    if entry.kind == "outlet":
        return "dailies", OutletCandidate(name=entry.name, homepage=entry.homepage, feeds=list(entry.feeds),
                                          home_state=entry.home_state, owner_group=entry.owner_group,
                                          paywall_model=entry.paywall_model)
    category = EXCLUSION_CATEGORIES.get(entry.actor_type or "")
    if entry.kind != "press_source" or category is None:
        return None
    return category, PressCandidate(name=entry.name, homepage=entry.homepage, feeds=list(entry.feeds),
                                    actor_type=entry.actor_type, jurisdiction=entry.jurisdiction, party=entry.party)


def exclusion_entry(rejection: Rejection, exclusion_id: str, today: date) -> NewEntry:
    """Builds the ``[[excluded]]`` entry of a rejected candidate, with the attributes for its recheck.

    Args:
        rejection: The rejection.
        exclusion_id: Id of the entry.
        today: Day of the review.

    Returns:
        NewEntry: The entry.
    """
    fields: dict[str, Any] = {"id": exclusion_id, "name": rejection.name, "homepage": rejection.homepage}
    if rejection.kind is not None:
        fields["kind"] = rejection.kind
        fields.update(rejection.attributes)
        if rejection.feeds:
            fields["feeds"] = list(rejection.feeds)
    fields.update(status=rejection.status, reason=rejection.reason, checked=today)
    return NewEntry("excluded", fields)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def run_review(settings: Settings, config_path: Path, web: WebClient, model: Optional[Any], now: datetime,
               clock: Callable[[], float] = time.monotonic) -> tuple[str, ReviewResult]:
    """Reviews the source list and returns the changed configuration.

    Args:
        settings: The run settings (data and state folders).
        config_path: The configuration file.
        web: The HTTP client.
        model: A ``SearchModel`` or a compatible test double; None skips the search, but
            not the recheck of the excluded candidates.
        now: Time of the review (UTC).
        clock: Monotonic clock in seconds, for the time budget.

    Returns:
        tuple[str, ReviewResult]: The text of the changed configuration and the result.

    Raises:
        ValueError: If the configuration or the changed configuration is invalid.
    """
    started = clock()
    config = load_config(config_path)
    health = SourceHealthStore.load(settings.state_dir)
    today = now.date()
    counts = count_items(settings.output_dir, today - timedelta(days=REVIEW_WINDOW_DAYS))
    result = ReviewResult()
    sources = list(config.active_sources)
    # Hosts that blocked requests or reserved TDM, and sources removed for low yield, are not requested
    requested = [source for source in sources if health.get(source.id).status not in ("blocked", "tdm_reserved")
                 and not lacks_yield(source, counts.get(source.id, 0), today)]
    prefetch_robots(web, (url for source in requested for url in _source_urls(source)))
    for position, source in enumerate(sources):
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append(f"Time budget reached: {len(sources) - position} sources not checked")
            break
        changes, notes = review_source(web, source, health.get(source.id), counts.get(source.id, 0), now)
        result.changes.extend(changes)
        result.notes.extend(notes)
        result.checked_sources += 1

    leaving = frozenset(change.source_id for change in result.changes if change.action == "remove_source")
    result.gaps = find_gaps(config, load_politicians(settings.state_dir), leaving)
    state = _CandidateState(
        known=known_hosts(config), known_feeds=known_feeds(config),
        taken={entry.id for entry in (*config.sources, *config.excluded)},
        press_keys={_press_key(source.actor_type, source.party, source.jurisdiction) for source in config.active_sources
                    if isinstance(source, PressSource) and source.id not in leaving},
    )
    _recheck_exclusions(web, config, result, state, now, clock, started)
    if model is not None:
        _search(web, model, config, result, state, now, clock, started)
    return apply_changes(config_path.read_text(encoding="utf-8"), config, result.changes, today), result


@dataclass
class _CandidateState:
    """What the recheck of the exclusions and the search share in one review.

    Attributes:
        known: Hosts of the listed sources and exclusions.
        known_feeds: Source id by normalized feed and news sitemap URL.
        taken: Ids in use.
        press_keys: Parties, parliamentary groups and state governments that have a source.
        additions: Sources admitted in this review.
    """
    known: set[str]
    known_feeds: dict[str, str]
    taken: set[str]
    press_keys: set[Optional[str]]
    additions: int = 0


def _admit(result: ReviewResult, state: _CandidateState, entry: NewEntry, key: Optional[str],
           candidate: SearchCandidate) -> bool:
    """Adds an admitted candidate to the changes, unless the limit of additions is reached.

    Returns:
        bool: True if the candidate was added.
    """
    if state.additions >= MAX_ADDITIONS_PER_REVIEW:
        result.notes.append(f"{candidate.name} ({candidate.homepage}) passes the checks; not added because of the "
                            f"limit of {MAX_ADDITIONS_PER_REVIEW} additions per review")
        return False
    state.additions += 1
    state.press_keys.add(key)
    sitemap = entry.fields.get("news_sitemap")
    urls = [*entry.fields.get("feeds", []), *([sitemap] if sitemap else [])]
    for url in urls:
        state.known_feeds[normalize_url(url)] = str(entry.fields["id"])
    result.changes.append(Change("add_source", str(entry.fields["id"]), entry.comment or "", entry=entry))
    return True


def _recheck_exclusions(web: WebClient, config: SourceConfig, result: ReviewResult, state: _CandidateState,
                        now: datetime, clock: Callable[[], float], started: float) -> None:
    """Checks the excluded candidates with a temporary reason again, without the search.

    A candidate that passes now is added under the id of its exclusion, and the
    exclusion is dropped. If its reason changed, the exclusion gets the new
    status; an unchanged result leaves the configuration as it is.
    """
    for exclusion in config.excluded:
        if not exclusion.rechecked:
            continue
        rebuilt = _candidate_from_exclusion(exclusion)
        if rebuilt is None:
            continue
        category, candidate = rebuilt
        key = _candidate_key(category, candidate)
        if key is not None and key in state.press_keys:
            continue
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append("Time budget reached: further excluded candidates not checked again")
            return
        result.rechecked += 1
        own_host = domain_id(exclusion.homepage)
        entry, rejection = evaluate_candidate(web, category, candidate, result.gaps, now, state.taken,
                                              state.known - {own_host}, state.known_feeds, source_id=exclusion.id)
        if entry is not None:
            if _admit(result, state, entry, key, candidate):
                result.changes.append(Change("drop_exclusion", exclusion.id, "admitted"))
            continue
        assert rejection is not None
        if rejection.status is None or rejection.status == exclusion.status:
            continue
        result.changes.append(Change("exclude", exclusion.id, rejection.reason,
                                     entry=exclusion_entry(rejection, exclusion.id, now.date())))


def _search(web: WebClient, model: Any, config: SourceConfig, result: ReviewResult, state: _CandidateState,
            now: datetime, clock: Callable[[], float], started: float) -> None:
    """Searches for candidates, evaluates them and adds the changes to the result."""
    listed = [source.name for source in config.active_sources if isinstance(source, PressSource)
              and source.actor_type in ("federal_government", "federal_ministry")]
    seen_hosts: set[str] = set()
    for category, candidate in search_candidates(model, result.gaps, state.known, result, listed):
        try:
            host = domain_id(candidate.homepage)
        except ValueError:
            result.rejections.append(Rejection(candidate.name, candidate.homepage, "homepage has no host"))
            continue
        if host in state.known or host in seen_hosts:
            continue
        key = _candidate_key(category, candidate)
        if key is not None and key in state.press_keys:
            continue
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append("Time budget reached: further candidates not evaluated")
            break
        seen_hosts.add(host)
        result.candidates += 1
        entry, rejection = evaluate_candidate(web, category, candidate, result.gaps, now, state.taken, state.known,
                                              state.known_feeds)
        if entry is not None:
            _admit(result, state, entry, key, candidate)
            continue
        assert rejection is not None
        result.rejections.append(rejection)
        if rejection.status is None:
            continue
        exclusion_id = candidate_id(rejection.attributes, host, state.taken) if rejection.attributes \
            else _new_id(host.split(".")[0], state.taken)
        result.changes.append(Change("exclude", exclusion_id, rejection.reason,
                                     entry=exclusion_entry(rejection, exclusion_id, now.date())))


# ---------------------------------------------------------------------------
# Configuration text
# ---------------------------------------------------------------------------

_TABLE_HEADER = re.compile(r"^\[\[(outlet|press_source|excluded)\]\]$")
_ID_LINE = re.compile(r'^id\s*=\s*"([^"]+)"$')


@dataclass(frozen=True)
class _Block:
    """An entry of the configuration text: the header line and the key lines up to ``end`` (exclusive)."""
    table: str
    source_id: str
    header: int
    end: int


def _blocks(lines: Sequence[str]) -> list[_Block]:
    """Finds the entries of the configuration text.

    Raises:
        ValueError: If an entry has no id line.
    """
    blocks = []
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.match(line.strip())
        if not match:
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith(("#", "[")):
            end += 1
        ids = [found.group(1) for found in (_ID_LINE.match(text.strip()) for text in lines[index + 1:end]) if found]
        if not ids:
            raise ValueError(f"The entry in line {index + 1} has no id")
        blocks.append(_Block(match.group(1), ids[0], index, end))
    return blocks


def toml_value(value: Any) -> str:
    """Formats a value as TOML.

    Args:
        value: A string, bool, date or a list of strings.

    Returns:
        str: The TOML literal.

    Raises:
        TypeError: For other types.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        # JSON string escapes are valid in TOML basic strings
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"Cannot write {type(value).__name__} as TOML")


def _render_entry(entry: NewEntry) -> list[str]:
    """Returns the lines of a new entry, preceded by a blank line."""
    lines = ["", *([f"# {entry.comment}"] if entry.comment else []), f"[[{entry.table}]]"]
    lines.extend(f"{key} = {toml_value(value)}" for key, value in entry.fields.items() if value is not None)
    return lines


def apply_changes(text: str, config: SourceConfig, changes: Sequence[Change], today: date) -> str:
    """Writes the changes into the configuration text and validates the result.

    Unchanged entries and all comments stay as they are. A removed source
    keeps its entry and gets ``removed`` and ``removed_reason``; feed and
    page-fetching changes get a comment line above the entry; new sources
    are appended to their table, exclusions to the excluded entries.

    Args:
        text: The configuration text.
        config: The parsed configuration.
        changes: The changes.
        today: Day of the review.

    Returns:
        str: The changed text; equal to ``text`` without changes.

    Raises:
        ValueError: If a change names an unknown entry or the result is not a valid configuration.
    """
    if not changes:
        return text
    lines = text.split("\n")
    blocks = _blocks(lines)
    by_id = {block.source_id: block for block in blocks}
    sources = {source.id: source for source in config.sources}
    comments: dict[int, list[str]] = {}
    values: dict[int, dict[str, Optional[str]]] = {}
    dropped: set[int] = set()
    feeds: dict[str, list[str]] = {}
    appended: dict[str, list[str]] = {"outlet": [], "press_source": [], "excluded": []}
    for change in changes:
        if change.action == "add_source" or (change.action == "exclude" and change.source_id not in by_id):
            assert change.entry is not None
            appended[change.entry.table].extend(_render_entry(change.entry))
            continue
        if change.source_id not in by_id:
            raise ValueError(f"No entry with the id {change.source_id}")
        block = by_id[change.source_id]
        keys = values.setdefault(block.header, {})
        note = comments.setdefault(block.header, [])
        if change.action == "drop_exclusion":
            dropped.add(block.header)
        elif change.action == "exclude":
            assert change.entry is not None
            for key in ("status", "reason", "checked"):
                keys[key] = toml_value(change.entry.fields[key])
        elif change.action == "remove_source":
            keys["removed"] = toml_value(today)
            keys["removed_reason"] = toml_value(change.reason)
        elif change.action in ("remove_feed", "replace_feed"):
            current = feeds.setdefault(change.source_id, list(sources[change.source_id].feeds))
            if change.action == "remove_feed":
                current[:] = [url for url in current if url != change.old]
                note.append(f"# source review {today.isoformat()}: feed {change.old} removed: {change.reason}")
            else:
                current[:] = list(dict.fromkeys(change.new if url == change.old else url for url in current))
                note.append(f"# source review {today.isoformat()}: feed {change.old} moved to {change.new}")
            keys["feeds"] = toml_value(current)
        elif change.action == "remove_sitemap":
            keys["news_sitemap"] = None
            note.append(f"# source review {today.isoformat()}: news sitemap {change.old} removed: {change.reason}")
        elif change.action == "set_fetch_pages":
            keys["fetch_pages"] = "false"
            note.append(f"# source review {today.isoformat()}: fetch_pages = false: {change.reason}")

    last_end = {table: max((block.end for block in blocks if block.table == table), default=None)
                for table in appended}
    inserts: dict[int, list[str]] = {}
    for table, new_lines in appended.items():
        if not new_lines:
            continue
        position = last_end[table]
        if position is None:
            if table != "excluded":
                raise ValueError(f"The configuration has no [[{table}]] entry to append to")
            position = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            new_lines = ["", "# Candidates that were checked and not admitted.", *new_lines[1:]]
        inserts.setdefault(position, []).extend(new_lines)

    out: list[str] = []
    block_at = {block.header: block for block in blocks}
    index = 0
    while index < len(lines):
        out.extend(inserts.get(index, []))
        block = block_at.get(index)
        if block is None:
            out.append(lines[index])
            index += 1
            continue
        if block.header in dropped:
            # Remove the blank line before the entry as well
            if out and out[-1] == "":
                out.pop()
            index = block.end
            continue
        if block.header in values:
            out.extend(comments.get(block.header, []))
            out.append(lines[index])
            keys = values[block.header]
            written: set[str] = set()
            for line in lines[index + 1:block.end]:
                key = line.split("=", 1)[0].strip()
                if key in keys:
                    written.add(key)
                    if keys[key] is not None:
                        out.append(f"{key} = {keys[key]}")
                    continue
                out.append(line)
            out.extend(f"{key} = {value}" for key, value in keys.items() if key not in written and value is not None)
            index = block.end
            continue
        out.append(lines[index])
        index += 1
    out.extend(inserts.get(len(lines), []))
    changed = "\n".join(out)
    validate_config_text(changed)
    return changed


def validate_config_text(text: str) -> SourceConfig:
    """Parses a configuration text with the rules of ``load_config``.

    Args:
        text: The configuration text.

    Returns:
        SourceConfig: The parsed configuration.

    Raises:
        ValueError: If the text is not a valid configuration.
    """
    # tomllib.TOMLDecodeError is a ValueError, as are the validation errors of load_config
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sources.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(text: Any) -> str:
    """Escapes a value for a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _party_label(code: str) -> str:
    """Name and code of a party for the report."""
    name = PARTY_BY_CODE[code].name if code in PARTY_BY_CODE else code
    return name if name == code else f"{name} ({code})"


def build_report(result: ReviewResult, config: SourceConfig, now: datetime, search: bool) -> str:
    """Builds the Markdown report of a review, used as pull request description and job summary.

    Args:
        result: The review result.
        config: The configuration before the changes.
        now: Time of the review (UTC).
        search: Whether the search was enabled.

    Returns:
        str: The Markdown text.
    """
    names = {source.id: source.name for source in (*config.sources, *config.excluded)}
    by_action: dict[str, list[Change]] = {}
    for change in result.changes:
        by_action.setdefault(change.action, []).append(change)
    removals = by_action.get("remove_source", [])
    edits = [change for change in result.changes
             if change.action in ("remove_feed", "replace_feed", "remove_sitemap", "set_fetch_pages")]
    additions = by_action.get("add_source", [])
    exclusions = by_action.get("exclude", [])
    lines = [
        f"## Source review {now:%Y-%m}",
        "",
        f"Checked {result.checked_sources} of {len(config.active_sources)} active sources and "
        f"{result.rechecked} excluded candidates again"
        + (f", and {result.candidates} candidates from {len(result.searches)} searches"
           if search else "; the search for new sources was turned off") + ".",
        "",
        f"**Proposed:** {len(removals)} removals, {len(edits)} changes of feeds or page fetching, "
        f"{len(additions)} additions, {len(exclusions)} exclusions of rejected candidates.",
    ]
    if removals:
        lines.extend(["", "### Removals", "",
                      "The entries stay in the configuration with the date of the review as `removed`.", "",
                      "| Source | Reason |", "|---|---|"])
        lines.extend(f"| {_cell(names.get(change.source_id, change.source_id))} (`{change.source_id}`) | "
                     f"{_cell(change.reason)} |" for change in removals)
    if edits:
        lines.extend(["", "### Feeds and page fetching", "", "| Source | Change | Reason |", "|---|---|---|"])
        for change in edits:
            what = {"remove_feed": f"remove feed {change.old}", "replace_feed": f"{change.old} → {change.new}",
                    "remove_sitemap": f"remove news sitemap {change.old}",
                    "set_fetch_pages": "fetch_pages = false"}[change.action]
            lines.append(f"| `{change.source_id}` | {_cell(what)} | {_cell(change.reason)} |")
    if additions:
        lines.extend(["", "### Additions", "", "| Source | Type | Feeds | Checks |", "|---|---|---|---|"])
        for change in additions:
            assert change.entry is not None
            entry = change.entry.fields
            kind = entry.get("actor_type") or entry.get("outlet_type")
            urls = [*entry.get("feeds", []), *([entry["news_sitemap"]] if entry.get("news_sitemap") else [])]
            lines.append(f"| {_cell(entry['name'])} (`{entry['id']}`) | {_cell(kind)} | "
                         f"{_cell(', '.join(urls))} | {_cell(change.reason.split(': ', 1)[-1])} |")
    if exclusions:
        lines.extend(["", "### Exclusions", "",
                      "Rejected candidates are added to the `[[excluded]]` entries, so that the search does not "
                      "propose them again. Every review checks the entries with the status no_feed, inactive or "
                      "robots again and adds them when they pass.", "",
                      "| Candidate | Status | Reason |", "|---|---|---|"])
        for change in exclusions:
            assert change.entry is not None
            entry = change.entry.fields
            lines.append(f"| {_cell(entry['name'])} ({_cell(entry['homepage'])}) | {entry['status']} | "
                         f"{_cell(entry['reason'])} |")
    temporary = [rejection for rejection in result.rejections if rejection.status is None]
    if temporary:
        lines.extend(["", "### Candidates not admitted for now", "",
                      "These candidates are not excluded; the next review may check them again.", "",
                      "| Candidate | Reason |", "|---|---|"])
        lines.extend(f"| {_cell(rejection.name)} ({_cell(rejection.homepage)}) | {_cell(rejection.reason)} |"
                     for rejection in temporary)
    gaps = result.gaps
    lines.extend([
        "", "### Gaps", "",
        "- **Bundestag groups without a press source:** "
        + (", ".join(_party_label(code) for code in gaps.bundestag_groups) or "none"),
        "- **Parties with seats without a press source:** "
        + (", ".join(_party_label(code) for code in gaps.parties) or "none"),
        "- **States without a state government source:** "
        + (", ".join(STATE_NAMES[code] for code in gaps.state_governments) or "none"),
        "- **States without a regional daily:** "
        + (", ".join(STATE_NAMES[code] for code in gaps.regional_dailies) or "none"),
    ])
    if result.notes:
        lines.extend(["", "### Notes", ""])
        lines.extend(f"- {note}" for note in result.notes[:MAX_REPORT_NOTES])
        if len(result.notes) > MAX_REPORT_NOTES:
            lines.append(f"- {len(result.notes) - MAX_REPORT_NOTES} further notes are in the workflow log")
    lines.extend([
        "", "---",
        "Merge this pull request to apply the changes, or edit `config/sources.toml` on its branch first. To keep "
        "a source that is proposed for removal because of low yield, set `keep = true` in its entry. The next "
        "review replaces the branch, so merge or close the pull request before then.",
    ])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parses the command-line arguments.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Reviews the source list and proposes changes.")
    parser.add_argument("--report", type=Path, required=True, help="path of the Markdown report to write")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="the source configuration to review")
    parser.add_argument("--output-dir", type=Path,
                        help="folder with the datasets and state (default: data/ in GitHub Actions, "
                             "~/Downloads/german_politics_dataset locally)")
    parser.add_argument("--search", action=argparse.BooleanOptionalAction, default=True,
                        help="search for new sources with Gemini and Google Search (default: on)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Runs the review, writes the changed configuration and the report.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 on success, 1 if the settings or the configuration are invalid.
    """
    args = parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError as e:
        logger.error("Invalid settings: %s", e)
        return 1
    if args.output_dir is not None:
        settings = replace(settings, output_dir=args.output_dir)
    model: Optional[SearchModel] = None
    if args.search:
        if settings.gemini_api_key:
            model = SearchModel(settings)
        else:
            logger.warning("GEMINI_API_KEY is not set; the review runs without the search for new sources.")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        config = load_config(args.config)
        with WebClient() as web:
            text, result = run_review(settings, args.config, web, model, now)
    except ValueError as e:
        logger.error("The review failed: %s", e)
        return 1
    if result.changes:
        args.config.write_text(text, encoding="utf-8")
    args.report.write_text(build_report(result, config, now, model is not None), encoding="utf-8")
    for note in result.notes:
        logger.info("Note: %s", note)
    logger.info("Source review: %d changes, %d notes, %d candidates; report written to %s.", len(result.changes),
                len(result.notes), result.candidates, args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
