"""
Real pytest assertions for TrainingGenerator, covering the M0 fixes:
  - learning objectives read as objectives (use step titles, no "..." truncation)
  - procedures HTML renders the full step body and HTML-escapes document text
"""

import os

import pytest

from src.parser import SOPParser
from src.generator import TrainingGenerator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_SOP = os.path.join(REPO_ROOT, "examples", "sample_sop.txt")


@pytest.fixture(scope="module")
def sample_sop():
    return SOPParser().parse(SAMPLE_SOP)


@pytest.fixture(scope="module")
def training_module(sample_sop):
    return TrainingGenerator().generate(sample_sop)


class TestLearningObjectives:
    def test_objectives_use_titles(self, training_module):
        joined = " | ".join(training_module.learning_objectives)
        assert "Perform Step 2: Activate Emergency Stop" in joined

    def test_no_ellipsis_truncation(self, training_module):
        for obj in training_module.learning_objectives:
            assert "..." not in obj

    def test_capped_at_eight(self, training_module):
        assert len(training_module.learning_objectives) <= 8

    def test_safety_objective_present_when_warnings_exist(self, sample_sop, training_module):
        assert sample_sop.safety_warnings  # sanity: sample has warnings
        assert any("safety warnings and precautions" in obj for obj in training_module.learning_objectives)


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
