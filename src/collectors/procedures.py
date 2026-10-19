"""
Collects parliamentary procedures (Vorgänge) from the DIP API of the Bundestag.

The DIP (Dokumentations- und Informationssystem für Parlamentsmaterialien)
lists every procedure of the Bundestag and the Bundesrat with its type,
initiators, subject areas and status. The API requires a key (header
``Authorization: ApiKey <key>``) and returns 100 procedures per request with
cursor pagination.

The collector reads the procedures of the current electoral period that were
updated since the previous run. The first run reads the whole period. A
procedure is written when it appears, when its status changes and when
another tracked field changes; the status at the previous observation is
kept in the seen store. Records are written in chunks, so that an
interrupted run keeps its progress, and the cursor is advanced only at the
end of a successful run. A run that stops at the run deadline keeps the
previous cursor.
"""
import html
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, CollectorSkipped, RunContext
from schema import ProcedureRecord
from state import CollectorEntry, content_hash

logger = logging.getLogger(__name__)

DATASET = "parliamentary_procedures"
SOURCE_ID = "dip"
LICENSE = "DIP-Nutzungsbedingungen"
API_URL = "https://search.dip.bundestag.de/api/v1"
PROCEDURES_URL = f"{API_URL}/vorgang"
# Electoral period of the Bundestag whose procedures are read; the 22nd period starts in 2029
ELECTORAL_PERIOD = 21
# Records after which the collected records are written
RECORDS_PER_WRITE = 1000
# Fields whose change produces a new record
TRACKED_FIELDS: tuple[str, ...] = ("title", "procedure_type", "initiators", "subject_areas", "consent_required",
                                   "status", "gesta_id")
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_text(value: Any) -> str:
    """Returns a value as plain text: no HTML tags, entities, line breaks or extra spaces."""
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(_HTML_TAG.sub(" ", value)).split())


def _clean_list(values: Any) -> list[str]:
    """Returns the non-empty strings of a list as plain text."""
    if not isinstance(values, list):
        return []
    return [text for text in (_clean_text(value) for value in values) if text]


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parses an ISO timestamp with offset and converts it to UTC; returns None for invalid values."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _parse_date(value: Any) -> Optional[date]:
    """Parses an ISO date (YYYY-MM-DD); returns None for missing or invalid values."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def consent_required(statements: Any) -> Optional[bool]:
    """Reads whether the consent of the Bundesrat is required.

    The DIP lists one statement per stage, e.g. "Nein, laut Gesetzentwurf (Drs
    21/218)" followed by "Ja, laut Verkündung (BGBl I)". The last statement
    that starts with Ja or Nein applies.

    Args:
        statements: The ``zustimmungsbeduerftigkeit`` list of a procedure.

    Returns:
        Optional[bool]: True for "Ja, ...", False for "Nein, ...", None if no statement says either.
    """
    for statement in reversed(_clean_list(statements)):
        answer = statement.split(",", 1)[0].strip().casefold()
        if answer == "ja":
            return True
        if answer == "nein":
            return False
    return None


def parse_procedure(document: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Converts one Vorgang document of the DIP to the fields of a procedure record.

    Args:
        document: The document from the ``vorgang`` list.

    Returns:
        Optional[dict]: The record fields without change and previous_status,
        or None if the document has no id, title or type.
    """
    procedure_id = str(document.get("id") or "").strip()
    title = _clean_text(document.get("titel"))
    procedure_type = _clean_text(document.get("vorgangstyp"))
    if not procedure_id or not title or not procedure_type:
        logger.warning("Skipped DIP document %r: missing id, title or type.", document.get("id"))
        return None
    period = document.get("wahlperiode")
    descriptors = [item.get("name") for item in document.get("deskriptor") or [] if isinstance(item, dict)]
    return {
        "procedure_id": procedure_id,
        "procedure_type": procedure_type,
        "electoral_period": period if isinstance(period, int) else None,
        "title": title,
        "initiators": _clean_list(document.get("initiative")),
        "subject_areas": _clean_list(document.get("sachgebiet")),
        "descriptors": _clean_list(descriptors),
        "consent_required": consent_required(document.get("zustimmungsbeduerftigkeit")),
        "status": _clean_text(document.get("beratungsstand")) or None,
        "gesta_id": _clean_text(document.get("gesta")) or None,
        "dip_updated_at": _parse_datetime(document.get("aktualisiert")),
        "latest_document_date": _parse_date(document.get("datum")),
    }


def tracked_hash(fields: dict[str, Any]) -> str:
    """Returns the hash of the tracked fields of a procedure; list order does not count.

    Args:
        fields: Result of ``parse_procedure``.

    Returns:
        str: The content hash.
    """
    tracked = {name: sorted(fields[name]) if isinstance(fields[name], list) else fields[name]
               for name in TRACKED_FIELDS}
    return content_hash(tracked)


class ProceduresCollector(Collector):
    """Writes new and changed procedures of the current electoral period from the DIP."""
    name = "parliamentary_procedures"
    datasets = (DATASET,)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the procedures updated since the previous run and emits new and changed ones.

        Args:
            ctx: The run context.
            entry: Run history of this collector; ``cursor["aktualisiert"]``
                is the latest update time read by the previous run.

        Returns:
            CollectorResult: The new cursor and counters.

        Raises:
            CollectorSkipped: If no DIP API key is set.
            ValueError: If a response is not a DIP list response.
        """
        api_key = ctx.settings.dip_api_key
        if not api_key:
            raise CollectorSkipped("DIP_API_KEY is not set")
        since = entry.cursor.get("aktualisiert")
        params: dict[str, Any] = {"f.wahlperiode": ELECTORAL_PERIOD, "format": "json"}
        if since:
            params["f.aktualisiert.start"] = since
        headers = {"Authorization": f"ApiKey {api_key}"}

        seen = ctx.seen(DATASET)
        counters = {"pages": 0, "documents": 0, "new": 0, "status_changed": 0, "updated": 0, "invalid": 0}
        pending: dict[str, tuple[dict[str, Any], str]] = {}
        latest: Optional[tuple[datetime, str]] = None
        page_cursor: Optional[str] = None
        while True:
            if ctx.past_deadline():
                # The cursor stays at the previous run: the next run reads the remaining pages again,
                # and the procedures written here are unchanged then.
                self._write(ctx, pending, counters)
                logger.warning("Procedures: stopped at the run deadline after %d documents.", counters["documents"])
                return CollectorResult(counters=counters, complete=False)
            page_params = {**params, "cursor": page_cursor} if page_cursor else params
            body = ctx.web.get(PROCEDURES_URL, params=page_params, headers=headers, respect_robots=False).json()
            documents = body.get("documents") if isinstance(body, dict) else None
            if not isinstance(documents, list):
                raise ValueError("DIP response has no documents list")
            counters["pages"] += 1
            for document in documents:
                if not isinstance(document, dict):
                    continue
                counters["documents"] += 1
                fields = parse_procedure(document)
                if fields is None:
                    counters["invalid"] += 1
                    continue
                updated = fields["dip_updated_at"]
                if updated is not None and (latest is None or updated > latest[0]):
                    latest = (updated, str(document["aktualisiert"]))
                procedure_id = fields["procedure_id"]
                item_hash = tracked_hash(fields)
                if seen.classify(procedure_id, item_hash) == "unchanged":
                    pending.pop(procedure_id, None)
                    continue
                pending[procedure_id] = (fields, item_hash)
                if len(pending) >= RECORDS_PER_WRITE:
                    self._write(ctx, pending, counters)
            next_cursor = body.get("cursor")
            if not documents or not next_cursor or next_cursor == page_cursor:
                break
            page_cursor = str(next_cursor)
        self._write(ctx, pending, counters)

        logger.info("Procedures: %d documents read, %d new, %d with a new status, %d updated.",
                    counters["documents"], counters["new"], counters["status_changed"], counters["updated"])
        if latest is None:
            return CollectorResult(counters=counters)
        # Some DIP documents carry an update time in the future; the cursor never passes the start of the run,
        # so that documents updated after the start are read again by the next run.
        cursor = latest[1] if latest[0] <= ctx.started else ctx.started.isoformat()
        return CollectorResult(cursor={"aktualisiert": cursor}, counters=counters)

    @staticmethod
    def _write(ctx: RunContext, pending: dict[str, tuple[dict[str, Any], str]], counters: dict[str, int]) -> None:
        """Marks the pending procedures as seen and emits their records.

        Marking happens here and not when a document is read, so that the seen
        store never contains procedures whose records were not written.

        Args:
            ctx: The run context.
            pending: Fields and hash by procedure id; emptied by this method.
            counters: Counters of the run, updated in place.
        """
        if not pending:
            return
        seen = ctx.seen(DATASET)
        records: list[ProcedureRecord] = []
        for procedure_id, (fields, item_hash) in pending.items():
            previous = seen.get(procedure_id)
            previous_status = previous.data.get("status") if previous is not None else None
            if previous is None:
                change = "new"
            elif previous_status != fields["status"]:
                change = "status_changed"
            else:
                change = "updated"
            counters[change] += 1
            revision = seen.mark(procedure_id, item_hash, ctx.today, data={"status": fields["status"]})
            records.append(ProcedureRecord(
                **ctx.envelope(
                    DATASET, f"dip-vorgang:{procedure_id}", revision=revision, source_id=SOURCE_ID,
                    source_url=f"{PROCEDURES_URL}/{procedure_id}", license=LICENSE, jurisdiction="DE",
                    backfilled=False,
                ),
                change=change,
                previous_status=previous_status,
                **fields,
            ))
        pending.clear()
        ctx.emit(DATASET, records)
