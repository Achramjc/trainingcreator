"""
Medical device compliance behaviours (ISO 13485 / 21 CFR 820), converted
from the old test_medical_device.py print-script into real assertions.
"""

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import MedicalDeviceAssessmentGenerator
from src.transparency_report import generate_transparency_report, create_html_report, create_json_report


def _build_assessment(sample_sop_path, num_questions=5):
    sop = SOPParser().parse(sample_sop_path)
    training = TrainingGenerator().generate(sop)
    assessment = MedicalDeviceAssessmentGenerator().generate(sop, num_questions=num_questions)
    return sop, training, assessment


def test_required_medical_device_questions_present(sample_sop_path):
    _, _, assessment = _build_assessment(sample_sop_path)
    question_ids = {q.id for q in assessment.questions}

    assert "md_req_1" in question_ids
    assert "md_req_2" in question_ids


def test_minimum_passing_score_enforced(sample_sop_path):
    # Even if a lower passing_score is requested, medical device training
    # must not go below the FDA-expected floor of 80%.
    sop = SOPParser().parse(sample_sop_path)
    assessment = MedicalDeviceAssessmentGenerator().generate(sop, num_questions=5, passing_score=50)

    assert assessment.passing_score >= 80


def test_transparency_report_has_expected_top_level_keys(sample_sop_path):
    sop, training, assessment = _build_assessment(sample_sop_path)
    report = generate_transparency_report(sop, training, assessment, sample_sop_path)

    for key in (
        "report_title",
        "disclaimer",
        "source_document",
        "generation_process",
        "review_checklist",
        "limitations",
        "usage_instructions",
    ):
        assert key in report


def test_transparency_report_html_and_json_written(tmp_path, sample_sop_path):
    sop, training, assessment = _build_assessment(sample_sop_path)
    report = generate_transparency_report(sop, training, assessment, sample_sop_path)

    html_path = tmp_path / "transparency_report.html"
    json_path = tmp_path / "transparency_report.json"

    create_html_report(report, str(html_path))
    create_json_report(report, str(json_path))

    assert html_path.exists() and html_path.stat().st_size > 0
    assert json_path.exists() and json_path.stat().st_size > 0

    import json as json_module
    reloaded = json_module.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded["report_title"] == report["report_title"]
