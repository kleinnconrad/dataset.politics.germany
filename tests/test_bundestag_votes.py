"""Tests of the Bundestag roll-call collector with list and XLSX fixtures of www.bundestag.de."""
import io
from datetime import date
from typing import Any

import openpyxl
import pytest

from collectors.bundestag_votes import (
    BASE_URL,
    LICENSE,
    LIST_PAGE_URL,
    BundestagVotesCollector,
    ListedVote,
    MemberMatcher,
    VoteFile,
    fold_name,
    load_aliases,
    member_votes,
    parse_list_page,
    parse_vote_file,
    parse_vote_list,
)
from collectors.feeds import TdmReservedError
from collectors.parliament import tally_votes
from conftest import FIXTURES_DIR, read_dataset
from entities import Mandate, Politician, save_politicians
from state import CollectorEntry
from web import BlockedError

FIXTURES = FIXTURES_DIR / "bundestag"
LIST_URL = BASE_URL + "/ajax/filterlist/de/parlament/plenum/abstimmung/liste/462112-462112"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADER = ("Wahlperiode", "Sitzungnr", "Abstimmnr", "Fraktion/Gruppe", "Name", "Vorname", "Titel", "ja", "nein",
          "Enthaltung", "ungültig", "nichtabgegeben", "Bezeichnung", "Bemerkung")


def member(name: str, faction: str, parliament: str = "Bundestag") -> Politician:
    """A politician with one current mandate."""
    first, last = name.rsplit(" ", 1)
    return Politician(name=name, first_name=first, last_name=last, party=faction,
                      mandates=[Mandate(parliament=parliament, period="2025 - 2029", faction=faction)])


# Ids as in the registry; the names as written there
REGISTRY = {
    "29084": member("Knut Abraham", "CDU_CSU"),
    "78881": member("Michael Brand", "CDU_CSU"),
    "182827": member("Vanessa-Kim Zobel", "CDU_CSU"),
    "184033": member("Cansın Köktürk", "LINKE"),
    "145841": member("Jürgen Kögel", "AFD"),
    "121017": member("Claudia Müller", "GRUENE", parliament="Landtag Mecklenburg-Vorpommern"),
}


def xlsx(rows: list[tuple[Any, ...]], header: tuple[str, ...] = HEADER) -> bytes:
    """Builds an XLSX list; rows are (period, session, number, group, last name, first name, choice)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header)
    columns = {"yes": 7, "no": 8, "abstain": 9, "invalid": 10, "no_show": 11}
    for period, session, number, group, last, first, choice in rows:
        values: list[Any] = [period, session, number, group, last, first, None, 0, 0, 0, 0, 0, f"{first} {last}", None]
        values[columns[choice]] = 1
        sheet.append(values[:len(header)])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def list_entries() -> list[ListedVote]:
    return parse_vote_list((FIXTURES / "list_page.html").read_text(encoding="utf-8"))


def setup_web(fake_web: Any, list_html: str | None = None) -> None:
    """Serves the list page, the vote list and the XLSX lists of the four votes of 2026-11-13."""
    fake_web.add(LIST_PAGE_URL, (FIXTURES / "liste.html").read_text(encoding="utf-8"))
    fake_web.add(LIST_URL, list_html or (FIXTURES / "list_page.html").read_text(encoding="utf-8"))
    for entry in list_entries()[:4]:
        number = int(entry.file_key.split("_")[1])
        if number == 4:
            content = (FIXTURES / "20261113_4_xls.xlsx").read_bytes()
        else:
            content = xlsx([(21, 109, number, "CDU/CSU", "Abraham", "Knut", "yes"),
                            (21, 109, number, "Die Linke", "Köktürk", "Cansin", "no")])
        fake_web.add(entry.xlsx_url, content, content_type=XLSX_TYPE)


def collector(tmp_path: Any, first_vote_date: date = date(2026, 11, 13)) -> BundestagVotesCollector:
    return BundestagVotesCollector(aliases_path=tmp_path / "aliases.toml", first_vote_date=first_vote_date)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_list_page_references_the_vote_list() -> None:
    assert parse_list_page((FIXTURES / "liste.html").read_text(encoding="utf-8")) == LIST_URL.removeprefix(BASE_URL)
    with pytest.raises(ValueError):
        parse_list_page("<html><body>Neue Seite</body></html>")


def test_vote_list_reads_each_table_row_once() -> None:
    entries = list_entries()

    assert len(entries) == 9
    assert entries[0] == ListedVote(date(2026, 11, 13), "Bundeswehreinsatz im Mittelmeer (Sea Guardian)",
                                    "https://www.bundestag.de/resource/blob/1233224/20261113_4_xls.xlsx")
    assert entries[-1].title == "Startchancen- und Qualitätsentwicklungsgesetz"
    assert entries[-1].file_key == "20261112_1.xlsx"


def test_vote_file_of_the_sea_guardian_vote_matches_the_published_result() -> None:
    vote_file = parse_vote_file((FIXTURES / "20261113_4_xls.xlsx").read_bytes())
    members, _unmatched = member_votes(vote_file, MemberMatcher({}), "https://example.org/x.xlsx")
    tally = tally_votes(members)

    assert vote_file.vote_id == "21-109-4" and len(vote_file.members) == 630
    assert (tally["yes"], tally["no"], tally["abstain"], tally["no_show"]) == (371, 186, 2, 71)
    assert sorted(tally["by_faction"]) == ["AFD", "CDU_CSU", "GRUENE", "LINKE", "NONE", "SPD"]
    assert sum(tally["by_faction"]["GRUENE"].values()) == 85
    assert sum(tally["by_faction"]["NONE"].values()) == 3


def test_vote_file_with_a_typo_in_one_row_and_an_invalid_vote() -> None:
    rows = [(21, 5, 1, "SPD", f"Name{index}", "Vorname", "yes") for index in range(20)]
    rows += [(21, 5, 11, "SPD", "Tippfehler", "Vorname", "no"), (21, 5, 1, "AfD", "Ungueltig", "Vorname", "invalid")]

    vote_file = parse_vote_file(xlsx(rows))
    members, _unmatched = member_votes(vote_file, MemberMatcher({}), "https://example.org/x.xlsx")

    assert vote_file.vote_id == "21-5-1" and len(vote_file.members) == 22
    assert tally_votes(members)["no_show"] == 1


def test_vote_file_with_an_extra_column_is_read() -> None:
    header = HEADER[:4] + ("AbgNr",) + HEADER[4:]
    workbook = openpyxl.Workbook()
    workbook.active.append(header)
    workbook.active.append((20, 210, 2, "FDP", 123, "Name", "Vorname", None, 0, 1, 0, 0, 0, "Vorname Name", None))
    buffer = io.BytesIO()
    workbook.save(buffer)

    vote_file = parse_vote_file(buffer.getvalue())

    assert vote_file.vote_id == "20-210-2" and vote_file.members[0].choice == "no"


@pytest.mark.parametrize("content", [
    xlsx([(21, 5, 1, "SPD", "A", "B", "yes"), (21, 5, 2, "SPD", "C", "D", "yes")]),
    xlsx([(21, 5, 1, "SPD", "A", "B", "yes")], header=HEADER[:7]),
    b"<html>Kein Arbeitsblatt</html>",
])
def test_vote_files_with_several_votes_missing_columns_or_another_format_are_rejected(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_vote_file(content)


def test_openpyxl_parses_with_defusedxml() -> None:
    from openpyxl.xml import DEFUSEDXML

    assert DEFUSEDXML


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_member_matcher_rules() -> None:
    registry = {**REGISTRY, "1": member("Thomas Müller", "SPD"), "2": member("Thomas Müller", "CDU_CSU")}
    matcher = MemberMatcher(registry, {fold_name("Hans Beispiel"): "999"})

    assert matcher.match("Knut", "Abraham", "CDU_CSU") == "29084"
    assert matcher.match("Michael", "Brand (Fulda)", "CDU_CSU") == "78881"
    assert matcher.match("Vanessa", "Zobel", "CDU_CSU") == "182827"
    assert matcher.match("Cansin", "Köktürk", "LINKE") == "184033"
    assert matcher.match("Jürgen", "Koegel", "AFD") == "145841"
    assert matcher.match("Thomas", "Müller", "SPD") == "1"
    assert matcher.match("Thomas", "Müller", None) is None
    assert matcher.match("Claudia", "Müller", "GRUENE") is None
    assert matcher.match("Hans", "Beispiel", "SPD") == "999"
    assert len(matcher) == 7


def test_a_politician_matched_by_two_rows_gets_no_vote() -> None:
    vote_file = VoteFile(period=21, session=1, number=1, members=(
        *(parse_vote_file(xlsx([(21, 1, 1, "CDU/CSU", "Abraham", "Knut", "yes"),
                                (21, 1, 1, "CDU/CSU", "Abraham", "Knut", "no")])).members),
    ))

    members, unmatched = member_votes(vote_file, MemberMatcher(REGISTRY), "https://example.org/x.xlsx")

    assert [m.politician_id for m in members] == [None, None] and len(unmatched) == 2


def test_alias_file(tmp_path) -> None:
    path = tmp_path / "aliases.toml"
    assert load_aliases(path) == {}
    path.write_text('[[alias]]\nname = "Hans Beispiel"\npolitician_id = "999"\n', encoding="utf-8")
    assert load_aliases(path) == {"hans beispiel": "999"}
    path.write_text('[[alias]]\nname = "Hans Beispiel"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_aliases(path)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

def test_collect_writes_the_votes_from_the_first_vote_date_on(fake_web, make_ctx, tmp_path) -> None:
    setup_web(fake_web)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)

    result = collector(tmp_path).collect(ctx, CollectorEntry(first_run=ctx.today))

    votes = read_dataset(ctx.settings.output_dir, "roll_call_votes")
    assert [vote["record_id"] for vote in votes] == [f"bt-vote:21-109-{number}" for number in (1, 2, 3, 4)]
    sea_guardian = votes[-1]
    assert sea_guardian["vote_id"] == "21-109-4" and sea_guardian["vote_date"] == "2026-11-13"
    assert sea_guardian["title"] == "Bundeswehreinsatz im Mittelmeer (Sea Guardian)"
    assert sea_guardian["parliament_period"] == "Bundestag 2025 - 2029"
    assert (sea_guardian["yes"], sea_guardian["no"], sea_guardian["abstain"], sea_guardian["no_show"]) == (371, 186, 2, 71)
    assert sea_guardian["source_id"] == "bundestag" and sea_guardian["license"] == LICENSE
    assert sea_guardian["source_url"] == "https://www.bundestag.de/resource/blob/1233224/20261113_4_xls.xlsx"
    assert sea_guardian["topics"] == [] and sea_guardian["printed_papers"] == [] and sea_guardian["accepted"] is None
    assert sea_guardian["schema_version"] == "1.1"
    member_records = {(r["vote_id"], r["politician_id"]): r for r in read_dataset(ctx.settings.output_dir, "mp_votes")}
    assert member_records[("21-109-4", "182827")]["vote"] == "yes"
    assert member_records[("21-109-4", "145841")]["vote"] == "no_show"
    assert member_records[("21-109-4", "184033")]["faction"] == "LINKE"
    assert member_records[("21-109-4", "78881")]["record_id"] == "bt-vote:21-109-4:78881"
    assert len([key for key in member_records if key[0] == "21-109-4"]) == 5
    assert result.counters["new"] == 4 and result.complete
    assert result.counters["member_votes_without_politician"] == 625


def test_second_run_reads_no_xlsx_list_again(fake_web, make_ctx, tmp_path) -> None:
    setup_web(fake_web)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)
    collector(tmp_path).collect(ctx, CollectorEntry(first_run=ctx.today))
    requested = len(fake_web.requested)

    result = collector(tmp_path).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert result.counters.get("new", 0) == 0
    assert not any(url.endswith(".xlsx") for url in fake_web.requested[requested:])


def test_vote_without_xlsx_list_is_read_in_a_later_run(fake_web, make_ctx, tmp_path) -> None:
    html = (FIXTURES / "list_page.html").read_text(encoding="utf-8")
    without_file = html.replace("20261113_3_xls.xlsx", "20261113_3_xls.pdf")
    setup_web(fake_web, without_file)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)

    first = collector(tmp_path).collect(ctx, CollectorEntry(first_run=ctx.today))
    fake_web.add(LIST_URL, html)
    second = collector(tmp_path).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert first.counters["new"] == 3 and first.counters["without_file"] == 1
    assert second.counters["new"] == 1
    assert len(read_dataset(ctx.settings.output_dir, "roll_call_votes")) == 4


def test_votes_before_the_first_vote_date_are_not_read(fake_web, make_ctx, tmp_path) -> None:
    setup_web(fake_web)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)

    result = collector(tmp_path, first_vote_date=date(2026, 11, 14)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters.get("new", 0) == 0
    assert not any(url.endswith(".xlsx") for url in fake_web.requested)


def test_refused_list_raises_blocked(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(LIST_PAGE_URL, "Forbidden", status=403)
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)

    with pytest.raises(BlockedError):
        collector(tmp_path).collect(ctx, CollectorEntry(first_run=ctx.today))


def test_tdm_reservation_stops_the_collector(fake_web, make_ctx, tmp_path) -> None:
    setup_web(fake_web)
    fake_web.add(BASE_URL + "/.well-known/tdmrep.json", [{"location": "/*", "tdm-reservation": 1}])
    ctx = make_ctx()
    save_politicians(ctx.settings.state_dir, REGISTRY)

    with pytest.raises(TdmReservedError):
        collector(tmp_path).collect(ctx, CollectorEntry(first_run=ctx.today))
    assert read_dataset(ctx.settings.output_dir, "roll_call_votes") == []


def test_empty_registry_and_unknown_period_raise(fake_web, make_ctx, tmp_path) -> None:
    setup_web(fake_web)
    with pytest.raises(ValueError):
        collector(tmp_path).collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 20)))
    with pytest.raises(ValueError):
        BundestagVotesCollector._fields(VoteFile(period=22, session=1, number=1, members=()), list_entries()[0], [])
