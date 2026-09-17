"""
Real pytest assertions for TrainingGenerator, covering:
  - M0: procedures HTML renders the full step body and HTML-escapes document text
  - M1: Bloom's-aligned, source-cited learning objectives; section citations
"""

import os

import pytest

from src.parser import SOPParser
from src.generator import TrainingGenerator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_SOP = os.path.join(REPO_ROOT, "examples", "sample_sop.txt")
SAMPLE_SOP_NUMBERED = os.path.join(REPO_ROOT, "examples", "sample_sop_numbered.txt")

VALID_BLOOM_LEVELS = {"Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"}


@pytest.fixture(scope="module")
def sample_sop():
    return SOPParser().parse(SAMPLE_SOP)


@pytest.fixture(scope="module")
def numbered_sop():
    return SOPParser().parse(SAMPLE_SOP_NUMBERED)


@pytest.fixture(scope="module")
def training_module(sample_sop):
    return TrainingGenerator().generate(sample_sop)


@pytest.fixture(scope="module")
def numbered_training_module(numbered_sop):
    return TrainingGenerator().generate(numbered_sop)


def _assert_span_resolves(sop, span):
    assert span is not None
    s, e = span
    assert 1 <= s <= e <= len(sop.lines)


class TestLearningObjectives:
    def test_no_ellipsis_truncation(self, training_module):
        for obj in training_module.learning_objectives:
            assert "..." not in obj

    def test_capped_at_eight(self, training_module):
        assert len(training_module.learning_objectives) <= 8

    def test_safety_objective_present_when_warnings_exist(self, sample_sop, training_module):
        assert sample_sop.safety_warnings  # sanity: sample has warnings
        assert any(o["source_ref"]["kind"] == "safety" for o in training_module.objectives)
        assert any("hazard" in obj for obj in training_module.learning_objectives)


class TestBloomObjectives:
    """M1: structured, Bloom's-aligned objectives with resolvable citations."""

    def test_learning_objectives_mirrors_structured_list(self, training_module):
        assert training_module.learning_objectives == [o["text"] for o in training_module.objectives]

    def test_every_objective_has_valid_bloom_level(self, training_module):
        for obj in training_module.objectives:
            assert obj["bloom_level"] in VALID_BLOOM_LEVELS
            assert obj["verb"]

    def test_every_objective_source_ref_resolves(self, sample_sop, training_module):
        for obj in training_module.objectives:
            ref = obj["source_ref"]
            assert "kind" in ref and "span" in ref and "step_number" in ref and "term" in ref
            _assert_span_resolves(sample_sop, ref["span"])

    def test_no_objective_exceeds_eight_total(self, training_module):
        assert len(training_module.objectives) <= 8

    def test_definitions_map_to_remember(self, sample_sop, training_module):
        def_objs = [o for o in training_module.objectives if o["source_ref"]["kind"] == "definition"]
        assert def_objs  # sample has definitions
        for obj in def_objs:
            assert obj["bloom_level"] == "Remember"
            assert obj["text"].startswith("Define ")
            assert obj["source_ref"]["term"] in sample_sop.definitions

    def test_purpose_maps_to_understand(self, training_module):
        purpose_objs = [o for o in training_module.objectives if o["source_ref"]["kind"] == "purpose"]
        # purpose objective may be squeezed out by the 8-cap on the busy sample
        # fixture, so only assert its shape when present.
        for obj in purpose_objs:
            assert obj["bloom_level"] == "Understand"

    def test_safety_maps_to_evaluate(self, training_module):
        safety_objs = [o for o in training_module.objectives if o["source_ref"]["kind"] == "safety"]
        assert safety_objs
        assert safety_objs[0]["bloom_level"] == "Evaluate"

    def test_steps_map_to_apply_and_get_grouped_past_six(self, sample_sop, training_module):
        # sample_sop.txt has 9 steps, well past the grouping threshold, and
        # 4 definitions + purpose + scope + safety already crowd the 8-cap,
        # so the 9 steps must collapse into a single grouped objective.
        step_objs = [o for o in training_module.objectives if o["source_ref"]["kind"] == "step"]
        assert step_objs
        for obj in step_objs:
            assert obj["bloom_level"] == "Apply"
        assert any("–" in o["source_ref"]["step_number"] for o in step_objs)

    def test_small_step_count_not_grouped(self, numbered_sop, numbered_training_module):
        # sample_sop_numbered.txt has only 3 steps: no grouping needed.
        step_objs = [o for o in numbered_training_module.objectives
                     if o["source_ref"]["kind"] == "step"]
        assert len(step_objs) == len(numbered_sop.procedures)
        for obj, proc in zip(step_objs, numbered_sop.procedures):
            assert obj["source_ref"]["step_number"] == str(proc["step_number"])


class TestSectionCitations:
    def test_every_section_has_citations_key(self, training_module):
        for section in training_module.sections:
            assert "citations" in section

    def test_all_citations_resolve(self, sample_sop, training_module):
        for section in training_module.sections:
            for ref in section["citations"]:
                _assert_span_resolves(sample_sop, ref["span"])

    def test_procedures_section_cites_every_step(self, sample_sop, training_module):
        procedures_section = next(s for s in training_module.sections if s["id"] == "procedures")
        assert len(procedures_section["citations"]) == len(sample_sop.procedures)

    def test_summary_cites_only_purpose(self, training_module):
        summary_section = next(s for s in training_module.sections if s["id"] == "summary")
        kinds = {ref["kind"] for ref in summary_section["citations"]}
        assert kinds <= {"purpose"}

    def test_safety_section_cites_every_warning(self, sample_sop, training_module):
        safety_section = next(s for s in training_module.sections if s["id"] == "safety")
        assert len(safety_section["citations"]) == len(sample_sop.safety_warnings)

    def test_definitions_section_cites_every_term(self, sample_sop, training_module):
        definitions_section = next(s for s in training_module.sections if s["id"] == "definitions")
        cited_terms = {ref["term"] for ref in definitions_section["citations"]}
        assert cited_terms == set(sample_sop.definitions.keys())

    def test_section_ids_and_order_unaffected(self, training_module):
        ids = [s["id"] for s in training_module.sections]
        assert ids == ["intro", "safety", "definitions", "procedures", "summary"]


class TestProceduresSection:
    def _procedures_html(self, training_module):
        section = next(s for s in training_module.sections if s["id"] == "procedures")
        return section["content"]

    def test_contains_step_body_text(self, training_module):
        html_content = self._procedures_html(training_module)
        assert "Press the red emergency stop button" in html_content

    def test_contains_step_title(self, training_module):
        html_content = self._procedures_html(training_module)
        assert "Activate Emergency Stop" in html_content

    def test_substeps_rendered_in_nested_list(self, training_module):
        html_content = self._procedures_html(training_module)
        assert "<ol type='a'>" in html_content
        assert "cut power to all moving equipment" in html_content

    def test_section_ids_and_order_unchanged(self, training_module):
        ids = [s["id"] for s in training_module.sections]
        assert "procedures" in ids
        procedures_section = next(s for s in training_module.sections if s["id"] == "procedures")
        assert procedures_section["order"] == 4

    def test_script_tag_in_body_is_escaped(self):
        sop = SOPParser()._extract_structure(
            "PROCEDURE:\n\nStep 1: Dangerous Step\n"
            "Do the thing <script>alert('xss')</script> carefully.\n"
        )
        module = TrainingGenerator().generate(sop)
        section = next(s for s in module.sections if s["id"] == "procedures")
        assert "<script>" not in section["content"]
        assert "&lt;script&gt;" in section["content"]


class TestOtherSectionsEscaping:
    def test_definitions_escaped(self):
        sop = SOPParser()._extract_structure(
            "DEFINITIONS:\n"
            "Bad Term <b>: A <script>alert(1)</script> definition.\n"
        )
        module = TrainingGenerator().generate(sop)
        section = next(s for s in module.sections if s["id"] == "definitions")
        assert "<script>" not in section["content"]

    def test_safety_warning_escaped(self):
        sop = SOPParser()._extract_structure(
            "WARNING: Do not <script>alert(1)</script> touch the panel.\n"
        )
        module = TrainingGenerator().generate(sop)
        section = next(s for s in module.sections if s["id"] == "safety")
        assert "<script>" not in section["content"]
        assert "&lt;script&gt;" in section["content"]
