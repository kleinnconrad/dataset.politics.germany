"""Tests of the abgeordnetenwatch collectors (politicians, roll-call votes and member votes)."""
import json
from datetime import date

import httpx
import pytest

import collectors.parliament as parliament
from collectors.parliament import (
    API_URL,
    PAGE_SIZE,
    MemberVote,
    PoliticiansCollector,
    RollCallVotesCollector,
    party_code,
    printed_papers,
    select_periods,
    strip_period,
    tally_votes,
)
from conftest import FIXTURES_DIR, read_dataset
from entities import load_politicians
from state import CollectorEntry

POLITICIANS_URL = API_URL + "politicians"
EMPTY_LIST = {"meta": {"status": "ok", "status_message": "", "result": {"count": 0, "total": 0}}, "data": []}


def aw_url(path: str, **params: object) -> str:
    """Returns the URL of the first page of an abgeordnetenwatch list, as requested by the collectors."""
    return str(httpx.URL(API_URL + path, params={**params, "range_end": PAGE_SIZE}))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / "abgeordnetenwatch" / name).read_text(encoding="utf-8"))


def _politician_id(mandates_fixture: str, mandate_id: int) -> str:
    """Returns the politician id of a mandate in a mandate fixture."""
    for mandate in _fixture(mandates_fixture)["data"]:
        if mandate["id"] == mandate_id:
            return str(mandate["politician"]["id"])
    raise KeyError(mandate_id)


def _serve_politicians(fake_web, mandates_161: dict | None = None) -> None:
    fake_web.add_fixture(aw_url("parliaments"), "abgeordnetenwatch/parliaments.json")
    if mandates_161 is None:
        fake_web.add_fixture(aw_url("candidacies-mandates", parliament_period="161", type="mandate"),
                             "abgeordnetenwatch/mandates_current_161.json")
    else:
        fake_web.add(aw_url("candidacies-mandates", parliament_period="161", type="mandate"), mandates_161)
    fake_web.add_fixture(aw_url("candidacies-mandates", parliament_period="137", type="mandate"),
                         "abgeordnetenwatch/mandates_current_137.json")
    fake_web.add_fixture(POLITICIANS_URL, "abgeordnetenwatch/politicians.json")


def _serve_votes(fake_web) -> None:
    fake_web.add_fixture(aw_url("parliament-periods", parliament=5, type="legislature"),
                         "abgeordnetenwatch/legislatures_5.json")
    fake_web.add_fixture(aw_url("polls", field_legislature="161"), "abgeordnetenwatch/polls_161.json")
    fake_web.add_fixture(aw_url("polls", field_legislature="132"), "abgeordnetenwatch/polls_132.json")
    fake_web.add_fixture(aw_url("candidacies-mandates", parliament_period="161", type="mandate", current_on="all"),
                         "abgeordnetenwatch/mandates_all_161.json")
    fake_web.add_fixture(aw_url("candidacies-mandates", parliament_period="132", type="mandate", current_on="all"),
                         "abgeordnetenwatch/mandates_all_132.json")
    fake_web.add_fixture(aw_url("votes", poll=6733), "abgeordnetenwatch/votes_6733.json")
    fake_web.add_fixture(aw_url("votes", poll=5864), "abgeordnetenwatch/votes_5864.json")
    # Poll 6731 is listed, but abgeordnetenwatch has not entered its votes yet
    fake_web.add(aw_url("votes", poll=6731), EMPTY_LIST)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def test_party_code_maps_group_and_party_labels() -> None:
    assert party_code("BÜNDNIS 90/­DIE GRÜNEN (Bundestag 2025 - 2029)") == "GRUENE"
    assert party_code("CDU/CSU (Bundestag 2025 - 2029)") == "CDU_CSU"
    assert party_code("Die Linke. (Gruppe) (Bundestag 2021 - 2025)") == "LINKE"
    assert party_code("BSW (Gruppe) (Bundestag 2021 - 2025)") == "BSW"
    assert party_code("FREIE WÄHLER (Bayern 2023 - 2028)") == "FW"
    assert party_code("fraktionslos (Bundestag 2025 - 2029)") == "NONE"
    assert party_code("parteilos") == "NONE"
    assert party_code("Bündnis Deutschland (Bremen 2023 - 2027)") == "OTHER"
    assert party_code(None) is None


def test_strip_period_removes_only_the_period_suffix() -> None:
    period = "Bundestag 2025 - 2029"
    assert strip_period("120 - Recklinghausen I (Bundestag 2025 - 2029)", period) == "120 - Recklinghausen I"
    assert strip_period("Landesliste (Saarland 2022 - 2027)", "Saarland 2022 - 2027") == "Landesliste"
    assert strip_period("Wahlkreis (Bayern)", period) == "Wahlkreis (Bayern)"
    assert strip_period(None, period) is None


def test_printed_papers_reads_drucksache_links() -> None:
    intro = ('<a href="https://dserver.bundestag.de/btd/21/078/2107869.pdf">Entwurf</a> und '
             '<a href="https://dserver.bundestag.de/btd/21/086/2108626.pdf">Beschlussempfehlung</a> '
             '<a href="https://dserver.bundestag.de/btd/21/078/2107869.pdf">noch einmal</a>')
    assert printed_papers(intro) == ["21/7869", "21/8626"]
    assert printed_papers(None) == []


def test_tally_votes_computes_rice_index_and_defectors() -> None:
    members = (
        [MemberVote(None, str(i), "CDU_CSU", "yes") for i in range(4)]
        + [MemberVote(None, "10", "CDU_CSU", "no"), MemberVote(None, "11", "CDU_CSU", "no_show")]
        + [MemberVote(None, "20", "SPD", "yes"), MemberVote(None, "21", "SPD", "no")]
        + [MemberVote(None, "30", "LINKE", "abstain")]
        + [MemberVote(None, "40", "NONE", "yes"), MemberVote(None, "41", "NONE", "no")]
    )
    tally = tally_votes(members)
    assert (tally["yes"], tally["no"], tally["abstain"], tally["no_show"]) == (6, 3, 1, 1)
    assert tally["by_faction"]["CDU_CSU"] == {"yes": 4, "no": 1, "abstain": 0, "no_show": 1}
    assert tally["cohesion"] == {"CDU_CSU": 0.6, "LINKE": None, "SPD": 0.0}
    # A tied group has no majority, so nobody in it defects; NONE is not a group
    assert tally["defectors"] == {"CDU_CSU": 1, "LINKE": 0, "SPD": 0}


def test_select_periods_ignores_periods_that_have_not_started() -> None:
    legislatures = _fixture("legislatures_5.json")["data"] + [
        {"id": 999, "label": "Bundestag 2029 - 2033", "start_date_period": "2029-03-01"},
    ]
    current, previous = select_periods(legislatures, date(2026, 10, 20))
    assert current["id"] == 161
    assert previous is not None and previous["id"] == 132


# ---------------------------------------------------------------------------
# PoliticiansCollector
# ---------------------------------------------------------------------------

def test_politicians_collector_builds_registry_and_records(fake_web, make_ctx) -> None:
    _serve_politicians(fake_web)
    ctx = make_ctx()

    result = PoliticiansCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    registry = load_politicians(ctx.settings.state_dir)
    assert len(registry) == 7
    assert result.counters["new"] == 7
    assert result.counters["parliaments"] == 2
    # The European Parliament is not read
    assert not any("parliament_period=155" in url for url in fake_web.requested)

    zschau = registry["175453"]
    assert zschau.name == "Katrin Zschau"
    assert zschau.party == "SPD"
    assert zschau.sex == "female"
    assert zschau.wikidata_qid == "Q108760646"
    mandate = zschau.mandates[0]
    assert (mandate.parliament, mandate.period, mandate.faction) == ("Bundestag", "Bundestag 2025 - 2029", "SPD")
    assert mandate.constituency == "14 - Rostock – Landkreis Rostock II"
    assert mandate.electoral_list is None
    assert mandate.start_date == "2026-07-20"

    assert registry["184332"].mandates[0].faction == "GRUENE"
    seidler = registry["176316"]
    assert seidler.party == "SSW"
    assert seidler.mandates[0].faction == "NONE"
    becker = registry["177567"]
    assert [m.parliament for m in becker.mandates] == ["Bundestag", "Landtag Saarland"]
    assert becker.mandates[1].electoral_list == "Wahlkreisliste Saarlouis"

    records = {record["politician_id"]: record for record in read_dataset(ctx.settings.output_dir, "politicians")}
    assert len(records) == 7
    assert records["177567"]["jurisdiction"] == "DE"
    assert records["185726"]["jurisdiction"] == "DE-SL"
    assert records["175453"]["record_id"] == "aw-politician:175453"
    assert records["175453"]["license"] == "CC0-1.0"
    assert records["175453"]["revision"] == 0
    assert records["175453"]["mandates"][0]["mandate_won"] == "moved_up"


def test_politicians_second_run_writes_only_changes_and_drops_former_members(fake_web, make_ctx) -> None:
    _serve_politicians(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    PoliticiansCollector().collect(ctx, entry)

    unchanged = PoliticiansCollector().collect(make_ctx(), entry)
    assert unchanged.counters.get("new", 0) == 0 and unchanged.counters.get("changed", 0) == 0

    mandates = _fixture("mandates_current_161.json")
    mandates["data"] = [m for m in mandates["data"] if m["politician"]["id"] != 175453]
    mandates["meta"]["result"]["total"] = len(mandates["data"])
    politicians = _fixture("politicians.json")
    for politician in politicians["data"]:
        if politician["id"] == 176316:
            politician["party"] = {"id": 999, "entity_type": "party", "label": "parteilos"}
    _serve_politicians(fake_web, mandates_161=mandates)
    fake_web.add(POLITICIANS_URL, politicians)

    result = PoliticiansCollector().collect(make_ctx(), entry)

    assert result.counters["changed"] == 1
    assert result.counters["left"] == 1
    registry = load_politicians(ctx.settings.state_dir)
    assert "175453" not in registry
    assert registry["176316"].party == "NONE"
    revisions = sorted((r["revision"], r["party"]) for r in read_dataset(ctx.settings.output_dir, "politicians")
                       if r["politician_id"] == "176316")
    assert revisions == [(0, "SSW"), (1, "NONE")]


def test_politicians_dry_run_keeps_the_registry_file(fake_web, make_ctx) -> None:
    _serve_politicians(fake_web)
    ctx = make_ctx(dry_run=True)

    PoliticiansCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert ctx.written["politicians"] == 7
    assert load_politicians(ctx.settings.state_dir) == {}


def test_politicians_fails_without_bundestag_mandates(fake_web, make_ctx) -> None:
    _serve_politicians(fake_web, mandates_161=EMPTY_LIST)
    ctx = make_ctx()

    with pytest.raises(ValueError, match="Bundestag"):
        PoliticiansCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert load_politicians(ctx.settings.state_dir) == {}


# ---------------------------------------------------------------------------
# RollCallVotesCollector
# ---------------------------------------------------------------------------

def test_roll_call_votes_first_run_imports_current_and_previous_period(fake_web, make_ctx) -> None:
    _serve_votes(fake_web)
    ctx = make_ctx()

    result = RollCallVotesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    votes = {record["vote_id"]: record for record in read_dataset(ctx.settings.output_dir, "roll_call_votes")}
    assert set(votes) == {"6733", "5864"}
    assert result.counters["without_votes"] == 1
    assert result.cursor == {"period": "161", "backfill_periods": []}

    old = votes["5864"]
    assert old["record_id"] == "aw-poll:5864"
    assert old["parliament_period"] == "Bundestag 2021 - 2025"
    assert old["vote_date"] == "2025-01-31"
    assert old["accepted"] is False
    assert old["backfilled"] is True
    assert (old["yes"], old["no"], old["abstain"], old["no_show"]) == (10, 7, 1, 2)
    assert old["by_faction"]["FDP"] == {"yes": 3, "no": 1, "abstain": 1, "no_show": 0}
    assert old["by_faction"]["NONE"] == {"yes": 1, "no": 1, "abstain": 0, "no_show": 0}
    assert old["by_faction"]["GRUENE"]["no"] == 2
    assert old["cohesion"]["FDP"] == 0.5
    assert old["cohesion"]["CDU_CSU"] == 1.0
    assert "NONE" not in old["cohesion"] and "NONE" not in old["defectors"]
    assert old["defectors"]["FDP"] == 1
    assert sum(old["defectors"].values()) == 1
    assert set(old["by_faction"]) == {"CDU_CSU", "SPD", "GRUENE", "FDP", "AFD", "BSW", "LINKE", "NONE"}

    new = votes["6733"]
    assert new["backfilled"] is False
    assert new["accepted"] is True
    assert new["cohesion"]["LINKE"] is None
    assert new["topics"][0] == "Energie"
    assert new["printed_papers"][0] == "21/7869"

    mp_votes = read_dataset(ctx.settings.output_dir, "mp_votes")
    # 11 member votes of 6733 and 20 of 5864, one of which has a mandate that is not in the mandate list
    assert len(mp_votes) == 30
    assert result.counters["member_votes_without_politician"] == 1
    lechte = _politician_id("mandates_all_132.json", 53633)
    defector = next(r for r in mp_votes if r["record_id"] == f"aw-vote:5864:{lechte}")
    assert (defector["faction"], defector["vote"], defector["voted_against_faction"]) == ("FDP", "no", True)
    seidler = next(r for r in mp_votes if r["vote_id"] == "5864" and r["faction"] == "NONE" and r["vote"] == "no")
    assert seidler["voted_against_faction"] is False
    assert all(r["license"] == "CC0-1.0" and r["jurisdiction"] == "DE" for r in mp_votes)


def test_roll_call_votes_second_run_reads_only_new_polls(fake_web, make_ctx) -> None:
    _serve_votes(fake_web)
    ctx = make_ctx()
    first = RollCallVotesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    fake_web.requested.clear()

    entry = CollectorEntry(first_run=ctx.today, cursor=first.cursor)
    result = RollCallVotesCollector().collect(make_ctx(), entry)

    assert result.counters.get("new", 0) == 0
    assert aw_url("polls", field_legislature="132") not in fake_web.requested
    assert aw_url("votes", poll=6733) not in fake_web.requested
    # The poll without votes is tried again
    assert aw_url("votes", poll=6731) in fake_web.requested
    assert len(read_dataset(ctx.settings.output_dir, "roll_call_votes")) == 2


def test_roll_call_votes_cap_defers_the_rest_to_later_runs(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(parliament, "MAX_NEW_POLLS_PER_RUN", 2)
    _serve_votes(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    first = RollCallVotesCollector().collect(ctx, entry)

    assert first.counters["deferred"] == 1
    assert first.cursor == {"period": "161", "backfill_periods": ["132"]}
    assert [r["vote_id"] for r in read_dataset(ctx.settings.output_dir, "roll_call_votes")] == ["6733"]

    second = RollCallVotesCollector().collect(make_ctx(), entry.model_copy(update={"cursor": first.cursor}))

    assert second.cursor == {"period": "161", "backfill_periods": []}
    assert {r["vote_id"] for r in read_dataset(ctx.settings.output_dir, "roll_call_votes")} == {"6733", "5864"}


def test_roll_call_votes_backfill_has_no_cap(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(parliament, "MAX_NEW_POLLS_PER_RUN", 1)
    _serve_votes(fake_web)
    ctx = make_ctx()
    ctx.backfill = True

    result = RollCallVotesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["deferred"] == 0 and result.complete
    assert result.cursor == {"period": "161", "backfill_periods": []}
    assert {r["vote_id"] for r in read_dataset(ctx.settings.output_dir, "roll_call_votes")} == {"6733", "5864"}


def test_roll_call_votes_stop_at_the_run_deadline(fake_web, make_ctx) -> None:
    _serve_votes(fake_web)
    ctx = make_ctx()
    ctx.deadline = ctx.started

    result = RollCallVotesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete
    assert result.counters["deferred"] == 3
    assert result.cursor == {"period": "161", "backfill_periods": ["132"]}
    assert read_dataset(ctx.settings.output_dir, "roll_call_votes") == []


def test_roll_call_votes_finishes_the_ended_period_after_a_change(fake_web, make_ctx) -> None:
    _serve_votes(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=date(2022, 1, 1), cursor={"period": "132", "backfill_periods": []})

    result = RollCallVotesCollector().collect(ctx, entry)

    assert aw_url("polls", field_legislature="132") in fake_web.requested
    assert result.cursor == {"period": "161", "backfill_periods": []}
    votes = {r["vote_id"]: r for r in read_dataset(ctx.settings.output_dir, "roll_call_votes")}
    assert votes["5864"]["backfilled"] is False
