"""Tests of the DIP procedures collector."""
import json
from datetime import datetime, timezone

import httpx
import pytest

import collectors.procedures as procedures
from collectors.base import CollectorSkipped
from collectors.procedures import PROCEDURES_URL, ProceduresCollector, consent_required, parse_procedure
from conftest import FIXTURES_DIR, FakeWeb, read_dataset
from state import CollectorEntry, SeenStore

API_KEY = "test-key"
FIRST_CURSOR = "AoJwkO-JoqEDLlZvcmdhbmctMzM4OTUx"
SECOND_CURSOR = "AoJw0KPAn6EDLlZvcmdhbmctMzI1NTM1"


def dip_url(since: str | None = None, cursor: str | None = None) -> str:
    """Returns the URL of a page of the procedure list, as requested by the collector."""
    params: dict[str, object] = {"f.wahlperiode": 21, "format": "json"}
    if since:
        params["f.aktualisiert.start"] = since
    if cursor:
        params["cursor"] = cursor
    return str(httpx.URL(PROCEDURES_URL, params=params))


def _page(name: str) -> dict:
    return json.loads((FIXTURES_DIR / "dip" / name).read_text(encoding="utf-8"))


def _document(procedure_id: str) -> dict:
    for name in ("vorgang_page1.json", "vorgang_page2.json"):
        for document in _page(name)["documents"]:
            if document["id"] == procedure_id:
                return document
    raise KeyError(procedure_id)


def _serve_first_run(fake_web: FakeWeb, page2: dict | None = None) -> None:
    fake_web.add_fixture(dip_url(), "dip/vorgang_page1.json")
    if page2 is None:
        fake_web.add_fixture(dip_url(cursor=FIRST_CURSOR), "dip/vorgang_page2.json")
    else:
        fake_web.add(dip_url(cursor=FIRST_CURSOR), page2)
    fake_web.add_fixture(dip_url(cursor=SECOND_CURSOR), "dip/vorgang_page3.json")


def test_consent_required_uses_the_latest_statement() -> None:
    assert consent_required(["Nein, laut Gesetzentwurf (Drs 21/218)", "Ja, laut Bundestag (Drs 21/6698)",
                             "Ja, laut Verkündung (BGBl I)"]) is True
    assert consent_required(["Ohne Angabe, laut Gesetzesantrag (203/25)", "Nein, laut Bundesrat (Drs 203/25(B))"]) is False
    assert consent_required(["Ohne Angabe, laut Gesetzesantrag (203/25)"]) is None
    assert consent_required(None) is None


def test_parse_procedure_maps_fields_and_cleans_the_title() -> None:
    bill = parse_procedure(_document("321949"))
    assert bill is not None
    assert bill["procedure_type"] == "Gesetzgebung"
    assert bill["status"] == "Verkündet"
    assert bill["consent_required"] is True
    assert bill["electoral_period"] == 21
    assert bill["gesta_id"] == "E003"
    assert bill["initiators"] == ["Fraktion der CDU/CSU", "Fraktion der SPD"]
    assert bill["subject_areas"] == ["Wirtschaft", "Wissenschaft, Forschung und Technologie"]
    assert bill["dip_updated_at"] == datetime(2026, 10, 9, 13, 46, 49, tzinfo=timezone.utc)

    question = parse_procedure(_document("339895"))
    assert question is not None
    assert question["procedure_type"] == "Kleine Anfrage"
    assert question["initiators"] == ["Fraktion der AfD"]
    assert question["gesta_id"] is None
    assert question["consent_required"] is None

    eu_document = _document("340611")
    assert "\r\n" in eu_document["titel"]
    eu = parse_procedure(eu_document)
    assert eu is not None
    assert "\r" not in eu["title"] and "  " not in eu["title"]
    assert eu["status"] is None

    assert parse_procedure({"id": "1", "titel": "<b>Titel</b> &amp; Text", "vorgangstyp": "Antrag"})["title"] == \
        "Titel & Text"
    assert parse_procedure({"id": "1", "vorgangstyp": "Antrag"}) is None


def test_collect_is_skipped_without_api_key(fake_web, make_ctx) -> None:
    ctx = make_ctx()

    with pytest.raises(CollectorSkipped, match="DIP_API_KEY"):
        ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert fake_web.requested == []


def test_first_run_reads_all_pages_with_the_api_key(fake_web, make_ctx, monkeypatch) -> None:
    authorization: list[str] = []
    handler = fake_web.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("authorization", ""))
        return handler(request)

    monkeypatch.setattr(fake_web, "handler", recording_handler)
    _serve_first_run(fake_web)
    ctx = make_ctx(dip_api_key=API_KEY)

    result = ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert authorization == [f"ApiKey {API_KEY}"] * 3
    assert result.counters["pages"] == 3
    records = read_dataset(ctx.settings.output_dir, "parliamentary_procedures")
    assert len(records) == 5
    assert {r["change"] for r in records} == {"new"}
    assert all(r["previous_status"] is None and r["revision"] == 0 for r in records)
    by_id = {r["procedure_id"]: r for r in records}
    assert by_id["321949"]["record_id"] == "dip-vorgang:321949"
    assert by_id["321949"]["license"] == "DIP-Nutzungsbedingungen"
    assert by_id["321949"]["source_url"] == f"{PROCEDURES_URL}/321949"
    assert by_id["321949"]["backfilled"] is False
    assert by_id["321949"]["latest_document_date"] == "2026-09-25"
    seen = SeenStore.load(ctx.settings.state_dir, "parliamentary_procedures")
    assert seen.get("321636").data == {"status": "Dem Bundestag zugeleitet - Noch nicht beraten"}
    # Document 340087 is dated 2026-10-26 by the DIP, after the start of the run; the cursor stops at the start
    assert result.cursor == {"aktualisiert": "2026-10-20T00:07:00+00:00"}


def test_cursor_is_the_latest_update_time_read(fake_web, make_ctx) -> None:
    page2 = _page("vorgang_page2.json")
    page2["documents"] = [d for d in page2["documents"] if d["id"] != "340087"]
    _serve_first_run(fake_web, page2=page2)
    ctx = make_ctx(dip_api_key=API_KEY)

    result = ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.cursor == {"aktualisiert": "2026-10-19T13:42:34+02:00"}


def test_records_are_written_in_chunks(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(procedures, "RECORDS_PER_WRITE", 2)
    _serve_first_run(fake_web)
    ctx = make_ctx(dip_api_key=API_KEY)

    result = ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["new"] == 5
    assert len(read_dataset(ctx.settings.output_dir, "parliamentary_procedures")) == 5


def test_second_run_writes_status_changes_and_updates_only(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx(dip_api_key=API_KEY)
    first = ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    since = first.cursor["aktualisiert"]

    unchanged = _document("339895")
    status_change = dict(_document("321636"), beratungsstand="Überwiesen", aktualisiert="2026-10-21T09:00:00+02:00")
    title_change = dict(_document("340611"), titel="Neuer Titel", aktualisiert="2026-10-21T09:30:00+02:00")
    descriptor_change = dict(_document("321949"), deskriptor=[{"fundstelle": True, "name": "Neu", "typ": "Sachbegriffe"}])
    page = {"numFound": 4, "cursor": "c1", "documents": [unchanged, status_change, title_change, descriptor_change]}
    fake_web.add(dip_url(since=since), page)
    fake_web.add(dip_url(since=since, cursor="c1"), {"numFound": 4, "cursor": "c1", "documents": []})

    entry = CollectorEntry(first_run=ctx.today, cursor=first.cursor)
    result = ProceduresCollector().collect(make_ctx(dip_api_key=API_KEY), entry)

    assert (result.counters["new"], result.counters["status_changed"], result.counters["updated"]) == (0, 1, 1)
    later = [r for r in read_dataset(ctx.settings.output_dir, "parliamentary_procedures") if r["revision"] == 1]
    by_id = {r["procedure_id"]: r for r in later}
    assert set(by_id) == {"321636", "340611"}
    assert by_id["321636"]["change"] == "status_changed"
    assert by_id["321636"]["status"] == "Überwiesen"
    assert by_id["321636"]["previous_status"] == "Dem Bundestag zugeleitet - Noch nicht beraten"
    assert by_id["340611"]["change"] == "updated"
    assert by_id["340611"]["title"] == "Neuer Titel"
    assert result.cursor == {"aktualisiert": "2026-10-20T00:07:00+00:00"}
    seen = SeenStore.load(ctx.settings.state_dir, "parliamentary_procedures")
    assert seen.get("321636").data == {"status": "Überwiesen"}


def test_cursor_is_kept_when_nothing_was_updated(fake_web, make_ctx) -> None:
    since = "2026-10-19T13:42:34+02:00"
    fake_web.add(dip_url(since=since), {"numFound": 0, "cursor": "c0", "documents": []})
    ctx = make_ctx(dip_api_key=API_KEY)

    result = ProceduresCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor={"aktualisiert": since}))

    assert result.cursor is None
    assert result.counters["documents"] == 0
