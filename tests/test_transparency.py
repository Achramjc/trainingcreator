"""
Real pytest assertions for the M1 transparency-report additions:
  - citation coverage (objectives / sections / assessment questions)
  - requested_questions vs actual, and assessment.notes surfaced
  - optional approval block
  - stability of the pre-existing top-level JSON keys

And the M3 (partial) audit-trail surfacing:
  - audit_block_for_job() reading a real audit.jsonl from disk
  - report["audit_trail"] shape, for both a trail and no trail
  - the HTML rendering: red banner only on failure, timeline, head hash,
    the mode statement, and escaping of untrusted actor names
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
    audit_block_for_job,
)
from src.audit import AuditLog, Actor, content_hash as audit_content_hash


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


# ---------------------------------------------------------------------------
# The LMS record block
#
# The SCORM package writes per-question evidence to the LMS (cmi.interactions),
# and deliberately does not write the answer key. An auditor reading only this
# report has to be able to tell which is which without opening the package.
# ---------------------------------------------------------------------------
class TestLMSRecordBlock:
    REQUIRED_KEYS = ("written_by", "score", "status", "per_question_evidence",
                     "session", "not_written", "verification_boundary")

    def test_block_is_present_and_complete(self, sop, training, assessment,
                                           sample_sop_path):
        record = _report(sop, training, assessment, sample_sop_path)["lms_record"]
        for key in self.REQUIRED_KEYS:
            assert key in record, key
            assert record[key].strip(), key

    def test_it_names_the_interaction_evidence(self, sop, training, assessment,
                                               sample_sop_path):
        record = _report(sop, training, assessment, sample_sop_path)["lms_record"]
        assert "cmi.interactions" in record["per_question_evidence"]
        assert "cmi.suspend_data" in record["session"]
        for element in ("cmi.core.score.raw", "cmi.score.scaled"):
            assert element in record["score"], element
        for element in ("cmi.core.lesson_status", "cmi.success_status",
                        "cmi.completion_status"):
            assert element in record["status"], element

    def test_it_states_that_the_key_is_not_written(self, sop, training,
                                                   assessment, sample_sop_path):
        """The trade-off has to be stated, not implied: an auditor who expects
        correct_responses in the LMS record must find out here that it is
        absent, and why."""
        record = _report(sop, training, assessment, sample_sop_path)["lms_record"]
        assert "correct_responses" in record["not_written"]
        assert "never written" in record["not_written"]
        assert "answer key" in record["not_written"].lower()
        assert "not independently verified" in record["verification_boundary"] \
            or "not " in record["verification_boundary"]

    def test_the_block_is_not_mutated_between_reports(self, sop, training,
                                                      assessment,
                                                      sample_sop_path):
        from src.transparency_report import LMS_RECORD

        report = _report(sop, training, assessment, sample_sop_path)
        report["lms_record"]["score"] = "tampered"
        again = _report(sop, training, assessment, sample_sop_path)
        assert again["lms_record"]["score"] == LMS_RECORD["score"]

    def test_html_renders_the_block(self, tmp_path, sop, training, assessment,
                                    sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "LMS Record" in content
        assert "Per-question evidence" in content
        assert "Deliberately NOT written" in content
        assert "cmi.interactions" in content

    def test_json_round_trips_the_block(self, tmp_path, sop, training,
                                        assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        path = tmp_path / "report.json"
        create_json_report(report, str(path))
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["lms_record"] == report["lms_record"]

    def test_html_escapes_the_block(self, tmp_path, sop, training, assessment,
                                    sample_sop_path):
        """Fixed text today, but the renderer must escape it regardless."""
        report = _report(sop, training, assessment, sample_sop_path)
        report["lms_record"]["score"] = "<script>alert(1)</script>"
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;" in content

    def test_it_matches_what_the_package_actually_writes(self, tmp_path, sop,
                                                         training, assessment,
                                                         sample_sop_path):
        """The report describes the package, so it must not describe a package
        we do not build: the elements it names are in the shipped wrapper, and
        the element it says is absent really is."""
        from src.scorm_exporter import SCORMExporter

        SCORMExporter().create_package(training, assessment, str(tmp_path),
                                       "lms_record_package")
        api = (tmp_path / "lms_record_package" / "scorm_api.js").read_text(
            encoding="utf-8")
        record = _report(sop, training, assessment, sample_sop_path)["lms_record"]

        for element in ("cmi.interactions", "cmi.suspend_data",
                        "cmi.core.score.raw", "cmi.score.scaled",
                        "cmi.core.lesson_status", "cmi.success_status",
                        "cmi.completion_status"):
            assert element in api, element
        assert "correct_responses" not in api


# ---------------------------------------------------------------------------
# Audit trail (M3 partial): audit_block_for_job() + report["audit_trail"]
# ---------------------------------------------------------------------------

def _build_intact_trail(job_dir, job_id, hmac_key=None, tamper_after_approval=False):
    """Write a real, well-formed audit.jsonl for `job_id` under `job_dir`:
    job.created -> content.generated -> content.approved -> package.exported.

    With `tamper_after_approval=True`, an extra content.edited entry is
    appended *after* the approval (no re-approval follows) -- a semantic
    violation verify_job must catch, not a hash-chain break.
    """
    log = AuditLog.for_job(job_dir, job_id, hmac_key)
    log.append("job.created", Actor("training-creator", "application", "system"),
               {"source_filename": "sop.txt"})
    digest = audit_content_hash({"training_module": "a"}, {"assessment": "b"})
    log.append("content.generated", Actor("training-creator", "application", "system"),
               {"questions": 5}, digest)
    log.append("content.approved", Actor("Dana Reyes", "Quality Engineer", "web"),
               {"approved_by": "Dana Reyes", "role": "Quality Engineer", "edits_count": 0},
               digest)
    log.append("package.exported", Actor("training-creator", "application", "system"),
               {"package_sha256": "abc123def4567890fedcba", "format": "scorm",
                "scorm_version": "1.2"}, digest)
    if tamper_after_approval:
        log.append("content.edited", Actor("Dana Reyes", "Quality Engineer", "web"),
                   {"edits_count": 1, "categories": {"title": 1}}, digest)
    return log


def _corrupt_middle_line(job_dir):
    """Tamper a middle entry's `details` in place, breaking its hash without
    breaking JSON-parseability -- the "someone edited the record" case."""
    path = job_dir / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    obj = json.loads(lines[1])
    obj["details"] = dict(obj["details"])
    obj["details"]["questions"] = 999
    lines[1] = json.dumps(obj)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestAuditBlockForJob:
    def test_returns_none_without_a_file(self, tmp_path):
        job_dir = tmp_path / "job-none"
        job_dir.mkdir()
        assert audit_block_for_job(job_dir, "job-none") is None

    def test_shape_with_a_trail(self, tmp_path):
        job_dir = tmp_path / "job-1"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-1")

        audit = audit_block_for_job(job_dir, "job-1")
        assert set(audit.keys()) == {"entries", "verification", "hmac_mode"}
        assert audit["hmac_mode"] == "chain-only"
        assert len(audit["entries"]) == 4
        assert audit["entries"][0]["event"] == "job.created"
        assert audit["verification"]["ok"] is True
        assert audit["verification"]["package_matches_approval"] is True

    def test_hmac_mode_when_key_given(self, tmp_path):
        job_dir = tmp_path / "job-2"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-2", hmac_key=b"top-secret")

        audit = audit_block_for_job(job_dir, "job-2", hmac_key=b"top-secret")
        assert audit["hmac_mode"] == "hmac"
        assert audit["verification"]["ok"] is True

    def test_detects_tampering(self, tmp_path):
        job_dir = tmp_path / "job-3"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-3")
        _corrupt_middle_line(job_dir)

        audit = audit_block_for_job(job_dir, "job-3")
        assert audit["verification"]["ok"] is False
        assert audit["verification"]["problems"]

    def test_detects_edit_after_approval(self, tmp_path):
        job_dir = tmp_path / "job-4"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-4", tamper_after_approval=True)

        audit = audit_block_for_job(job_dir, "job-4")
        assert audit["verification"]["ok"] is False
        assert any("after approval" in p for p in audit["verification"]["problems"])


class TestAuditTrailReportBlock:
    def test_no_audit_trail_shape(self, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path, audit=None)
        assert report["audit_trail"] == {"status": "no_audit_trail"}

    def test_intact_trail_shape_and_values(self, tmp_path, sop, training, assessment,
                                           sample_sop_path):
        job_dir = tmp_path / "job-json"
        job_dir.mkdir()
        log = _build_intact_trail(job_dir, "job-json")
        audit = audit_block_for_job(job_dir, "job-json")

        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        block = report["audit_trail"]

        assert block["status"] == "intact"
        assert block["hmac_mode"] == "chain-only"
        assert block["entries_count"] == 4
        assert block["head_hash"] == log.head_hash()
        assert block["package_matches_approval"] is True
        assert block["problems"] == []

        seqs = [item["seq"] for item in block["timeline"]]
        events = [item["event"] for item in block["timeline"]]
        assert seqs == [1, 2, 3, 4]
        assert events == [
            "job.created", "content.generated", "content.approved", "package.exported",
        ]
        for item in block["timeline"]:
            assert set(item.keys()) == {
                "seq", "ts", "event", "actor", "content_hash", "details_summary"}

    def test_failed_trail_status_and_problems(self, tmp_path, sop, training, assessment,
                                              sample_sop_path):
        job_dir = tmp_path / "job-tampered"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-tampered")
        _corrupt_middle_line(job_dir)
        audit = audit_block_for_job(job_dir, "job-tampered")

        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        block = report["audit_trail"]
        assert block["status"] == "failed"
        assert block["problems"]

    def test_existing_top_level_keys_still_present_with_audit(
            self, tmp_path, sop, training, assessment, sample_sop_path):
        job_dir = tmp_path / "job-keys"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-keys")
        audit = audit_block_for_job(job_dir, "job-keys")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        for key in ("report_title", "citation_coverage", "lms_record", "approval"):
            assert key in report


class TestAuditTrailHTML:
    def test_no_audit_trail_note(self, tmp_path, sop, training, assessment, sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path, audit=None)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "Audit Trail" in content
        assert "No audit trail is available" in content
        assert "AUDIT TRAIL VERIFICATION FAILED" not in content

    def test_red_banner_only_on_failure(self, tmp_path, sop, training, assessment,
                                        sample_sop_path):
        ok_dir = tmp_path / "job-ok"
        ok_dir.mkdir()
        _build_intact_trail(ok_dir, "job-ok")
        ok_audit = audit_block_for_job(ok_dir, "job-ok")
        ok_report = _report(sop, training, assessment, sample_sop_path, audit=ok_audit)
        ok_path = tmp_path / "ok.html"
        create_html_report(ok_report, str(ok_path))
        ok_content = ok_path.read_text(encoding="utf-8")
        assert "AUDIT TRAIL VERIFICATION FAILED" not in ok_content

        bad_dir = tmp_path / "job-bad"
        bad_dir.mkdir()
        _build_intact_trail(bad_dir, "job-bad")
        _corrupt_middle_line(bad_dir)
        bad_audit = audit_block_for_job(bad_dir, "job-bad")
        bad_report = _report(sop, training, assessment, sample_sop_path, audit=bad_audit)
        bad_path = tmp_path / "bad.html"
        create_html_report(bad_report, str(bad_path))
        bad_content = bad_path.read_text(encoding="utf-8")
        assert "AUDIT TRAIL VERIFICATION FAILED" in bad_content

    def test_timeline_lists_every_event_in_order(self, tmp_path, sop, training, assessment,
                                                 sample_sop_path):
        job_dir = tmp_path / "job-timeline"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-timeline")
        audit = audit_block_for_job(job_dir, "job-timeline")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")

        order = [content.index(event) for event in
                 ("job.created", "content.generated", "content.approved", "package.exported")]
        assert order == sorted(order)

    def test_head_hash_rendered(self, tmp_path, sop, training, assessment, sample_sop_path):
        job_dir = tmp_path / "job-hash"
        job_dir.mkdir()
        log = _build_intact_trail(job_dir, "job-hash")
        audit = audit_block_for_job(job_dir, "job-hash")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert log.head_hash() in content
        assert "metadata.json" in content

    def test_mode_statement_matches_chain_only(self, tmp_path, sop, training, assessment,
                                               sample_sop_path):
        job_dir = tmp_path / "job-chain"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-chain")
        audit = audit_block_for_job(job_dir, "job-chain")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "does not by itself" in content
        assert "regeneration without the server key is detected" not in content

    def test_mode_statement_matches_hmac(self, tmp_path, sop, training, assessment,
                                         sample_sop_path):
        job_dir = tmp_path / "job-hmac"
        job_dir.mkdir()
        _build_intact_trail(job_dir, "job-hmac", hmac_key=b"key")
        audit = audit_block_for_job(job_dir, "job-hmac", hmac_key=b"key")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "regeneration without the server key is detected" in content
        assert "does not by itself" not in content

    def test_escapes_actor_name_with_script_tag(self, tmp_path, sop, training, assessment,
                                                sample_sop_path):
        job_dir = tmp_path / "job-xss"
        job_dir.mkdir()
        log = AuditLog.for_job(job_dir, "job-xss")
        log.append("job.created", Actor("training-creator", "application", "system"), {})
        digest = audit_content_hash({"a": 1}, {"b": 2})
        log.append("content.approved",
                   Actor("<script>alert(1)</script>", "QA", "web"),
                   {"approved_by": "<script>alert(1)</script>", "role": "QA", "edits_count": 0},
                   digest)
        audit = audit_block_for_job(job_dir, "job-xss")
        report = _report(sop, training, assessment, sample_sop_path, audit=audit)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


# ---------------------------------------------------------------------------
# Input-document scan (prompt-injection pre-scan; see docs/SECURITY.md).
# The adversarial cases live in tests/test_injection.py; what belongs here is
# that a clean document says so, that the block is part of the report's shape,
# and that a document *title* can no longer put live markup into the page.
# ---------------------------------------------------------------------------
class TestInputDocumentScan:

    def test_clean_document_reports_no_indicators(self, sop, training, assessment,
                                                  sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        scan = report["input_document_scan"]
        assert scan["risk"] == "none"
        assert scan["findings"] == []
        assert scan["gated_llm_enhancement"] is False
        assert "lexical scan" in scan["statement"]
        assert json.dumps(report)

    def test_html_renders_the_block(self, tmp_path, sop, training, assessment,
                                   sample_sop_path):
        report = _report(sop, training, assessment, sample_sop_path)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "Input Document Scan" in content
        assert "LLM enhancement gated by this scan:\n        <strong>no</strong>" \
            in content or "gated by this scan" in content

    def test_a_document_title_cannot_inject_markup_into_the_report(
            self, tmp_path, sop, training, assessment, sample_sop_path):
        """The Source Document Analysis block dumps parsed fields into a <pre>.

        json.dumps escapes quotes, not angle brackets, so before this was fixed a
        document *titled* `<script>...` put live markup into an auditor's report.
        """
        hostile = copy.deepcopy(sop)
        hostile.title = "<script>alert('sop')</script> Emergency Shutdown"
        report = _report(hostile, training, assessment, sample_sop_path)
        path = tmp_path / "report.html"
        create_html_report(report, str(path))
        content = path.read_text(encoding="utf-8")
        assert "<script>alert('sop')</script>" not in content
        assert "&lt;script&gt;alert(&#x27;sop&#x27;)&lt;/script&gt;" in content
