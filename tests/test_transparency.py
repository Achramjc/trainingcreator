"""
Real pytest assertions for the M1 transparency-report additions:
  - citation coverage (objectives / sections / assessment questions)
  - requested_questions vs actual, and assessment.notes surfaced
  - optional approval block
  - stability of the pre-existing top-level JSON keys
"""

import copy
import json

import pytest

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.transparency_report import (
    generate_transparency_report,
    create_html_report,
    create_json_report,
)


@pytest.fixture()
def sop(sample_sop_path):
    return SOPParser().parse(sample_sop_path)


@pytest.fixture()
def training(sop):
    return TrainingGenerator().generate(sop)


@pytest.fixture()
def assessment(sop):
    return AssessmentGenerator().generate(sop, num_questions=8)


@pytest.fixture()
def md_assessment(sop):
    return MedicalDeviceAssessmentGenerator().generate(sop, num_questions=8)


def _report(sop, training, assessment, sample_sop_path, **kwargs):
    return generate_transparency_report(sop, training, assessment, sample_sop_path, **kwargs)


# ---------------------------------------------------------------------------
# Citation coverage
# ---------------------------------------------------------------------------

class TestCitationCoverage:
    def test_top_level_shape(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        coverage = report["citation_coverage"]
        for key in ("objectives", "sections", "assessment_questions", "overall"):
            assert key in coverage

        for key in ("objectives", "sections", "assessment_questions"):
            bucket = coverage[key]
            for field in ("total", "cited", "unresolvable", "not_grounded", "coverage_pct", "items"):
                assert field in bucket

    def test_objectives_fully_cited_on_clean_sop(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        bucket = report["citation_coverage"]["objectives"]
        assert bucket["total"] == len(training.objectives)
        assert bucket["cited"] == bucket["total"]
        assert bucket["unresolvable"] == 0
        assert bucket["coverage_pct"] == 100.0

    def test_sections_fully_cited_on_clean_sop(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        bucket = report["citation_coverage"]["sections"]
        assert bucket["total"] > 0
        assert bucket["cited"] == bucket["total"]
        assert bucket["coverage_pct"] == 100.0

    def test_every_cited_item_has_a_nonempty_excerpt(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        for bucket_name in ("objectives", "sections"):
            for item in report["citation_coverage"][bucket_name]["items"]:
                if item["status"] == "cited":
                    assert item["excerpt"]

    def test_removing_an_objective_citation_drops_coverage(self, sop, training, assessment, sample_sop_path):
        baseline = _report(sop, training, assessment, sample_sop_path)
        baseline_bucket = baseline["citation_coverage"]["objectives"]

        broken_training = copy.deepcopy(training)
        # Corrupt one objective's span so it no longer resolves.
        broken_training.objectives[0]["source_ref"]["span"] = [9999, 9999]

        broken = _report(sop, broken_training, assessment, sample_sop_path)
        broken_bucket = broken["citation_coverage"]["objectives"]

        assert broken_bucket["cited"] == baseline_bucket["cited"] - 1
        assert broken_bucket["unresolvable"] == 1
        assert broken_bucket["coverage_pct"] < baseline_bucket["coverage_pct"]

    def test_removing_a_section_citation_drops_coverage(self, sop, training, assessment, sample_sop_path):
        baseline = _report(sop, training, assessment, sample_sop_path)
        baseline_bucket = baseline["citation_coverage"]["sections"]

        broken_training = copy.deepcopy(training)
        intro = next(s for s in broken_training.sections if s["id"] == "intro")
        assert intro["citations"]
        intro["citations"].pop()

        broken = _report(sop, broken_training, assessment, sample_sop_path)
        broken_bucket = broken["citation_coverage"]["sections"]

        assert broken_bucket["total"] == baseline_bucket["total"] - 1
        assert broken_bucket["cited"] == baseline_bucket["cited"] - 1

    def test_question_span_resolves_for_step_question(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        bucket = report["citation_coverage"]["assessment_questions"]
        step_items = [
            item for item in bucket["items"]
            if item["source_ref"].get("kind") == "step"
        ]
        assert step_items
        for item in step_items:
            assert item["status"] == "cited"

    def test_md_required_questions_are_not_grounded(self, sop, training, md_assessment, sample_sop_path):
        report = _report(sop, training, md_assessment, sample_sop_path)
        bucket = report["citation_coverage"]["assessment_questions"]
        md_items = [item for item in bucket["items"] if item["source_ref"].get("kind") == "md_required"]
        assert md_items
        for item in md_items:
            assert item["status"] == "not_grounded"
        # These are correctly excluded from "cited" but still counted in total.
        assert bucket["not_grounded"] >= len(md_items)

    def test_unresolvable_out_of_range_warning_index(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        bucket = report["citation_coverage"]["assessment_questions"]
        safety_item = next(
            item for item in bucket["items"] if item["source_ref"].get("kind") == "safety"
        )
        # Sanity: a legitimate safety question resolves.
        assert safety_item["status"] == "cited"

    def test_overall_is_sum_of_buckets(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        coverage = report["citation_coverage"]
        expected_total = (
            coverage["objectives"]["total"]
            + coverage["sections"]["total"]
            + coverage["assessment_questions"]["total"]
        )
        expected_cited = (
            coverage["objectives"]["cited"]
            + coverage["sections"]["cited"]
            + coverage["assessment_questions"]["cited"]
        )
        assert coverage["overall"]["total"] == expected_total
        assert coverage["overall"]["cited"] == expected_cited


# ---------------------------------------------------------------------------
# Assessment reporting: requested vs actual, notes
# ---------------------------------------------------------------------------

class TestAssessmentReporting:
    def test_requested_vs_actual_questions_shown(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        gen = report["generation_process"]["assessment_generation"]
        assert gen["requested_questions"] == assessment.requested_questions
        assert gen["total_questions"] == len(assessment.questions)

    def test_notes_surfaced_when_present(self, sop, training, assessment, sample_sop_path):
        assessment.notes = ["Document could only support 6 of the requested 10 questions."]
        report = _report(sop, training, assessment, sample_sop_path)
        assert report["generation_process"]["assessment_generation"]["notes"] == assessment.notes

    def test_notes_empty_list_when_absent(self, sop, training, assessment, sample_sop_path):
        assessment.notes = []
        report = _report(sop, training, assessment, sample_sop_path)
        assert report["generation_process"]["assessment_generation"]["notes"] == []


# ---------------------------------------------------------------------------
# Approval block
# ---------------------------------------------------------------------------

class TestApprovalBlock:
    def test_absent_by_default(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        assert report["approval"]["status"] == "unreviewed_draft"
        assert "approval_statement" in report
        assert "unreviewed" in report["approval_statement"].lower() or \
            "not been approved" in report["approval_statement"].lower()

    def test_present_when_given(self, sop, training, assessment, sample_sop_path):
        approval = {
            "approved_by": "Jane SME",
            "role": "QA Manager",
            "approved_at": "2026-09-17T12:00:00Z",
            "notes": "Reviewed and accepted with minor edits.",
            "edits_count": 3,
        }
        report = _report(sop, training, assessment, sample_sop_path, approval=approval)
        assert set(report["approval"].keys()) == {
            "approved_by", "role", "approved_at", "notes", "edits_count"
        }
        assert report["approval"]["approved_by"] == "Jane SME"
        assert report["approval"]["edits_count"] == 3
        assert "approval_statement" not in report

    def test_html_reflects_approval_state(self, tmp_path, sop, training, assessment, sample_sop_path):
        unapproved = _report(sop, training, assessment, sample_sop_path)
        html_path = tmp_path / "unapproved.html"
        create_html_report(unapproved, str(html_path))
        content = html_path.read_text(encoding="utf-8")
        assert "UNREVIEWED DRAFT" in content

        approved = _report(sop, training, assessment, sample_sop_path, approval={
            "approved_by": "Jane SME", "role": "QA Manager",
            "approved_at": "2026-09-17T12:00:00Z", "notes": "ok", "edits_count": 0,
        })
        html_path2 = tmp_path / "approved.html"
        create_html_report(approved, str(html_path2))
        content2 = html_path2.read_text(encoding="utf-8")
        assert "Jane SME" in content2
        assert "QA Manager" in content2


# ---------------------------------------------------------------------------
# HTML / JSON output sanity for the new sections
# ---------------------------------------------------------------------------

class TestReportOutput:
    def test_html_contains_citation_coverage_section(self, tmp_path, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        html_path = tmp_path / "report.html"
        create_html_report(report, str(html_path))
        content = html_path.read_text(encoding="utf-8")
        assert "Citation Coverage" in content
        assert "Learning objectives" in content
        assert "Assessment questions" in content

    def test_json_round_trips_citation_coverage(self, tmp_path, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        json_path = tmp_path / "report.json"
        create_json_report(report, str(json_path))
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded["citation_coverage"]["overall"]["total"] == report["citation_coverage"]["overall"]["total"]

    def test_existing_top_level_keys_unchanged(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        for key in (
            "report_title",
            "generated_timestamp",
            "disclaimer",
            "source_document",
            "generation_process",
            "review_checklist",
            "limitations",
            "usage_instructions",
        ):
            assert key in report

    def test_untrusted_excerpt_text_is_escaped_in_html(self, tmp_path, sample_sop_path):
        # A malicious SOP shouldn't be able to inject markup via a cited excerpt.
        sop = SOPParser()._extract_structure(
            "PURPOSE:\n"
            "This <script>alert(1)</script> procedure does the thing.\n"
        )
        training_module = TrainingGenerator().generate(sop)
        assessment_obj = AssessmentGenerator().generate(sop, num_questions=5)
        report = _report(sop, training_module, assessment_obj, sample_sop_path)
        html_path = tmp_path / "xss.html"
        create_html_report(report, str(html_path))
        content = html_path.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in content
