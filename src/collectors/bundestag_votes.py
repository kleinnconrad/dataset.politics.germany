"""
Collects the roll-call votes of the Bundestag from the lists that the Bundestag publishes.

The Deutscher Bundestag publishes every roll-call vote (namentliche
Abstimmung) on www.bundestag.de as a PDF and an XLSX list with one row per
member: electoral period, session, vote number, parliamentary group, name and
vote. The page of roll-call votes loads its entries from a list
(``/ajax/filterlist/...``), newest first, 30 per request. The collector reads
that list until it reaches votes that are already stored or older than
FIRST_VOTE_DATE, downloads the XLSX list of each new vote and writes one
``roll_call_votes`` record per vote and one ``mp_votes`` record per member.
The requests follow robots.txt and the TDM reservations of the host.

Until 2026-11-13 the roll-call votes came from the abgeordnetenwatch API
(``collectors/parliament.py``), which answers automated requests with a
bot-protection challenge since 2026-11-24. This collector starts with the
votes after the last vote stored from abgeordnetenwatch, so that no vote is
stored twice. Compared with the abgeordnetenwatch records, the records have
the title that the Bundestag gives the vote, no topics or committees, no
printed papers, and no result (``accepted`` is null).

The lists contain no ids of the members. ``MemberMatcher`` assigns the
abgeordnetenwatch id of the politician registry to each member by name; the
members that cannot be matched are counted in the totals of the vote but get
no ``mp_votes`` record. ``config/politician_aliases.toml`` lists names that
the rules do not match.
"""
import io
import logging
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional
from zipfile import BadZipFile

import openpyxl
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag, TemplateString

from collectors.base import Collector, CollectorResult, RunContext
from collectors.feeds import TdmReservedError
from collectors.parliament import (
    MP_VOTES_DATASET,
    VOTES_DATASET,
    MemberVote,
    group_majorities,
    party_code,
    tally_votes,
    voted_against_group,
)
from entities import NO_FACTION, Politician, load_politicians, normalize_person_name
from schema import MpVoteRecord, RollCallVoteRecord
from state import CollectorEntry, content_hash, is_backfilled

logger = logging.getLogger(__name__)

SOURCE_ID = "bundestag"
LICENSE = "Deutscher Bundestag; official work (§ 5 UrhG)"
BASE_URL = "https://www.bundestag.de"
LIST_PAGE_URL = f"{BASE_URL}/parlament/plenum/abstimmung/liste"
ALIASES_PATH = Path(__file__).resolve().parents[2] / "config" / "politician_aliases.toml"
# First vote date read from the Bundestag; the votes before it are stored from abgeordnetenwatch
FIRST_VOTE_DATE = date(2026, 11, 14)
# Entries per list request (the page of the Bundestag uses 30) and an upper bound of list requests per run
PAGE_SIZE = 30
MAX_LIST_PAGES = 20
# Roll-call votes after which the records are written, so that an interrupted run keeps its progress
VOTES_PER_WRITE = 10
# An XLSX list of one vote has about 50 KB; larger files are rejected before they are unpacked
MAX_FILE_BYTES = 2_000_000
# Share of the rows that must name the same vote (period, session, number)
MIN_ID_SHARE = 0.95
# Labels of the electoral periods as stored by abgeordnetenwatch, so that the series keeps one label per period.
# A new electoral period must be added here when it starts.
PERIOD_LABELS: dict[int, str] = {20: "Bundestag 2021 - 2025", 21: "Bundestag 2025 - 2029"}
CHOICE_COLUMNS: dict[str, str] = {"ja": "yes", "nein": "no", "Enthaltung": "abstain", "ungültig": "invalid",
                                  "nichtabgegeben": "no_show"}
REQUIRED_COLUMNS: tuple[str, ...] = ("Wahlperiode", "Sitzungnr", "Abstimmnr", "Fraktion/Gruppe", "Name", "Vorname",
                                     *CHOICE_COLUMNS)
_LIST_PATH = re.compile(r"/ajax/filterlist/de/parlament/plenum/abstimmung/liste/[0-9-]+")
_DATE = re.compile(r"(\d{1,2})\.\s*([A-Za-zäÄ]+)\s+(\d{4})")
_MONTHS: dict[str, int] = {name: number for number, name in enumerate(
    ("januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november",
     "dezember"), start=1)}
_SUFFIX = re.compile(r"\s*\([^)]*\)")
_TRANSLITERATION = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ı": "i",
                                  "İ": "I"})


# ---------------------------------------------------------------------------
# List of votes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ListedVote:
    """A roll-call vote in the list of www.bundestag.de.

    Attributes:
        vote_date (date): Date of the vote.
        title (str): Title of the vote as given by the Bundestag.
        xlsx_url (Optional[str]): URL of the XLSX list; None if the entry has none.
    """
    vote_date: date
    title: str
    xlsx_url: Optional[str]

    @property
    def file_key(self) -> Optional[str]:
        """The file name of the XLSX list, which identifies the entry before the file is read."""
        return self.xlsx_url.rsplit("/", 1)[-1] if self.xlsx_url else None


def parse_list_page(html: str) -> str:
    """Returns the path of the list that the page of roll-call votes loads its entries from.

    Args:
        html: The page of roll-call votes.

    Returns:
        str: For example ``/ajax/filterlist/de/parlament/plenum/abstimmung/liste/462112-462112``.

    Raises:
        ValueError: If the page does not reference the list.
    """
    match = _LIST_PATH.search(html)
    if match is None:
        raise ValueError(f"The page {LIST_PAGE_URL} references no list of roll-call votes; its format may have changed.")
    return match.group(0)


def _parse_german_date(text: str) -> Optional[date]:
    """Parses a date such as "13. November 2026"; returns None if the text contains none."""
    match = _DATE.search(text)
    if match is None or match.group(2).lower() not in _MONTHS:
        return None
    return date(int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1)))


def _text(tag: Tag) -> str:
    """Returns the text of a tag with single spaces, including text inside ``<template>`` elements.

    The list response wraps its rows in templates, whose strings ``get_text`` leaves out by default.
    """
    return " ".join(tag.get_text(" ", strip=True, types=(NavigableString, TemplateString)).split())


def parse_vote_list(html: str) -> list[ListedVote]:
    """Reads the entries of one page of the list of roll-call votes.

    The response lists each vote twice, as a table row and as a list item
    for small screens, each in a ``<template>`` element; the table rows are read.

    Args:
        html: The list response.

    Returns:
        list[ListedVote]: The entries in the order of the list (newest first);
        rows without a date or a link are left out.
    """
    votes = []
    for row in BeautifulSoup(html, "html.parser").select("tr.m-documents__tableRow"):
        cells = row.find_all("td")
        anchors = row.find_all("a", href=True)
        vote_date = _parse_german_date(_text(cells[0])) if cells else None
        if vote_date is None or not anchors:
            logger.warning("Skipped a row of the roll-call list without date or link: %s", _text(row)[:200])
            continue
        title_tag = anchors[0].find("span")
        title = _text(title_tag if isinstance(title_tag, Tag) else anchors[0])
        xlsx = next((anchor["href"] for anchor in anchors if anchor["href"].lower().endswith(".xlsx")), None)
        if xlsx is not None and xlsx.startswith("/"):
            xlsx = BASE_URL + xlsx
        votes.append(ListedVote(vote_date=vote_date, title=title, xlsx_url=xlsx))
    return votes


# ---------------------------------------------------------------------------
# XLSX list of one vote
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemberRow:
    """The vote of one member as listed in the XLSX file.

    Attributes:
        first_name (str): First name (Vorname).
        last_name (str): Last name (Name), possibly with a suffix such as "(Fulda)".
        group_label (str): Parliamentary group as written in the file, e.g. "BÜ90/GR".
        choice (str): yes, no, abstain, invalid or no_show.
    """
    first_name: str
    last_name: str
    group_label: str
    choice: str


@dataclass(frozen=True)
class VoteFile:
    """The content of the XLSX list of one vote.

    Attributes:
        period (int): Electoral period (Wahlperiode).
        session (int): Number of the plenary session (Sitzungnr).
        number (int): Number of the vote in the session (Abstimmnr).
        members (tuple[MemberRow, ...]): One entry per member.
        skipped_rows (int): Rows without exactly one vote.
    """
    period: int
    session: int
    number: int
    members: tuple[MemberRow, ...]
    skipped_rows: int = 0

    @property
    def vote_id(self) -> str:
        """The id of the vote: ``<period>-<session>-<number>``, e.g. ``21-109-4``."""
        return f"{self.period}-{self.session}-{self.number}"


def parse_vote_file(content: bytes) -> VoteFile:
    """Reads the XLSX list of one roll-call vote.

    openpyxl parses the XML parts of the file with defusedxml, which rejects
    entity declarations.

    Args:
        content: The XLSX file.

    Returns:
        VoteFile: Period, session, number and the votes of the members.

    Raises:
        ValueError: If the file is too large, not a workbook, lacks a required
            column or lists more than one vote.
    """
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"The XLSX list has {len(content)} bytes, more than {MAX_FILE_BYTES}.")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (BadZipFile, KeyError, OSError) as e:
        raise ValueError(f"The file is not a readable XLSX workbook: {e}") from e
    try:
        rows = [row for row in workbook.worksheets[0].iter_rows(values_only=True)
                if any(value is not None for value in row)]
    finally:
        workbook.close()
    if not rows:
        raise ValueError("The XLSX list is empty.")
    header = [str(value).strip() if value is not None else "" for value in rows[0]]
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValueError(f"The XLSX list lacks the columns {', '.join(missing)}.")
    column = {name: header.index(name) for name in REQUIRED_COLUMNS}
    ids = Counter((row[column["Wahlperiode"]], row[column["Sitzungnr"]], row[column["Abstimmnr"]]) for row in rows[1:])
    if not ids:
        raise ValueError("The XLSX list has no member rows.")
    (period_value, session_value, number_value), count = ids.most_common(1)[0]
    # Single rows with a typo in the id occur (one of 733 rows in the list of 2025-01-30); a list with
    # several votes is rejected
    if count < MIN_ID_SHARE * sum(ids.values()):
        raise ValueError(f"The XLSX list contains several votes: {dict(ids)}")
    if len(ids) > 1:
        logger.warning("XLSX list of vote %s-%s-%s: %d rows name another vote %s; they are counted for this vote.",
                       period_value, session_value, number_value, sum(ids.values()) - count,
                       [key for key in ids if key != (period_value, session_value, number_value)])
    try:
        period, session, number = int(period_value), int(session_value), int(number_value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"The XLSX list has an invalid vote id: {period_value}, {session_value}, {number_value}") from e
    members: list[MemberRow] = []
    skipped = 0
    for row in rows[1:]:
        choices = [choice for name, choice in CHOICE_COLUMNS.items() if row[column[name]] == 1]
        if len(choices) != 1:
            skipped += 1
            continue
        members.append(MemberRow(
            first_name=str(row[column["Vorname"]] or "").strip(),
            last_name=str(row[column["Name"]] or "").strip(),
            group_label=str(row[column["Fraktion/Gruppe"]] or "").strip(),
            choice=choices[0],
        ))
    return VoteFile(period=period, session=session, number=number, members=tuple(members), skipped_rows=skipped)


# ---------------------------------------------------------------------------
# Matching members to politician ids
# ---------------------------------------------------------------------------

def fold_name(name: str) -> str:
    """Normalizes a name for matching between the lists of the Bundestag and the registry.

    In addition to ``normalize_person_name``, umlauts are written as ae, oe
    and ue (the lists write "Koegel" where the registry has "Kögel") and the
    dotless i is folded.

    Args:
        name: For example "Dr. Jürgen Kögel".

    Returns:
        str: For example "juergen koegel".
    """
    return normalize_person_name(name.translate(_TRANSLITERATION))


def _first_part(folded_first_name: str) -> str:
    """Returns the first given name: the first word, and of a hyphenated name the first part."""
    words = folded_first_name.split()
    return words[0].split("-")[0] if words else ""


def load_aliases(path: Path = ALIASES_PATH) -> dict[str, str]:
    """Reads the names of members that the matching rules do not resolve.

    Args:
        path: The TOML file with ``[[alias]]`` entries (``name``, ``politician_id``).

    Returns:
        dict[str, str]: Politician id by folded name; empty if the file does not exist.

    Raises:
        ValueError: If an entry lacks a field.
    """
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    aliases: dict[str, str] = {}
    for item in data.get("alias", []):
        name, politician_id = item.get("name"), item.get("politician_id")
        if not isinstance(name, str) or not isinstance(politician_id, str) or not name or not politician_id:
            raise ValueError(f"Alias entries in {path.name} need a name and a politician_id: {item}")
        aliases[fold_name(name)] = politician_id
    return aliases


class MemberMatcher:
    """Assigns the abgeordnetenwatch id of the politician registry to the members in a roll-call list.

    The rules, in this order: an entry of the alias file; the full name
    without suffixes such as "(Fulda)"; the first given name and the last
    word of the last name, narrowed to the parliamentary group if several
    members share them. Only politicians with a Bundestag mandate are
    considered. A name that matches no or several politicians gets no id.
    """

    def __init__(self, politicians: dict[str, Politician], aliases: Optional[dict[str, str]] = None) -> None:
        """Builds the lookup tables.

        Args:
            politicians: The politician registry, by abgeordnetenwatch id.
            aliases: Politician id by folded name, from ``load_aliases``.
        """
        self._aliases = aliases or {}
        self._by_full_name: dict[str, set[str]] = defaultdict(set)
        self._by_first_last: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._factions: dict[str, set[str]] = defaultdict(set)
        for politician_id, politician in politicians.items():
            mandates = [mandate for mandate in politician.mandates if mandate.parliament == "Bundestag"]
            if not mandates:
                continue
            self._factions[politician_id] = {mandate.faction for mandate in mandates if mandate.faction}
            first = politician.first_name or politician.name.split()[0]
            last = politician.last_name or politician.name.split()[-1]
            for name in {politician.name, f"{first} {last}"}:
                self._by_full_name[fold_name(name)].add(politician_id)
            folded_last = fold_name(last).split()
            if folded_last:
                self._by_first_last[(_first_part(fold_name(first)), folded_last[-1])].add(politician_id)

    def __len__(self) -> int:
        return len(self._factions)

    def match(self, first_name: str, last_name: str, group: Optional[str]) -> Optional[str]:
        """Returns the politician id of a member of a roll-call list.

        Args:
            first_name: First name as in the list.
            last_name: Last name as in the list, possibly with a suffix such as "(Fulda)".
            group: Parliamentary group as party code (NONE without group), or None.

        Returns:
            Optional[str]: The abgeordnetenwatch id, or None if no or several politicians match.
        """
        last_name = _SUFFIX.sub("", last_name).strip()
        full = fold_name(f"{first_name} {last_name}")
        if full in self._aliases:
            return self._aliases[full]
        candidates = self._by_full_name.get(full, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        folded_last = fold_name(last_name).split()
        if not candidates and folded_last:
            candidates = self._by_first_last.get((_first_part(fold_name(first_name)), folded_last[-1]), set())
        if len(candidates) > 1 and group:
            candidates = {candidate for candidate in candidates if self._in_group(candidate, group)}
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _in_group(self, politician_id: str, group: str) -> bool:
        """Tells whether a politician's Bundestag mandate is in a group; CDU_CSU covers CDU and CSU."""
        factions = self._factions.get(politician_id, set())
        if group == "CDU_CSU":
            return bool(factions & {"CDU_CSU", "CDU", "CSU"})
        return group in factions


def member_votes(vote_file: VoteFile, matcher: MemberMatcher, source_url: str) -> tuple[list[MemberVote], list[str]]:
    """Converts the rows of a roll-call list to member votes with politician ids.

    An invalid vote (ungültig) is counted as no_show, the category in which
    abgeordnetenwatch reports the members who cast no valid vote. A politician
    id that two rows of one list match is dropped for both rows.

    Args:
        vote_file: The parsed list.
        matcher: The member matcher.
        source_url: URL of the XLSX list.

    Returns:
        tuple: The member votes, and the names of the members without a politician id.
    """
    rows = []
    for row in vote_file.members:
        group = party_code(row.group_label) or NO_FACTION
        rows.append((row, group, matcher.match(row.first_name, row.last_name, group)))
    counts = Counter(politician_id for _row, _group, politician_id in rows if politician_id is not None)
    members: list[MemberVote] = []
    unmatched: list[str] = []
    for row, group, politician_id in rows:
        if politician_id is not None and counts[politician_id] > 1:
            politician_id = None
        if politician_id is None:
            unmatched.append(f"{row.first_name} {row.last_name}")
        choice = "no_show" if row.choice == "invalid" else row.choice
        members.append(MemberVote(vote_url=source_url, politician_id=politician_id, faction=group, choice=choice))
    return members, unmatched


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class BundestagVotesCollector(Collector):
    """Writes the roll-call votes of the Bundestag and the votes of the single members from www.bundestag.de."""
    name = "bundestag_votes"
    datasets = (VOTES_DATASET, MP_VOTES_DATASET)
    source_id = SOURCE_ID

    def __init__(self, aliases_path: Path = ALIASES_PATH, first_vote_date: date = FIRST_VOTE_DATE) -> None:
        """Initializes the collector.

        Args:
            aliases_path: The alias file of member names.
            first_vote_date: Votes before this date are not read.
        """
        self._aliases_path = aliases_path
        self._first_vote_date = first_vote_date

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the list of roll-call votes and writes the votes that are not stored yet.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the collector keeps no cursor.

        Raises:
            ValueError: If the politician registry has no Bundestag members, or a list has an unknown format.
            TdmReservedError: If the host declares a text-and-data-mining reservation.
        """
        matcher = MemberMatcher(load_politicians(ctx.settings.state_dir), load_aliases(self._aliases_path))
        if not len(matcher):
            raise ValueError("The politician registry contains no members of the Bundestag.")
        counters: Counter[str] = Counter()
        new = self._new_votes(ctx, counters)
        seen = ctx.seen(VOTES_DATASET)
        batch: list[tuple[ListedVote, list[MemberVote], dict[str, Any]]] = []
        complete = True
        for position, listed in enumerate(new):
            if ctx.past_deadline():
                counters["deferred"] += len(new) - position
                complete = False
                break
            if listed.xlsx_url is None:
                continue
            response = ctx.web.get(listed.xlsx_url)
            if ctx.web.tdm_reserved(response.url, response):
                raise TdmReservedError(listed.xlsx_url)
            vote_file = parse_vote_file(response.content)
            if vote_file.vote_id in seen or any(vote_file.vote_id == fields["vote_id"] for *_, fields in batch):
                logger.warning("Bundestag vote %s is listed twice (%s); the second entry is skipped.",
                               vote_file.vote_id, listed.file_key)
                counters["duplicates"] += 1
                continue
            members, unmatched = member_votes(vote_file, matcher, listed.xlsx_url)
            if unmatched:
                logger.warning("Bundestag vote %s: %d members without a politician id: %s", vote_file.vote_id,
                               len(unmatched), ", ".join(unmatched[:20]))
            counters["skipped_rows"] += vote_file.skipped_rows
            counters["invalid_votes"] += sum(1 for row in vote_file.members if row.choice == "invalid")
            batch.append((listed, members, self._fields(vote_file, listed, members)))
            if len(batch) >= VOTES_PER_WRITE:
                self._write(ctx, entry, batch, counters)
                batch = []
        self._write(ctx, entry, batch, counters)
        logger.info("Bundestag roll-call votes: %d new, %d member votes, %d members without a politician id, "
                    "%d deferred to later runs.", counters["new"], counters["member_votes"],
                    counters["member_votes_without_politician"], counters["deferred"])
        return CollectorResult(counters=dict(counters), complete=complete)

    def _new_votes(self, ctx: RunContext, counters: Counter[str]) -> list[ListedVote]:
        """Reads the list of roll-call votes until it reaches stored votes or the first vote date.

        Args:
            ctx: The run context.
            counters: Counters of the run, updated in place.

        Returns:
            list[ListedVote]: The new votes with an XLSX list, oldest first.

        Raises:
            ValueError: If the list has an unknown format.
            TdmReservedError: If the host declares a text-and-data-mining reservation.
        """
        page = ctx.web.get(LIST_PAGE_URL)
        if ctx.web.tdm_reserved(page.url, page):
            raise TdmReservedError(LIST_PAGE_URL)
        list_url = BASE_URL + parse_list_page(page.text)
        seen = ctx.seen(VOTES_DATASET)
        new: list[ListedVote] = []
        for page_number in range(MAX_LIST_PAGES):
            params = {"limit": PAGE_SIZE, "noFilterSet": "true", "offset": page_number * PAGE_SIZE}
            listed = parse_vote_list(ctx.web.get(list_url, params=params).text)
            if page_number == 0 and not listed:
                raise ValueError(f"The list {list_url} has no roll-call votes; its format may have changed.")
            reached_known = False
            for vote in listed:
                if vote.vote_date < self._first_vote_date:
                    reached_known = True
                elif vote.file_key is None:
                    logger.warning("Bundestag vote of %s (%s) has no XLSX list yet; it is read in a later run.",
                                   vote.vote_date, vote.title)
                    counters["without_file"] += 1
                elif f"file:{vote.file_key}" in seen:
                    reached_known = True
                elif vote not in new:
                    new.append(vote)
            if reached_known or len(listed) < PAGE_SIZE:
                break
        return list(reversed(new))

    @staticmethod
    def _fields(vote_file: VoteFile, listed: ListedVote, members: list[MemberVote]) -> dict[str, Any]:
        """Returns the record-specific fields of a roll-call vote.

        Raises:
            ValueError: If the electoral period has no label in PERIOD_LABELS.
        """
        label = PERIOD_LABELS.get(vote_file.period)
        if label is None:
            raise ValueError(f"Electoral period {vote_file.period} has no label in PERIOD_LABELS; add it.")
        return {
            "vote_id": vote_file.vote_id,
            "parliament_period": label,
            "vote_date": listed.vote_date,
            "title": listed.title,
            "topics": [],
            "committees": [],
            "printed_papers": [],
            "accepted": None,
            **tally_votes(members),
        }

    @staticmethod
    def _write(
        ctx: RunContext,
        entry: CollectorEntry,
        batch: list[tuple[ListedVote, list[MemberVote], dict[str, Any]]],
        counters: Counter[str],
    ) -> None:
        """Emits the member votes and then the roll-call votes of a batch.

        The votes are marked as seen only after their member votes are
        written, so that an interrupted run reads them again.

        Args:
            ctx: The run context.
            entry: Run history of the collector.
            batch: List entry, member votes and record fields per vote.
            counters: Counters of the run, updated in place.
        """
        if not batch:
            return
        mp_records: list[MpVoteRecord] = []
        for listed, members, fields in batch:
            backfilled = is_backfilled(fields["vote_date"], entry.first_run)
            majorities = group_majorities(members)
            for member in members:
                if member.politician_id is None:
                    counters["member_votes_without_politician"] += 1
                    continue
                mp_records.append(MpVoteRecord(
                    **ctx.envelope(
                        MP_VOTES_DATASET, f"bt-vote:{fields['vote_id']}:{member.politician_id}", revision=0,
                        source_id=SOURCE_ID, source_url=listed.xlsx_url, license=LICENSE, jurisdiction="DE",
                        backfilled=backfilled,
                    ),
                    vote_id=fields["vote_id"],
                    vote_date=fields["vote_date"],
                    politician_id=member.politician_id,
                    faction=member.faction,
                    vote=member.choice,
                    voted_against_faction=voted_against_group(member, majorities),
                ))
        ctx.emit(MP_VOTES_DATASET, mp_records)
        counters["member_votes"] += len(mp_records)

        seen = ctx.seen(VOTES_DATASET)
        vote_records: list[RollCallVoteRecord] = []
        for listed, _members, fields in batch:
            revision = seen.mark(fields["vote_id"], content_hash(fields), ctx.today)
            seen.mark(f"file:{listed.file_key}", fields["vote_id"], ctx.today)
            vote_records.append(RollCallVoteRecord(
                **ctx.envelope(
                    VOTES_DATASET, f"bt-vote:{fields['vote_id']}", revision=revision, source_id=SOURCE_ID,
                    source_url=listed.xlsx_url, license=LICENSE, jurisdiction="DE",
                    backfilled=is_backfilled(fields["vote_date"], entry.first_run),
                ),
                **fields,
            ))
        ctx.emit(VOTES_DATASET, vote_records)
        counters["new"] += len(vote_records)
