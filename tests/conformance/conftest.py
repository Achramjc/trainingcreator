"""Fixtures for the SCORM conformance harness.

Every check runs over the full matrix the exporter can produce: both SCORM
versions, both assessment generators, both shipped SOP fixtures.  Packages are
built once per session and reused, because building them is the slow part and
none of the checks mutate a package.
"""

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.assessments import (  # noqa: E402
    AssessmentGenerator, MedicalDeviceAssessmentGenerator)
from src.generator import TrainingGenerator  # noqa: E402
from src.parser import SOPParser  # noqa: E402
from src.scorm_exporter import SCORMExporter  # noqa: E402

FIXTURES = {
    "sample": REPO_ROOT / "examples" / "sample_sop.txt",
    "numbered": REPO_ROOT / "examples" / "sample_sop_numbered.txt",
}
GENERATORS = {
    "standard": AssessmentGenerator,
    "medical": MedicalDeviceAssessmentGenerator,
}
VERSIONS = ("1.2", "2004")

#: The whole matrix, as ids pytest can print.
MATRIX = [(version, generator, fixture)
          for version in VERSIONS
          for generator in sorted(GENERATORS)
          for fixture in sorted(FIXTURES)]
MATRIX_IDS = ["{0}-{1}-{2}".format(*combo) for combo in MATRIX]


class Package:
    """A built package plus the Python model it was built from."""

    def __init__(self, version, generator, fixture, directory, zip_path,
                 training, assessment):
        self.version = version
        self.generator = generator
        self.fixture = fixture
        self.dir = directory
        self.zip_path = zip_path
        self.training = training
        self.assessment = assessment

    def __repr__(self):
        return "Package({0}, {1}, {2})".format(
            self.version, self.generator, self.fixture)

    # -- the scores the page must report ----------------------------------
    def _percentage(self, earned):
        total = sum(question.points for question in self.assessment.questions)
        if not total:
            return 0
        # Math.round in JavaScript rounds halves away from zero; Python's
        # round() rounds them to even.  The page's number is the contract.
        return int(math.floor((earned / total) * 100 + 0.5))

    @property
    def naive_percentage(self):
        """What a learner who always clicks the first option scores."""
        earned = sum(question.points for question in self.assessment.questions
                     if question.correct_answer == 0)
        return self._percentage(earned)

    @property
    def perfect_percentage(self):
        return self._percentage(
            sum(question.points for question in self.assessment.questions))

    @property
    def passing_score(self):
        return self.assessment.passing_score


def _build(version, generator, fixture, output_root):
    sop = SOPParser().parse(str(FIXTURES[fixture]))
    training = TrainingGenerator().generate(sop)
    assessment = GENERATORS[generator]().generate(sop, num_questions=8)
    name = "pkg_{0}_{1}_{2}".format(version.replace(".", ""), generator, fixture)
    zip_path = SCORMExporter(scorm_version=version).create_package(
        training, assessment, str(output_root), name)
    return Package(version, generator, fixture, Path(output_root) / name,
                   Path(zip_path), training, assessment)


@pytest.fixture(scope="session")
def package_factory(tmp_path_factory):
    root = tmp_path_factory.mktemp("conformance")
    cache = {}

    def factory(version, generator="standard", fixture="sample"):
        key = (version, generator, fixture)
        if key not in cache:
            cache[key] = _build(version, generator, fixture, root)
        return cache[key]

    return factory


@pytest.fixture
def chromium():
    """A headless Chromium, or a clean skip when none is installed.

    Mirrors ``tests/test_scorm.py::_launch_chromium``: never installs
    anything, so the plain CI job stays green without a browser.

    Deliberately function-scoped.  ``sync_playwright()`` owns an asyncio loop
    for as long as it is open, and a second ``sync_playwright()`` on the same
    thread while one is open raises - which is exactly what a session-scoped
    browser here would do to ``tests/test_scorm.py``'s own browser test.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright for Python is not installed")

    from tests.test_scorm import _launch_chromium

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            yield browser
        finally:
            browser.close()
