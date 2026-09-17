"""
End-to-end pipeline tests: parse -> generate -> assess -> export.

These assert on STABLE contracts only (types, non-empty collections, files
existing, ZIP contents) since src/parser.py, src/generator.py,
src/assessments.py and src/scorm_exporter.py are being actively rewritten in
parallel. The one exception, per the sample SOP's fixed content, is that
examples/sample_sop.txt always yields 9 procedures and 4 safety warnings.
"""

import json
import zipfile

from src.parser import SOPParser, SOPContent
from src.generator import TrainingGenerator, TrainingModule
from src.assessments import AssessmentGenerator, Assessment
from src.scorm_exporter import SCORMExporter


def test_parse_sample_sop(sample_sop_path):
    parser = SOPParser()
    sop = parser.parse(sample_sop_path)

    assert isinstance(sop, SOPContent)
    assert sop.title
    assert len(sop.procedures) == 9
    assert len(sop.safety_warnings) == 4


def test_generate_training_module(sample_sop_path):
    sop = SOPParser().parse(sample_sop_path)
    training = TrainingGenerator().generate(sop)

    assert isinstance(training, TrainingModule)
    assert training.title
    assert len(training.sections) > 0
    assert training.estimated_duration > 0


def test_generate_assessment(sample_sop_path):
    sop = SOPParser().parse(sample_sop_path)
    assessment = AssessmentGenerator().generate(sop, num_questions=5)

    assert isinstance(assessment, Assessment)
    assert len(assessment.questions) > 0
    assert assessment.passing_score == 70

    for question in assessment.questions:
        assert question.id
        assert question.text
        assert isinstance(question.options, list)


def test_scorm_export_creates_zip_with_manifest(tmp_path, sample_sop_path):
    sop = SOPParser().parse(sample_sop_path)
    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=5)

    exporter = SCORMExporter(scorm_version="1.2")
    package_path = exporter.create_package(
        training, assessment, str(tmp_path), "emergency_shutdown_training"
    )

    assert package_path.endswith(".zip")
    assert (tmp_path / "emergency_shutdown_training.zip").exists()

    with zipfile.ZipFile(package_path) as zf:
        names = zf.namelist()
        assert "imsmanifest.xml" in names
        assert any(name == "assessment.html" for name in names)


def test_scorm_export_scorm_2004(tmp_path, sample_sop_path):
    sop = SOPParser().parse(sample_sop_path)
    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=3)

    exporter = SCORMExporter(scorm_version="2004")
    package_path = exporter.create_package(
        training, assessment, str(tmp_path), "sop_2004_pkg"
    )

    with zipfile.ZipFile(package_path) as zf:
        assert "imsmanifest.xml" in zf.namelist()


def test_json_export_round_trips(tmp_path, sample_sop_path):
    sop = SOPParser().parse(sample_sop_path)
    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=5)

    json_data = {
        "sop_content": sop.to_dict(),
        "training_module": training.to_dict(),
        "assessment": assessment.to_dict(),
    }

    json_path = tmp_path / "training.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["sop_content"]["title"] == sop.title
    assert len(loaded["assessment"]["questions"]) == len(assessment.questions)
