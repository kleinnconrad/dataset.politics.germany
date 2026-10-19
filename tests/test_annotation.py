"""Tests of the Gemini annotation with a fake client."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from google.genai import types

from annotation import (
    BATCH_SIZE,
    AnnotationInput,
    CallBudgetExceededError,
    GeminiAnnotator,
    build_prompt,
    clean_annotation,
    extraction_hash,
)
from schema import ItemAnnotation
from settings import Settings


def label(number: int, **overrides: Any) -> dict[str, Any]:
    """Returns a valid model answer for one item."""
    base = {
        "item_number": number, "is_political": True, "political_level": "Federal", "states_concerned": [],
        "cap_topic": "Macroeconomics", "cap_topics_secondary": [], "issue_detail": "Haushalt 2027",
        "article_type": "News", "event_types": ["Coalition conflict"], "event_status": "Ongoing",
        "government_involved": True,
        "parties": [{"party": "CDU_CSU", "role": "Initiator", "tone": "Neutral", "quoted": True},
                    {"party": "SPD", "role": "Target", "tone": "Negative", "quoted": False}],
        "politicians": [{"name": "Max Muster", "party": "SPD", "role": "Commentator", "tone": "Neutral"}],
        "overall_tone": "Negative", "government_tone": "Negative", "frame": "Conflict",
        "conflict_pairs": [{"party_a": "SPD", "party_b": "CDU_CSU"}], "reports_poll": False,
        "poll_institute": None, "upcoming_events": [], "communication_type": "Not applicable",
        "populism_markers": [],
    }
    base.update(overrides)
    return base


def response(items: list[dict[str, Any]], finish: Any = types.FinishReason.STOP) -> SimpleNamespace:
    """Builds a fake GenerateContentResponse."""
    return SimpleNamespace(
        text=json.dumps({"items": items}), candidates=[SimpleNamespace(finish_reason=finish)],
        prompt_feedback=None, usage_metadata=None,
    )


class FakeClient:
    """Fake genai client whose responses are produced by a function of the prompt."""

    def __init__(self, answer: Callable[[str], SimpleNamespace]) -> None:
        self.prompts: list[str] = []
        self.models = SimpleNamespace(generate_content=self._generate)
        self._answer = answer

    def _generate(self, model: str, contents: str, config: Any) -> SimpleNamespace:
        self.prompts.append(contents)
        return self._answer(contents)


def items(count: int) -> list[AnnotationInput]:
    return [AnnotationInput(kind="news article", source="News Example (national daily newspaper)",
                            published="2026-10-19", title=f"Titel {n}", teaser="Teaser") for n in range(count)]


def annotator(answer: Callable[[str], SimpleNamespace], **overrides: Any) -> tuple[GeminiAnnotator, FakeClient]:
    client = FakeClient(answer)
    settings = Settings(**overrides)
    return GeminiAnnotator(settings, client=client, sleep=lambda s: None, clock=lambda: 0.0), client


def test_annotate_aligns_labels_with_items_by_number() -> None:
    def answer(prompt: str) -> SimpleNamespace:
        return response([label(2, cap_topic="Health"), label(1)])

    ann, _ = annotator(answer)
    labels = ann.annotate(items(2))

    assert labels[0] is not None and labels[0].cap_topic == "Macroeconomics"
    assert labels[1] is not None and labels[1].cap_topic == "Health"


def test_annotate_returns_none_for_items_missing_in_the_response() -> None:
    ann, _ = annotator(lambda prompt: response([label(1)]))
    labels = ann.annotate(items(3))
    assert labels[0] is not None and labels[1] is None and labels[2] is None


def test_unusable_response_is_split_until_single_items() -> None:
    def answer(prompt: str) -> SimpleNamespace:
        count = prompt.count("\nKind:")
        if count > 1:
            return response([], finish=types.FinishReason.MAX_TOKENS)
        return response([label(1)])

    ann, client = annotator(answer)
    labels = ann.annotate(items(4))

    assert all(result is not None for result in labels)
    assert len(client.prompts) == 1 + 2 + 4


def test_call_budget_stops_the_annotation() -> None:
    ann, _ = annotator(lambda prompt: response([label(1)]), max_gemini_calls_per_run=1)
    ann.annotate(items(1))
    with pytest.raises(CallBudgetExceededError):
        ann.annotate(items(1))


def test_call_budget_holds_across_threads() -> None:
    ann, client = annotator(lambda prompt: response([label(1)]), max_gemini_calls_per_run=3)

    def call(_: int) -> bool:
        try:
            ann.annotate(items(1))
        except CallBudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call, range(8)))

    assert sum(results) == 3
    assert len(client.prompts) == 3 and ann.usage.calls == 3


def test_annotate_rejects_oversized_batches() -> None:
    ann, _ = annotator(lambda prompt: response([]))
    with pytest.raises(ValueError):
        ann.annotate(items(BATCH_SIZE + 1))


def test_clean_annotation_normalizes_lists_and_press_fields() -> None:
    raw = ItemAnnotation.model_validate(label(
        1,
        parties=[{"party": "SPD", "role": "Target", "tone": "Negative", "quoted": False},
                 {"party": "SPD", "role": "Initiator", "tone": "Positive", "quoted": True}],
        conflict_pairs=[{"party_a": "SPD", "party_b": "SPD"}, {"party_a": "SPD", "party_b": "AFD"},
                        {"party_a": "AFD", "party_b": "SPD"}],
        cap_topics_secondary=["Macroeconomics", "Health", "Labor", "Energy"],
        upcoming_events=[{"event_type": "Legislation", "date": "2026-11-05"},
                         {"event_type": "Legislation", "date": "next week"},
                         {"event_type": "Legislation", "date": "2020-01-01"}],
        communication_type="Policy proposal", populism_markers=["Anti-elite"], issue_detail="  Haushalt   2027 ",
    ))

    news = clean_annotation(raw, "news article", "2026-10-19")
    press = clean_annotation(raw, "press release", "2026-10-19")

    assert [p.party for p in news.parties] == ["SPD"] and news.parties[0].tone == "Negative"
    assert [(p.party_a, p.party_b) for p in news.conflict_pairs] == [("AFD", "SPD")]
    assert news.cap_topics_secondary == ["Health", "Labor"]
    assert [e.date for e in news.upcoming_events] == ["2026-11-05"]
    assert news.issue_detail == "Haushalt 2027"
    assert news.communication_type == "Not applicable" and news.populism_markers == []
    assert press.communication_type == "Policy proposal" and press.populism_markers == ["Anti-elite"]


def test_build_prompt_numbers_items_and_marks_missing_text() -> None:
    prompt = build_prompt([AnnotationInput(kind="press release", source="SPD (party)", published=None, title="T")])
    assert "Item 1" in prompt and "Kind: press release" in prompt
    assert "Published: (unknown)" in prompt and "Teaser: (none)" in prompt


def test_extraction_hash_changes_with_the_model() -> None:
    assert extraction_hash(Settings()) != extraction_hash(Settings(gemini_model="other-model"))
