"""Round-trip tests for src/serialization.py.

``sop_from_dict`` / ``module_from_dict`` / ``assessment_from_dict`` rebuild the
real model objects from ``to_dict()`` output, without modifying those classes.
The property that matters: ``obj.to_dict() == from_dict(obj.to_dict()).to_dict()``,
and for the assessment specifically, that the *rebuilt* object still produces
the same learner-facing payload and the same rendered assessment.html bytes.
"""

import re
from pathlib import Path

import pytest

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.scorm_exporter import SCORMExporter
from src.serialization import sop_from_dict, module_from_dict, assessment_from_dict

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = [
    REPO_ROOT / "examples" / "sample_sop.txt",
    REPO_ROOT / "examples" / "sample_sop_numbered.txt",
]


def _mask_timestamp(html_text):
    return re.sub(r"Generated: [^|]+\|", "Generated: |", html_text)


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_sop_round_trip(fixture_path):
    sop = SOPParser().parse(str(fixture_path))
    rebuilt = sop_from_dict(sop.to_dict())
    assert rebuilt.to_dict() == sop.to_dict()


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_module_round_trip(fixture_path):
    sop = SOPParser().parse(str(fixture_path))
    module = TrainingGenerator().generate(sop)
    rebuilt = module_from_dict(module.to_dict())
    assert rebuilt.to_dict() == module.to_dict()


@pytest.mark.parametrize("fixture_path", FIXTURES)
@pytest.mark.parametrize("generator_cls", [AssessmentGenerator, MedicalDeviceAssessmentGenerator])
def test_assessment_round_trip(fixture_path, generator_cls):
    sop = SOPParser().parse(str(fixture_path))
    assessment = generator_cls().generate(sop, num_questions=8)
    rebuilt = assessment_from_dict(assessment.to_dict())
    assert rebuilt.to_dict() == assessment.to_dict()


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_assessment_round_trip_preserves_learner_payload(fixture_path):
    sop = SOPParser().parse(str(fixture_path))
    assessment = MedicalDeviceAssessmentGenerator().generate(sop, num_questions=8)
    rebuilt = assessment_from_dict(assessment.to_dict())
    assert rebuilt.to_learner_dict() == assessment.to_learner_dict()


def test_assessment_round_trip_produces_same_package_bytes(tmp_path):
    sop = SOPParser().parse(str(FIXTURES[0]))
    training = TrainingGenerator().generate(sop)
    assessment = MedicalDeviceAssessmentGenerator().generate(sop, num_questions=8)
    rebuilt_assessment = assessment_from_dict(assessment.to_dict())
    rebuilt_training = module_from_dict(training.to_dict())

    SCORMExporter().create_package(training, assessment, str(tmp_path / "orig"), "pkg")
    SCORMExporter().create_package(
        rebuilt_training, rebuilt_assessment, str(tmp_path / "rebuilt"), "pkg")

    orig_html = (tmp_path / "orig" / "pkg" / "assessment.html").read_text(encoding="utf-8")
    rebuilt_html = (tmp_path / "rebuilt" / "pkg" / "assessment.html").read_text(encoding="utf-8")

    assert _mask_timestamp(orig_html) == _mask_timestamp(rebuilt_html)


def test_sop_from_dict_tolerates_missing_keys():
    sop = sop_from_dict({"title": "Only Title"})
    assert sop.title == "Only Title"
    assert sop.procedures == []
    assert sop.definitions == {}
    assert sop.raw_content == ""


def test_module_from_dict_tolerates_missing_keys():
    module = module_from_dict({"title": "Only Title"})
    assert module.title == "Only Title"
    assert module.learning_objectives == []
    assert module.sections == []
    assert module.estimated_duration == 0


def test_assessment_from_dict_tolerates_missing_keys():
    assessment = assessment_from_dict({"title": "Only Title"})
    assert assessment.title == "Only Title"
    assert assessment.questions == []
    assert assessment.passing_score == 70


def test_assessment_from_dict_empty_input():
    assessment = assessment_from_dict(None)
    assert assessment.to_dict() == assessment.__class__().to_dict()


def test_sop_from_dict_empty_input():
    sop = sop_from_dict(None)
    assert sop.to_dict() == sop.__class__().to_dict()


def test_module_from_dict_empty_input():
    module = module_from_dict(None)
    assert module.to_dict() == module.__class__().to_dict()
