"""
Collects opinion polls (Sonntagsfrage) from the DAWUM API.

DAWUM publishes its whole database as one JSON file under the ODbL. The
collector downloads it only when ``last_update.txt`` has changed since the
previous run, writes new polls and writes a new revision of a poll whose
content DAWUM corrected. The first run imports all polls since 2017; polls
older than a week before that run are marked as backfilled.
"""
import logging
from datetime import date
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from entities import PARTY_CODE_BY_DAWUM_ID
from schema import PollRecord
from state import CollectorEntry, content_hash, is_backfilled

logger = logging.getLogger(__name__)

DATASET = "polls"
SOURCE_ID = "dawum"
DATABASE_URL = "https://api.dawum.de/"
LAST_UPDATE_URL = "https://api.dawum.de/last_update.txt"
LICENSE = "ODbL-1.0"

# DAWUM parliament id -> (parliament, jurisdiction)
PARLIAMENT_BY_DAWUM_ID: dict[str, tuple[str, str]] = {
    "0": ("Bundestag", "DE"),
    "1": ("Landtag", "DE-BW"),
    "2": ("Landtag", "DE-BY"),
    "3": ("Landtag", "DE-BE"),
    "4": ("Landtag", "DE-BB"),
    "5": ("Landtag", "DE-HB"),
    "6": ("Landtag", "DE-HH"),
    "7": ("Landtag", "DE-HE"),
    "8": ("Landtag", "DE-MV"),
    "9": ("Landtag", "DE-NI"),
    "10": ("Landtag", "DE-NW"),
    "11": ("Landtag", "DE-RP"),
    "12": ("Landtag", "DE-SL"),
    "13": ("Landtag", "DE-SN"),
    "14": ("Landtag", "DE-ST"),
    "15": ("Landtag", "DE-SH"),
    "16": ("Landtag", "DE-TH"),
    "17": ("European Parliament", "DE"),
}
METHOD_BY_DAWUM_ID: dict[str, str] = {
    "0": "Unknown",
    "1": "Telephone",
    "2": "Face to face",
    "3": "Online",
    "4": "Telephone and online",
    "5": "Face to face and online",
}
OTHERS_DAWUM_ID = "0"


def _parse_date(value: Any) -> Optional[date]:
    """Parses an ISO date (YYYY-MM-DD); returns None for missing or invalid values."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _name(table: dict[str, Any], key: Any) -> Optional[str]:
    """Returns the Name of an entry of a DAWUM lookup table, or None if it is missing."""
    entry = table.get(str(key)) if key is not None else None
    return entry.get("Name") if isinstance(entry, dict) else None


def parse_survey(survey_id: str, survey: dict[str, Any], data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Converts one DAWUM survey to the poll fields of a record.

    Args:
        survey_id: Id of the survey at DAWUM.
        survey: The survey object.
        data: The whole DAWUM database, for the lookup tables.

    Returns:
        Optional[dict]: The poll-specific fields and the jurisdiction, or None
        if the survey has no valid parliament, date or results.
    """
    parliament = PARLIAMENT_BY_DAWUM_ID.get(str(survey.get("Parliament_ID")))
    published = _parse_date(survey.get("Date"))
    raw_results = survey.get("Results")
    if parliament is None or published is None or not isinstance(raw_results, dict) or not raw_results:
        logger.warning("Skipped DAWUM survey %s: missing parliament, date or results.", survey_id)
        return None
    results: dict[str, float] = {}
    others_share: Optional[float] = None
    for party_id, share in raw_results.items():
        if not isinstance(share, (int, float)):
            continue
        if str(party_id) == OTHERS_DAWUM_ID:
            others_share = float(share)
            continue
        code = PARTY_CODE_BY_DAWUM_ID.get(str(party_id))
        if code is None:
            shortcut = (data.get("Parties", {}).get(str(party_id)) or {}).get("Shortcut", party_id)
            logger.warning("Unknown DAWUM party %s (%s) in survey %s; stored as DAWUM_%s.",
                           party_id, shortcut, survey_id, party_id)
            code = f"DAWUM_{party_id}"
        results[code] = float(share)
    period = survey.get("Survey_Period") if isinstance(survey.get("Survey_Period"), dict) else {}
    persons = str(survey.get("Surveyed_Persons") or "").strip()
    return {
        "jurisdiction": parliament[1],
        "poll_id": str(survey_id),
        "parliament": parliament[0],
        "institute": _name(data.get("Institutes", {}), survey.get("Institute_ID")) or "Unknown",
        "tasker": _name(data.get("Taskers", {}), survey.get("Tasker_ID")),
        "method": METHOD_BY_DAWUM_ID.get(str(survey.get("Method_ID")), "Unknown"),
        "fieldwork_start": _parse_date(period.get("Date_Start")),
        "fieldwork_end": _parse_date(period.get("Date_End")),
        "published_date": published,
        "sample_size": int(persons) if persons.isdigit() else None,
        "results": results,
        "others_share": others_share,
    }


class PollsCollector(Collector):
    """Writes new and corrected polls from DAWUM."""
    name = "polls"
    datasets = (DATASET,)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads the DAWUM database if it changed and emits new and changed polls.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: The DAWUM update time as cursor, and counters.
        """
        last_update = ctx.web.get(LAST_UPDATE_URL, respect_robots=False).text.strip()
        if last_update and entry.cursor.get("last_update") == last_update:
            logger.info("DAWUM database unchanged since %s.", last_update)
            return CollectorResult(counters={"database_changed": False})
        data = ctx.web.get(DATABASE_URL, respect_robots=False).json()
        surveys = data.get("Surveys")
        if not isinstance(surveys, dict):
            raise ValueError("DAWUM response has no Surveys object")
        seen = ctx.seen(DATASET)
        records: list[PollRecord] = []
        counters = {"database_changed": True, "surveys": len(surveys), "new": 0, "changed": 0, "invalid": 0}
        for survey_id, survey in sorted(surveys.items(), key=lambda item: int(item[0]) if item[0].isdigit() else 0):
            fields = parse_survey(survey_id, survey, data) if isinstance(survey, dict) else None
            if fields is None:
                counters["invalid"] += 1
                continue
            item_hash = content_hash(fields)
            status = seen.classify(survey_id, item_hash)
            if status == "unchanged":
                continue
            counters[status] += 1
            revision = seen.mark(survey_id, item_hash, ctx.today)
            jurisdiction = fields.pop("jurisdiction")
            records.append(PollRecord(
                **ctx.envelope(
                    DATASET, f"dawum:{survey_id}", revision=revision, source_id=SOURCE_ID,
                    source_url=DATABASE_URL, license=LICENSE, jurisdiction=jurisdiction,
                    backfilled=is_backfilled(fields["published_date"], entry.first_run),
                ),
                **fields,
            ))
        ctx.emit(DATASET, records)
        logger.info("Polls: %d new, %d changed of %d surveys.", counters["new"], counters["changed"], len(surveys))
        return CollectorResult(cursor={"last_update": last_update}, counters=counters)
