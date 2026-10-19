"""
Collects politicians and roll-call votes from the abgeordnetenwatch API v2.

abgeordnetenwatch publishes parliaments, mandates, politicians, roll-call
votes and the votes of the single members under CC0 1.0. The API allows 30
requests per minute per IP address; the pipeline keeps 2.1 seconds between
requests to www.abgeordnetenwatch.de (``WebClient.host_intervals``), and the
collectors request lists of up to 1,000 entities at once. The robots.txt of
the host disallows crawlers; the documented API is used under its own terms.

``PoliticiansCollector`` reads the current mandates of the Bundestag and of
the 16 state parliaments, rebuilds the politician registry
(``state/politicians.json``) and writes a record for each politician who is
new or whose name, party, sex, year of birth, Wikidata id or mandates changed.
It runs once a week.

``RollCallVotesCollector`` reads the roll-call votes (polls) of the Bundestag
and writes one record per vote, with the counts by parliamentary group, and
one record per member and vote. A vote does not change once it is held, so
each vote is read once. The first run imports the current and the previous
electoral period; at most ``MAX_NEW_POLLS_PER_RUN`` votes are read per run
(no limit in a backfill run), and the rest follow in later runs. The
collector also stops at the run deadline; the remaining votes follow in the
next run.
"""
import html
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from entities import NO_FACTION, Mandate, Politician, load_politicians, resolve_party, save_politicians
from schema import MpVoteRecord, PoliticianRecord, RollCallVoteRecord
from state import CollectorEntry, content_hash, is_backfilled

logger = logging.getLogger(__name__)

SOURCE_ID = "abgeordnetenwatch"
LICENSE = "CC0-1.0"
API_URL = "https://www.abgeordnetenwatch.de/api/v2/"
POLITICIANS_DATASET = "politicians"
VOTES_DATASET = "roll_call_votes"
MP_VOTES_DATASET = "mp_votes"
# Entities per list request (range_end); the API maximum
PAGE_SIZE = 1000
# Politician ids per request of the politicians list, so that the URL stays short
POLITICIAN_IDS_PER_REQUEST = 200
BUNDESTAG_ID = 5
# New roll-call votes read per run except in backfill runs. Each needs one request at the abgeordnetenwatch
# interval, so that a regular run stays short.
MAX_NEW_POLLS_PER_RUN = 120
# Roll-call votes after which the records are written, so that an interrupted run keeps its progress
POLLS_PER_WRITE = 20

# abgeordnetenwatch parliament id -> jurisdiction. The European Parliament (id 1) is not read.
JURISDICTION_BY_PARLIAMENT_ID: dict[int, str] = {
    5: "DE", 6: "DE-BW", 13: "DE-BY", 2: "DE-BE", 16: "DE-BB", 10: "DE-HB", 3: "DE-HH", 11: "DE-HE", 9: "DE-MV",
    12: "DE-NI", 4: "DE-NW", 7: "DE-RP", 14: "DE-SL", 17: "DE-SN", 8: "DE-ST", 18: "DE-SH", 15: "DE-TH",
}
SEX_BY_CODE: dict[str, str] = {"f": "female", "m": "male", "d": "diverse"}
VOTE_CHOICES: tuple[str, ...] = ("yes", "no", "abstain", "no_show")
OTHER_PARTY = "OTHER"
# Link to a Bundestag printed paper (Drucksache): .../btd/21/078/2107869.pdf is 21/7869
_PRINTED_PAPER_LINK = re.compile(r"dserver\.bundestag\.de/btd/(\d{2})/\d{3}/(\d{2})(\d{5})\.pdf")
_HTML_TAG = re.compile(r"<[^>]+>")
# Soft hyphens and zero-width characters, which occur in some group names (e.g. "BÜNDNIS 90/\xadDIE GRÜNEN")
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "­​‌‍﻿"))


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def fetch_list(ctx: RunContext, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Reads all entities of an API list, PAGE_SIZE entities per request.

    Args:
        ctx: The run context.
        path: Path of the list below API_URL, e.g. ``polls``.
        params: Filters of the list.

    Returns:
        list[dict]: The entities.

    Raises:
        ValueError: If a response is not a successful list response.
    """
    entities: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_params = dict(params)
        if offset:
            page_params["range_start"] = offset
        page_params["range_end"] = PAGE_SIZE
        body = ctx.web.get(API_URL + path, params=page_params, respect_robots=False).json()
        meta = body.get("meta") if isinstance(body, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(meta, dict) or meta.get("status") != "ok" or not isinstance(data, list):
            message = meta.get("status_message") if isinstance(meta, dict) else "no meta object"
            raise ValueError(f"abgeordnetenwatch returned no list for {path}: {message}")
        entities.extend(item for item in data if isinstance(item, dict))
        offset += len(data)
        total = (meta.get("result") or {}).get("total")
        if not data or not isinstance(total, int) or offset >= total:
            return entities


def clean_text(value: Any) -> str:
    """Returns a label or title as plain text: no HTML tags, entities, invisible characters or extra spaces.

    Args:
        value: The raw value; anything but a string gives an empty string.

    Returns:
        str: The cleaned text.
    """
    if not isinstance(value, str):
        return ""
    text = html.unescape(_HTML_TAG.sub(" ", value)).translate(_INVISIBLE_CHARS)
    return " ".join(text.split())


def strip_period(label: Any, period_label: str) -> Optional[str]:
    """Removes the period suffix that abgeordnetenwatch appends to labels.

    Args:
        label: For example "120 - Recklinghausen I (Bundestag 2025 - 2029)".
        period_label: For example "Bundestag 2025 - 2029".

    Returns:
        Optional[str]: For example "120 - Recklinghausen I", or None for an empty label.
    """
    text = clean_text(label)
    suffix = f" ({period_label})"
    if period_label and text.endswith(suffix):
        text = text[: -len(suffix)].strip()
    return text or None


def party_code(label: Any) -> Optional[str]:
    """Returns the code of a party or parliamentary group label of abgeordnetenwatch.

    Args:
        label: For example "BÜNDNIS 90/\\xadDIE GRÜNEN (Bundestag 2025 - 2029)", "parteilos" or "fraktionslos".

    Returns:
        Optional[str]: The party code, NONE without party or group, OTHER for
        parties that are not in the registry, or None for an empty label.
    """
    text = clean_text(label)
    if not text:
        return None
    code = resolve_party(text)
    if code is None:
        logger.debug("abgeordnetenwatch label %r is not a known party; stored as %s.", text, OTHER_PARTY)
        return OTHER_PARTY
    return code


def _label_of(reference: Any) -> Optional[str]:
    """Returns the label of a referenced entity, or None if the reference is missing."""
    return reference.get("label") if isinstance(reference, dict) else None


def _id_of(reference: Any) -> Optional[str]:
    """Returns the id of a referenced entity as a string, or None if the reference is missing."""
    if isinstance(reference, dict) and reference.get("id") is not None:
        return str(reference["id"])
    return None


def _parse_date(value: Any) -> Optional[date]:
    """Parses an ISO date (YYYY-MM-DD); returns None for missing or invalid values."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Politicians
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CurrentMandate:
    """A current mandate of a politician, as read from the mandate list of a parliament.

    Attributes:
        politician_id (str): Id of the politician at abgeordnetenwatch.
        jurisdiction (str): DE or the ISO 3166-2 code of the state.
        mandate (Mandate): The mandate in the form of the registry.
        mandate_won (Optional[str]): How the mandate was won: constituency, list or moved_up.
    """
    politician_id: str
    jurisdiction: str
    mandate: Mandate
    mandate_won: Optional[str]


def parse_mandate(raw: dict[str, Any], parliament: str, jurisdiction: str) -> Optional[CurrentMandate]:
    """Converts one entity of the candidacies-mandates list to a current mandate.

    Args:
        raw: The candidacy_mandate entity.
        parliament: Name of the parliament, e.g. "Bundestag" or "Landtag Bayern".
        jurisdiction: DE or the ISO 3166-2 code of the state.

    Returns:
        Optional[CurrentMandate]: The mandate, or None if the entity has no politician.
    """
    politician_id = _id_of(raw.get("politician"))
    if politician_id is None:
        logger.warning("Skipped abgeordnetenwatch mandate %s: no politician.", raw.get("id"))
        return None
    period = clean_text(_label_of(raw.get("parliament_period")))
    electoral = raw.get("electoral_data") if isinstance(raw.get("electoral_data"), dict) else {}
    memberships = [item for item in raw.get("fraction_membership") or [] if isinstance(item, dict)]
    current = [item for item in memberships if not item.get("valid_until")] or memberships
    fraction = max(current, key=lambda item: item.get("valid_from") or "") if current else None
    mandate = Mandate(
        parliament=parliament,
        period=period,
        faction=party_code(_label_of(fraction.get("fraction"))) if fraction else None,
        constituency=strip_period(_label_of(electoral.get("constituency")), period),
        electoral_list=strip_period(_label_of(electoral.get("electoral_list")), period),
        start_date=raw.get("start_date") or None,
        end_date=raw.get("end_date") or None,
    )
    mandate_won = electoral.get("mandate_won") if isinstance(electoral.get("mandate_won"), str) else None
    return CurrentMandate(politician_id, jurisdiction, mandate, mandate_won)


def _ordered(mandates: list[CurrentMandate]) -> list[CurrentMandate]:
    """Sorts mandates: the Bundestag first, then the state parliaments by name."""
    return sorted(mandates, key=lambda item: (item.jurisdiction != "DE", item.mandate.parliament, item.mandate.period))


def build_politician(raw: dict[str, Any], mandates: list[CurrentMandate]) -> Politician:
    """Builds the registry entry of a politician from the politician entity and the current mandates.

    Args:
        raw: The politician entity.
        mandates: The current mandates of the politician.

    Returns:
        Politician: The registry entry; mandates of the Bundestag come first.
    """
    first_name = clean_text(raw.get("first_name")) or None
    last_name = clean_text(raw.get("last_name")) or None
    name = clean_text(raw.get("label")) or " ".join(part for part in (first_name, last_name) if part)
    year_of_birth = raw.get("year_of_birth")
    qid = raw.get("qid_wikidata")
    return Politician(
        name=name,
        first_name=first_name,
        last_name=last_name,
        party=party_code(_label_of(raw.get("party"))),
        sex=SEX_BY_CODE.get(raw.get("sex") or ""),
        year_of_birth=year_of_birth if isinstance(year_of_birth, int) and year_of_birth > 0 else None,
        wikidata_qid=qid.strip() if isinstance(qid, str) and qid.strip() else None,
        mandates=[item.mandate for item in _ordered(mandates)],
    )


def politician_fields(politician_id: str, politician: Politician, mandates: list[CurrentMandate]) -> dict[str, Any]:
    """Returns the tracked fields of the politician record.

    Args:
        politician_id: Id of the politician at abgeordnetenwatch.
        politician: The registry entry.
        mandates: The current mandates of the politician.

    Returns:
        dict: The record-specific fields; their hash decides whether a new revision is written.
    """
    return {
        "politician_id": politician_id,
        "name": politician.name,
        "party": politician.party,
        "sex": politician.sex,
        "year_of_birth": politician.year_of_birth,
        "wikidata_qid": politician.wikidata_qid,
        "mandates": [{**item.mandate.model_dump(), "mandate_won": item.mandate_won} for item in _ordered(mandates)],
    }


class PoliticiansCollector(Collector):
    """Keeps the registry of politicians with a current mandate and writes new and changed entries.

    Runs once a week: mandates change rarely, and a change is recorded with the next run.
    """
    name = "politicians"
    datasets = (POLITICIANS_DATASET,)
    min_days_between_runs = 6

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the current mandates of the Bundestag and the state parliaments and their politicians.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the collector keeps no cursor.

        Raises:
            ValueError: If the API returns no current mandates of the Bundestag.
        """
        counters: Counter[str] = Counter()
        mandates_by_politician: dict[str, list[CurrentMandate]] = defaultdict(list)
        for parliament in fetch_list(ctx, "parliaments", {}):
            jurisdiction = JURISDICTION_BY_PARLIAMENT_ID.get(parliament.get("id"))
            period_id = _id_of(parliament.get("current_project"))
            if jurisdiction is None or period_id is None:
                continue
            name = clean_text(parliament.get("label_external_long") or parliament.get("label"))
            raw_mandates = fetch_list(ctx, "candidacies-mandates", {"parliament_period": period_id, "type": "mandate"})
            if not raw_mandates:
                if jurisdiction == "DE":
                    raise ValueError(f"abgeordnetenwatch returned no current mandates of the Bundestag (period {period_id})")
                logger.warning("abgeordnetenwatch returned no current mandates of %s (period %s).", name, period_id)
                continue
            counters["parliaments"] += 1
            for raw in raw_mandates:
                mandate = parse_mandate(raw, name, jurisdiction)
                if mandate is not None:
                    mandates_by_politician[mandate.politician_id].append(mandate)
                    counters["mandates"] += 1

        details = self._fetch_politicians(ctx, sorted(mandates_by_politician, key=int))
        seen = ctx.seen(POLITICIANS_DATASET)
        registry: dict[str, Politician] = {}
        records: list[PoliticianRecord] = []
        for politician_id in sorted(mandates_by_politician, key=int):
            raw = details.get(politician_id)
            if raw is None:
                logger.warning("abgeordnetenwatch returned no data for politician %s.", politician_id)
                counters["missing"] += 1
                continue
            mandates = mandates_by_politician[politician_id]
            politician = build_politician(raw, mandates)
            registry[politician_id] = politician
            fields = politician_fields(politician_id, politician, mandates)
            item_hash = content_hash(fields)
            status = seen.classify(politician_id, item_hash)
            if status == "unchanged":
                continue
            counters[status] += 1
            revision = seen.mark(politician_id, item_hash, ctx.today)
            jurisdiction = "DE" if any(item.jurisdiction == "DE" for item in mandates) else mandates[0].jurisdiction
            records.append(PoliticianRecord(
                **ctx.envelope(
                    POLITICIANS_DATASET, f"aw-politician:{politician_id}", revision=revision, source_id=SOURCE_ID,
                    source_url=f"{API_URL}politicians/{politician_id}", license=LICENSE, jurisdiction=jurisdiction,
                    backfilled=False,
                ),
                **fields,
            ))
        ctx.emit(POLITICIANS_DATASET, records)
        previous = load_politicians(ctx.settings.state_dir)
        counters["left"] = len(set(previous) - set(registry))
        counters["registry"] = len(registry)
        if not ctx.dry_run:
            save_politicians(ctx.settings.state_dir, registry)
        logger.info("Politicians: %d in the registry, %d new, %d changed, %d without a current mandate.",
                    len(registry), counters["new"], counters["changed"], counters["left"])
        return CollectorResult(counters=dict(counters))

    @staticmethod
    def _fetch_politicians(ctx: RunContext, politician_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Reads the politician entities, POLITICIAN_IDS_PER_REQUEST ids per request.

        Args:
            ctx: The run context.
            politician_ids: Ids of the politicians.

        Returns:
            dict: Politician entities by id.
        """
        details: dict[str, dict[str, Any]] = {}
        for start in range(0, len(politician_ids), POLITICIAN_IDS_PER_REQUEST):
            chunk = politician_ids[start:start + POLITICIAN_IDS_PER_REQUEST]
            for raw in fetch_list(ctx, "politicians", {"id[in]": "[" + ",".join(chunk) + "]"}):
                politician_id = _id_of(raw)
                if politician_id is not None:
                    details[politician_id] = raw
        return details


# ---------------------------------------------------------------------------
# Roll-call votes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemberVote:
    """The vote of one member in a roll-call vote.

    Attributes:
        vote_url (Optional[str]): API URL of the vote entity.
        politician_id (Optional[str]): Id of the politician; None if the mandate is unknown.
        faction (str): Parliamentary group at the time of the vote as party code, NONE without group.
        choice (str): yes, no, abstain or no_show.
    """
    vote_url: Optional[str]
    politician_id: Optional[str]
    faction: str
    choice: str


def parse_votes(raw_votes: list[dict[str, Any]], politician_by_mandate: dict[str, str]) -> list[MemberVote]:
    """Converts the vote entities of a roll-call vote.

    Args:
        raw_votes: Entities of the votes list.
        politician_by_mandate: Politician id by mandate id.

    Returns:
        list[MemberVote]: One entry per member.
    """
    members = []
    for raw in raw_votes:
        choice = raw.get("vote")
        if choice not in VOTE_CHOICES:
            logger.warning("Unknown vote value %r in abgeordnetenwatch vote %s; stored as no_show.", choice, raw.get("id"))
            choice = "no_show"
        mandate_id = _id_of(raw.get("mandate"))
        members.append(MemberVote(
            vote_url=raw.get("api_url") if isinstance(raw.get("api_url"), str) else None,
            politician_id=politician_by_mandate.get(mandate_id) if mandate_id else None,
            faction=party_code(_label_of(raw.get("fraction"))) or NO_FACTION,
            choice=choice,
        ))
    return members


def group_majorities(members: list[MemberVote]) -> dict[str, Optional[str]]:
    """Returns the yes/no majority of each parliamentary group.

    Args:
        members: The votes of the members.

    Returns:
        dict: yes or no by group, None if the group cast as many yes as no
        votes. Members without a group (NONE) have no majority.
    """
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for member in members:
        counts[member.faction][member.choice] += 1
    majorities: dict[str, Optional[str]] = {}
    for faction, count in counts.items():
        if faction == NO_FACTION or count["yes"] == count["no"]:
            majorities[faction] = None
        else:
            majorities[faction] = "yes" if count["yes"] > count["no"] else "no"
    return majorities


def voted_against_group(member: MemberVote, majorities: dict[str, Optional[str]]) -> bool:
    """Tells whether a member voted yes or no against the yes/no majority of the group.

    Args:
        member: The vote of the member.
        majorities: Result of ``group_majorities``.

    Returns:
        bool: True for a yes vote in a group with a no majority and vice versa.
    """
    majority = majorities.get(member.faction)
    return member.choice in ("yes", "no") and majority is not None and member.choice != majority


def tally_votes(members: list[MemberVote]) -> dict[str, Any]:
    """Computes the counts, group counts, cohesion and defectors of a roll-call vote.

    Args:
        members: The votes of the members.

    Returns:
        dict: The fields yes, no, abstain, no_show, by_faction, cohesion and
        defectors of the roll-call vote record. Cohesion is the Rice index
        |yes - no| / (yes + no); groups without yes or no votes get None.
        Members without a group (NONE) are counted in by_faction only.
    """
    totals: Counter[str] = Counter(member.choice for member in members)
    by_faction: dict[str, dict[str, int]] = {}
    for member in members:
        by_faction.setdefault(member.faction, dict.fromkeys(VOTE_CHOICES, 0))[member.choice] += 1
    majorities = group_majorities(members)
    defectors: Counter[str] = Counter(member.faction for member in members if voted_against_group(member, majorities))
    cohesion: dict[str, Optional[float]] = {}
    for faction, count in by_faction.items():
        if faction == NO_FACTION:
            continue
        cast = count["yes"] + count["no"]
        cohesion[faction] = round(abs(count["yes"] - count["no"]) / cast, 4) if cast else None
    groups = sorted(cohesion)
    return {
        "yes": totals["yes"], "no": totals["no"], "abstain": totals["abstain"], "no_show": totals["no_show"],
        "by_faction": dict(sorted(by_faction.items())),
        "cohesion": {faction: cohesion[faction] for faction in groups},
        "defectors": {faction: defectors[faction] for faction in groups},
    }


def printed_papers(intro: Any) -> list[str]:
    """Returns the numbers of the Bundestag printed papers linked in the description of a roll-call vote.

    Args:
        intro: The HTML description (field_intro).

    Returns:
        list[str]: Numbers such as "21/7869", in the order of their first link.
    """
    numbers: list[str] = []
    for period, link_period, number in _PRINTED_PAPER_LINK.findall(intro if isinstance(intro, str) else ""):
        if period != link_period:
            continue
        paper = f"{int(period)}/{int(number)}"
        if paper not in numbers:
            numbers.append(paper)
    return numbers


def poll_fields(poll: dict[str, Any], members: list[MemberVote]) -> Optional[dict[str, Any]]:
    """Converts a poll entity and the votes of its members to the fields of a roll-call vote record.

    Args:
        poll: The poll entity.
        members: The votes of the members.

    Returns:
        Optional[dict]: The record-specific fields, or None if the poll has no valid date.
    """
    vote_date = _parse_date(poll.get("field_poll_date"))
    if vote_date is None:
        logger.warning("Skipped abgeordnetenwatch poll %s: no valid date.", poll.get("id"))
        return None
    accepted = poll.get("field_accepted")
    return {
        "vote_id": str(poll["id"]),
        "parliament_period": clean_text(_label_of(poll.get("field_legislature"))),
        "vote_date": vote_date,
        "title": clean_text(poll.get("label")),
        "topics": [clean_text(_label_of(topic)) for topic in poll.get("field_topics") or [] if _label_of(topic)],
        "committees": [clean_text(_label_of(item)) for item in poll.get("field_committees") or [] if _label_of(item)],
        "printed_papers": printed_papers(poll.get("field_intro")),
        "accepted": accepted if isinstance(accepted, bool) else None,
        **tally_votes(members),
    }


def select_periods(legislatures: list[dict[str, Any]], today: date) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Returns the current and the previous electoral period of the Bundestag.

    Args:
        legislatures: Entities of the parliament-periods list (type legislature).
        today: Date of the run.

    Returns:
        tuple: The period that started most recently on or before today, and the one before it (or None).

    Raises:
        ValueError: If no period has started.
    """
    started = sorted(
        (period for period in legislatures
         if (start := _parse_date(period.get("start_date_period"))) is not None and start <= today),
        key=lambda period: period["start_date_period"], reverse=True,
    )
    if not started:
        raise ValueError("abgeordnetenwatch lists no started electoral period of the Bundestag")
    return started[0], started[1] if len(started) > 1 else None


class RollCallVotesCollector(Collector):
    """Writes the roll-call votes of the Bundestag and the votes of the single members."""
    name = "roll_call_votes"
    datasets = (VOTES_DATASET, MP_VOTES_DATASET)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the polls of the current period (and of periods still to import) and emits the new ones.

        The cursor holds the current period and the periods whose polls are
        still to be imported: on the first run the previous period, after a
        change of period the period that ended.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: The new cursor and counters.
        """
        legislatures = fetch_list(ctx, "parliament-periods", {"parliament": BUNDESTAG_ID, "type": "legislature"})
        current, previous = select_periods(legislatures, ctx.today)
        current_id = str(current["id"])
        backfill = [str(period) for period in entry.cursor.get("backfill_periods", [])]
        known_period = entry.cursor.get("period")
        if known_period is None:
            if previous is not None:
                backfill.append(str(previous["id"]))
        elif str(known_period) != current_id:
            backfill.append(str(known_period))
        period_ids = [current_id] + [period for period in dict.fromkeys(backfill) if period != current_id]

        seen = ctx.seen(VOTES_DATASET)
        counters: Counter[str] = Counter()
        polls_by_period: dict[str, list[dict[str, Any]]] = {}
        queue: list[tuple[str, dict[str, Any]]] = []
        for period_id in period_ids:
            polls = [poll for poll in fetch_list(ctx, "polls", {"field_legislature": period_id})
                     if poll.get("id") is not None]
            polls_by_period[period_id] = polls
            new = sorted((poll for poll in polls if str(poll["id"]) not in seen),
                         key=lambda poll: (str(poll.get("field_poll_date") or ""), int(poll["id"])), reverse=True)
            budget = len(new) if ctx.backfill else max(MAX_NEW_POLLS_PER_RUN - len(queue), 0)
            queue.extend((period_id, poll) for poll in new[:budget])
            counters["deferred"] += len(new) - min(budget, len(new))

        politicians_by_period: dict[str, dict[str, str]] = {}
        batch: list[tuple[dict[str, Any], list[MemberVote], dict[str, Any]]] = []
        complete = True
        for position, (period_id, poll) in enumerate(queue):
            if ctx.past_deadline():
                counters["deferred"] += len(queue) - position
                complete = False
                break
            if period_id not in politicians_by_period:
                politicians_by_period[period_id] = self._politicians_by_mandate(ctx, period_id)
            raw_votes = fetch_list(ctx, "votes", {"poll": poll["id"]})
            if not raw_votes:
                logger.info("abgeordnetenwatch poll %s has no votes yet; retried in a later run.", poll["id"])
                counters["without_votes"] += 1
                continue
            members = parse_votes(raw_votes, politicians_by_period[period_id])
            fields = poll_fields(poll, members)
            if fields is None:
                counters["invalid"] += 1
                continue
            batch.append((poll, members, fields))
            if len(batch) >= POLLS_PER_WRITE:
                self._write(ctx, entry, batch, counters)
                batch = []
        self._write(ctx, entry, batch, counters)

        remaining = [period for period in period_ids[1:]
                     if any(str(poll["id"]) not in seen for poll in polls_by_period[period])]
        logger.info("Roll-call votes: %d new, %d member votes, %d deferred to later runs.",
                    counters["new"], counters["member_votes"], counters["deferred"])
        return CollectorResult(cursor={"period": current_id, "backfill_periods": remaining},
                               counters=dict(counters), complete=complete)

    @staticmethod
    def _politicians_by_mandate(ctx: RunContext, period_id: str) -> dict[str, str]:
        """Returns the politician id of every mandate of a period, including mandates that ended early.

        Args:
            ctx: The run context.
            period_id: Id of the electoral period.

        Returns:
            dict: Politician id by mandate id.
        """
        mapping: dict[str, str] = {}
        params = {"parliament_period": period_id, "type": "mandate", "current_on": "all"}
        for raw in fetch_list(ctx, "candidacies-mandates", params):
            mandate_id, politician_id = _id_of(raw), _id_of(raw.get("politician"))
            if mandate_id is not None and politician_id is not None:
                mapping[mandate_id] = politician_id
        return mapping

    @staticmethod
    def _write(
        ctx: RunContext,
        entry: CollectorEntry,
        batch: list[tuple[dict[str, Any], list[MemberVote], dict[str, Any]]],
        counters: Counter[str],
    ) -> None:
        """Emits the member votes and then the roll-call votes of a batch of polls.

        The polls are marked as seen only after their member votes are written,
        so that an interrupted run reads them again.

        Args:
            ctx: The run context.
            entry: Run history of the collector.
            batch: Poll entity, member votes and record fields per poll.
            counters: Counters of the run, updated in place.
        """
        if not batch:
            return
        mp_records: list[MpVoteRecord] = []
        for poll, members, fields in batch:
            backfilled = is_backfilled(fields["vote_date"], entry.first_run)
            majorities = group_majorities(members)
            for member in members:
                if member.politician_id is None:
                    counters["member_votes_without_politician"] += 1
                    continue
                mp_records.append(MpVoteRecord(
                    **ctx.envelope(
                        MP_VOTES_DATASET, f"aw-vote:{fields['vote_id']}:{member.politician_id}", revision=0,
                        source_id=SOURCE_ID, source_url=member.vote_url, license=LICENSE, jurisdiction="DE",
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
        for poll, _members, fields in batch:
            revision = seen.mark(fields["vote_id"], content_hash(fields), ctx.today)
            vote_records.append(RollCallVoteRecord(
                **ctx.envelope(
                    VOTES_DATASET, f"aw-poll:{fields['vote_id']}", revision=revision, source_id=SOURCE_ID,
                    source_url=poll.get("api_url") or f"{API_URL}polls/{fields['vote_id']}", license=LICENSE,
                    jurisdiction="DE", backfilled=is_backfilled(fields["vote_date"], entry.first_run),
                ),
                **fields,
            ))
        ctx.emit(VOTES_DATASET, vote_records)
        counters["new"] += len(vote_records)
