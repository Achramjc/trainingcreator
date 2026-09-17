"""
Tests for the SCORM package: what reaches the learner, and what must not.

The headline property is Defect 2 from GOAL.md - the answer key used to be
serialised straight into client-side JavaScript.  These tests assert that the
package now carries only a salted hash, re-derive that hash with an independent
implementation, and (when node is available) check that the JavaScript shipped
inside assessment.html agrees with Python byte for byte.
"""

import hashlib
import html as html_module
import json
import os
import re
import shutil
import subprocess
import unicodedata
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.generator import TrainingGenerator
from src.parser import SOPParser
from src.scorm_exporter import SCORMExporter

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SOP = REPO_ROOT / "examples" / "sample_sop.txt"

PACKAGE_NAME = "integrity_test_package"
#: Files an LMS serves to the learner.  Nothing here may contain the key.
LEARNER_FACING = ("assessment.html", "scorm_api.js", "styles.css")


# ---------------------------------------------------------------------------
# Independent re-implementation of the client's normalise + hash.
#
# Deliberately NOT importing src.answer_key: it classifies characters through
# unicodedata.category rather than str.isalnum(), which is what validates the
# claim that Python's isalnum() and JavaScript's \p{L}\p{N} agree.
# ---------------------------------------------------------------------------
def reference_normalize(text):
    source = unicodedata.normalize("NFKC", text or "").lower()
    chars = []
    gap = False
    for ch in source:
        if unicodedata.category(ch)[0] in ("L", "N"):
            if gap and chars:
                chars.append(" ")
            chars.append(ch)
            gap = False
        else:
            gap = True
    return "".join(chars)


def reference_hash(salt, option_text):
    material = "{0}|{1}".format(salt, reference_normalize(option_text))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sop():
    return SOPParser().parse(str(SAMPLE_SOP))


@pytest.fixture(scope="module")
def built(tmp_path_factory, sop):
    """Build one package and reuse it across the module."""
    output = tmp_path_factory.mktemp("scorm")
    training = TrainingGenerator().generate(sop)
    assessment = MedicalDeviceAssessmentGenerator().generate(sop, num_questions=8)
    zip_path = SCORMExporter(scorm_version="1.2").create_package(
        training, assessment, str(output), PACKAGE_NAME)
    package_dir = output / PACKAGE_NAME
    return {
        "assessment": assessment,
        "training": training,
        "package_dir": package_dir,
        "zip_path": Path(zip_path),
        "assessment_html": (package_dir / "assessment.html").read_text(encoding="utf-8"),
    }


def shipped_questions(assessment_html):
    """The learner payload as the browser sees it."""
    match = re.search(r"var questions = (\[.*?\]);\s*\n", assessment_html, re.S)
    assert match, "learner payload not found in assessment.html"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Package shape - must not regress while fixing integrity
# ---------------------------------------------------------------------------
def test_zip_contains_manifest_at_root(built):
    with zipfile.ZipFile(built["zip_path"]) as archive:
        names = archive.namelist()
    assert "imsmanifest.xml" in names, "manifest must sit at the archive root"
    assert "assessment.html" in names
    assert "scorm_api.js" in names
    assert "styles.css" in names
    assert "metadata.json" in names
    assert any(n.startswith("content_") and n.endswith(".html") for n in names)
    assert not any(n.startswith("/") or ".." in n for n in names)


def test_manifest_is_well_formed_with_expected_items(built):
    manifest_bytes = (built["package_dir"] / "imsmanifest.xml").read_bytes()
    root = etree.fromstring(manifest_bytes)

    namespace = {"cp": "http://www.imsproject.org/xsd/imscp_rootv1p1p2"}
    assert root.tag.endswith("manifest")
    assert root.get("identifier") == "MANIFEST-01"

    schema = root.find("cp:metadata/cp:schema", namespace)
    assert schema is not None and schema.text == "ADL SCORM"
    version = root.find("cp:metadata/cp:schemaversion", namespace)
    assert version is not None and version.text == "1.2"

    organization = root.find("cp:organizations/cp:organization", namespace)
    assert organization is not None
    items = organization.findall("cp:item", namespace)
    sections = len(built["training"].sections)
    assert len(items) == sections + 1, "one item per section plus the assessment"
    assert items[-1].get("identifier") == "ITEM-ASSESSMENT"

    resources = root.findall("cp:resources/cp:resource", namespace)
    identifiers = {r.get("identifier") for r in resources}
    assert "RES-ASSESSMENT" in identifiers
    hrefs = {r.get("href") for r in resources}
    assert "assessment.html" in hrefs

    # Every referenced file is actually in the package.
    for resource in resources:
        for file_element in resource.findall("cp:file", namespace):
            assert (built["package_dir"] / file_element.get("href")).exists()


def test_scorm_api_still_present_and_wired(built):
    api = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    for function in ("initializeSCORM", "setComplete", "setPassed", "setFailed",
                     "setScore", "getAPI"):
        assert function in api
    page = built["assessment_html"]
    assert 'src="scorm_api.js"' in page
    for call in ("setScore(", "setComplete()", "setPassed()", "setFailed()",
                 "finishSCORM()"):
        assert call in page


def test_scorm_api_speaks_both_runtime_bindings(built):
    """One wrapper, two run-time APIs.

    SCORM 1.2 exposes ``API``/``LMSSetValue``/``cmi.core.*``; SCORM 2004
    exposes ``API_1484_11``/``SetValue``/``cmi.score.scaled``.  The wrapper
    used to know only the first, so a 2004 LMS recorded nothing at all.  The
    behaviour is asserted end to end in ``tests/conformance/test_runtime.py``;
    this is the cheap regression guard that runs without a browser.
    """
    api = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    for token in ("API_1484_11", "Initialize", "Terminate", "SetValue",
                  "cmi.score.scaled", "cmi.completion_status",
                  "cmi.success_status"):
        assert token in api, "no SCORM 2004 support: {0} missing".format(token)
    for token in ("LMSInitialize", "LMSFinish", "LMSSetValue", "LMSCommit",
                  "cmi.core.lesson_status", "cmi.core.score.raw"):
        assert token in api, "no SCORM 1.2 support: {0} missing".format(token)
    # The discovery walk is bounded, as the specification's pseudo-code is.
    assert "SCORM_MAX_PARENTS = 7" in api
    assert "window.opener" in api


def test_scorm_api_is_identical_in_both_versions(tmp_path, sop):
    """The wrapper is discovery-driven, so the same file ships in both
    packages; only the manifest differs by version."""
    written = {}
    for version in ("1.2", "2004"):
        training = TrainingGenerator().generate(sop)
        assessment = AssessmentGenerator().generate(sop, num_questions=6)
        SCORMExporter(scorm_version=version).create_package(
            training, assessment, str(tmp_path / version), PACKAGE_NAME)
        package = tmp_path / version / PACKAGE_NAME
        written[version] = (package / "scorm_api.js").read_text(encoding="utf-8")
    assert written["1.2"] == written["2004"]


def test_2004_package_is_a_2004_package(tmp_path, sop):
    """The regression this exporter needed: asking for 2004 used to produce a
    SCORM 1.2 manifest with the string "2004" in <schemaversion>."""
    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=6)
    SCORMExporter(scorm_version="2004").create_package(
        training, assessment, str(tmp_path), PACKAGE_NAME)
    root = etree.fromstring(
        (tmp_path / PACKAGE_NAME / "imsmanifest.xml").read_bytes())

    cp = "http://www.imsglobal.org/xsd/imscp_v1p1"
    adlcp = "http://www.adlnet.org/xsd/adlcp_v1p3"
    assert root.tag == "{%s}manifest" % cp
    assert "http://www.imsproject.org/xsd/imscp_rootv1p1p2" not in \
        set(root.nsmap.values()), "still bound to the SCORM 1.2 namespace"

    version = root.find("{%s}metadata/{%s}schemaversion" % (cp, cp))
    assert version is not None and version.text == "2004 4th Edition"

    resources = root.findall("{%s}resources/{%s}resource" % (cp, cp))
    assert resources
    for resource in resources:
        # capital T: 2004 renamed the attribute
        assert resource.get("{%s}scormType" % adlcp) in ("sco", "asset")
        assert resource.get("{%s}scormtype" % adlcp) is None


def test_every_declared_file_is_in_the_package_and_vice_versa(built):
    """styles.css and scorm_api.js used to be in the zip but declared by no
    resource.  An LMS that deploys only what the manifest declares then serves
    a SCO with no API wrapper at all."""
    root = etree.fromstring(
        (built["package_dir"] / "imsmanifest.xml").read_bytes())
    namespace = {"cp": "http://www.imsproject.org/xsd/imscp_rootv1p1p2"}

    declared = {element.get("href")
                for element in root.iter("{%s}file" % namespace["cp"])}
    for href in declared:
        assert (built["package_dir"] / href).exists(), href

    present = {path.name for path in built["package_dir"].iterdir()
               if path.is_file()} - {"imsmanifest.xml"}
    assert present <= declared, sorted(present - declared)
    for asset in ("styles.css", "scorm_api.js", "metadata.json"):
        assert asset in declared


def test_draft_watermark_is_kept(built):
    assert "DRAFT TRAINING - REVIEW REQUIRED" in built["assessment_html"]
    for content in built["package_dir"].glob("content_*.html"):
        assert "DRAFT TRAINING - REVIEW REQUIRED" in content.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Defect 2: the answer key shipped to the learner
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("filename", LEARNER_FACING)
def test_no_answer_key_structure_in_learner_files(built, filename):
    blob = (built["package_dir"] / filename).read_text(encoding="utf-8")
    for token in ("correct_answer", "correctAnswer", "explanation", "to_dict"):
        assert token not in blob, (
            "{0} still mentions {1!r} - the key structure must not ship".format(
                filename, token))


def test_no_explanation_text_reaches_the_learner(built):
    page = built["assessment_html"]
    for question in built["assessment"].questions:
        if len(question.explanation) > 25:
            assert question.explanation not in page, question.id


def test_correct_option_appears_exactly_where_every_option_appears(built):
    """The correct answer must be indistinguishable from the distractors in the
    rendered page: same number of appearances, same markup, no extra flag."""
    page = built["assessment_html"]
    blocks = re.split(r'<div class="question" id="question-\d+">', page)[1:]
    assert len(blocks) == len(built["assessment"].questions)

    for question, block in zip(built["assessment"].questions, blocks):
        block = block.split('<div class="navigation">')[0]
        counts = {}
        for index, option in enumerate(question.options):
            counts[index] = block.count(html_module.escape(option))
        # At minimum every option appears twice: once as the visible label and
        # once as the data-option attribute the verifier hashes.  "True or
        # False:" prompts push every option of that question to three.  What
        # matters is that the count is the SAME for all of them, so the correct
        # answer has no extra appearance anywhere to give it away.
        assert len(set(counts.values())) == 1, (
            "{0}: option appearance counts differ {1}".format(question.id, counts))
        assert min(counts.values()) >= 2, counts

    payload = shipped_questions(page)
    for question, shipped in zip(built["assessment"].questions, payload):
        assert shipped["options"] == question.options
        # Nothing in the payload marks which one is right.
        assert set(shipped) == {"id", "type", "text", "options", "points",
                                "salt", "answer_hash", "topic"}


def test_every_question_ships_a_salt_and_a_64_hex_hash(built):
    payload = shipped_questions(built["assessment_html"])
    assert payload
    salts = set()
    for shipped in payload:
        assert re.fullmatch(r"[0-9a-f]{32}", shipped["salt"]), shipped["id"]
        assert re.fullmatch(r"[0-9a-f]{64}", shipped["answer_hash"]), shipped["id"]
        salts.add(shipped["salt"])
    assert len(salts) == len(payload), "salts must be per question"


def test_hash_round_trip_identifies_exactly_one_option(built):
    """Independent re-implementation: the shipped hash matches the correct
    option and no other."""
    payload = {q["id"]: q for q in shipped_questions(built["assessment_html"])}

    for question in built["assessment"].questions:
        shipped = payload[question.id]
        correct = question.options[question.correct_answer]
        assert reference_hash(shipped["salt"], correct) == shipped["answer_hash"], (
            "hash does not verify the correct option for {0}".format(question.id))

        matches = [option for option in shipped["options"]
                   if reference_hash(shipped["salt"], option) == shipped["answer_hash"]]
        assert matches == [correct], (
            "{0}: {1} options match the hash".format(question.id, len(matches)))


def test_hash_is_stable_across_rendering_differences(built):
    """Normalisation absorbs whitespace and punctuation drift, so a browser that
    reflows an option still verifies."""
    payload = shipped_questions(built["assessment_html"])
    shipped = payload[0]
    correct = None
    for option in shipped["options"]:
        if reference_hash(shipped["salt"], option) == shipped["answer_hash"]:
            correct = option
    assert correct is not None

    for variant in ("  " + correct + "  ",
                    re.sub(r"\s+", "\n    ", correct),
                    correct.upper()):
        assert reference_hash(shipped["salt"], variant) == shipped["answer_hash"]


def test_metadata_declares_the_integrity_limitation(built):
    metadata = json.loads(
        (built["package_dir"] / "metadata.json").read_text(encoding="utf-8"))
    integrity = metadata.get("assessment_integrity")
    assert integrity, "metadata.json must disclose how scoring is protected"
    assert integrity["plaintext_key_shipped"] is False
    assert "developer tools" in integrity["known_limitation"]
    assert integrity["planned_remediation"]


# ---------------------------------------------------------------------------
# Python / JavaScript parity
# ---------------------------------------------------------------------------
def test_client_verifier_is_inlined_not_a_separate_file(built):
    """Package file layout is unchanged, so the verifier lives in the page."""
    page = built["assessment_html"]
    assert "var AnswerKey" in page
    assert "normalizeOptionText" in page
    assert "sha256Fallback" in page, "a pure-JS fallback is required for file://"
    assert "crypto.subtle" in page or "subtle.digest" in page
    assert not (built["package_dir"] / "answer_key.js").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_javascript_hashing_matches_python(built, tmp_path):
    """Run the shipped verifier under node and compare with Python.

    Both the crypto.subtle path and the pure-JS fallback are exercised: if they
    ever diverge from hashlib, every learner's score is wrong.
    """
    from src.answer_key import CLIENT_VERIFIER_JS

    payload = shipped_questions(built["assessment_html"])
    cases = []
    for shipped in payload:
        for option in shipped["options"]:
            cases.append({"salt": shipped["salt"], "text": option})
    assert cases

    (tmp_path / "answer_key.js").write_text(CLIENT_VERIFIER_JS, encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")

    runner = """
const fs = require('fs');
const path = require('path');
const force = process.argv[2] === 'fallback';
if (force) {
    Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
}
const AnswerKey = require(path.join(__dirname, 'answer_key.js'));
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'cases.json'), 'utf8'));
const out = new Array(cases.length);
let pending = cases.length;
cases.forEach((c, i) => {
    AnswerKey.sha256Hex(c.salt + '|' + AnswerKey.normalize(c.text), h => {
        out[i] = h;
        if (--pending === 0) { process.stdout.write(JSON.stringify(out)); }
    });
});
"""
    (tmp_path / "runner.js").write_text(runner, encoding="utf-8")

    expected = [reference_hash(c["salt"], c["text"]) for c in cases]
    for mode in ("subtle", "fallback"):
        result = subprocess.run(
            ["node", str(tmp_path / "runner.js"), mode],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == expected, (
            "JavaScript ({0} path) disagrees with Python".format(mode))


# ---------------------------------------------------------------------------
# Determinism at the package level
# ---------------------------------------------------------------------------
def test_assessment_page_is_reproducible(tmp_path, sop):
    """Two builds of the same SOP produce the same assessment page.

    The rest of the package embeds a generation timestamp, so only the
    assessment body is compared.
    """
    pages = []
    for run in range(2):
        training = TrainingGenerator().generate(sop)
        assessment = AssessmentGenerator().generate(sop, num_questions=8)
        SCORMExporter().create_package(
            training, assessment, str(tmp_path / str(run)), PACKAGE_NAME)
        page = (tmp_path / str(run) / PACKAGE_NAME / "assessment.html").read_text(
            encoding="utf-8")
        pages.append(re.sub(r"Generated: [^|]+\|", "Generated: |", page))
    assert pages[0] == pages[1]


# ---------------------------------------------------------------------------
# Option text is escaped, so SOP content cannot break or inject into the page
# ---------------------------------------------------------------------------
def test_sop_markup_is_escaped_in_the_assessment(tmp_path):
    from src.parser import SOPContent

    sop = SOPContent()
    sop.title = "Injection <script>alert(1)</script> SOP"
    sop.version = "1.0"
    sop.purpose = ('Prevent contamination when the "A & B" valve <assembly> is '
                   "removed during a controlled shutdown of the line.")
    sop.scope = ('Applies to operators on Line <3> and Line "4" during planned '
                 "maintenance of the valve assembly and associated pipework.")
    sop.safety_warnings = [
        'Never open the <main> valve while the line is pressurised & running.',
        'All operators must wear goggles when the "B" reagent is decanted.',
        'Do not vent the line into the <room> during a purge cycle.',
    ]
    sop.procedures = [
        {"step_number": str(i), "title": "Step <{0}> & more".format(i),
         "body": 'Close the "{0}" valve </script> and log reading <{0}> on the sheet.'.format(i),
         "content": "x", "substeps": []}
        for i in range(1, 6)
    ]
    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=8)
    SCORMExporter().create_package(training, assessment, str(tmp_path), PACKAGE_NAME)

    page = (tmp_path / PACKAGE_NAME / "assessment.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    for question in assessment.questions:
        for option in question.options:
            assert html_module.escape(option) in page

    # A step body containing "</script>" must not close the payload's script
    # tag. The page has exactly the script tags the exporter opened.
    assert page.count("</script>") == page.count("<script")
    # The payload is still valid JSON and still round-trips.
    payload = shipped_questions(page)
    assert len(payload) == len(assessment.questions)
    for question, shipped in zip(assessment.questions, payload):
        assert shipped["options"] == question.options
        assert reference_hash(
            shipped["salt"],
            question.options[question.correct_answer]) == shipped["answer_hash"]


# ---------------------------------------------------------------------------
# The CLI's standalone HTML is a preview, not an assessment
# ---------------------------------------------------------------------------
def test_cli_standalone_html_escapes_and_declares_itself_a_preview():
    """`--format html` renders SOP text, so it must escape it, and it must say
    that it does no scoring - it has no answer key and no submit button."""
    from src.generator import TrainingGenerator
    from src.parser import SOPContent
    from src.cli import _create_standalone_html

    sop = SOPContent()
    sop.title = "Injection <script>alert(1)</script> SOP"
    sop.version = "1.0"
    sop.purpose = ('Prevent contamination when the "A & B" valve <assembly> is '
                   "removed during a controlled shutdown of the production line.")
    sop.scope = ('Applies to operators on Line <3> and Line "4" during planned '
                 "maintenance of the valve assembly and associated pipework.")
    sop.safety_warnings = [
        "Never open the <main> valve while the line is pressurised & running.",
        'All operators must wear goggles when the "B" reagent is decanted.',
        "Do not vent the line into the <room> during a purge cycle.",
    ]
    sop.procedures = [
        {"step_number": str(i), "title": "Step <{0}> & more".format(i),
         "body": 'Close the "{0}" valve </script> and log reading <{0}>.'.format(i),
         "content": "x", "substeps": []}
        for i in range(1, 7)
    ]

    training = TrainingGenerator().generate(sop)
    assessment = AssessmentGenerator().generate(sop, num_questions=6)
    page = _create_standalone_html(training, assessment)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    for question in assessment.questions:
        for option in question.options:
            assert html_module.escape(str(option)) in page
    # No answer key, and it says plainly that it does not score.
    for token in ("correct_answer", "answer_hash", "AnswerKey"):
        assert token not in page
    assert "Preview only" in page


# ---------------------------------------------------------------------------
# Browser check (bonus - skipped unless Playwright for Python is installed)
# ---------------------------------------------------------------------------
def _launch_chromium(playwright):
    """Launch Chromium, or skip.

    Never installs anything: if the Playwright package and the browser build on
    disk disagree (a pinned image can carry one and expect another) fall back to
    whatever Chromium binary is already present under PLAYWRIGHT_BROWSERS_PATH.
    """
    try:
        return playwright.chromium.launch()
    except Exception:
        pass

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
    candidates += sorted(root.glob("chromium-*/chrome-linux/chrome"))
    for candidate in candidates:
        try:
            return playwright.chromium.launch(executable_path=str(candidate))
        except Exception:
            continue
    pytest.skip("no usable Chromium build is installed")


def test_naive_learner_fails_in_a_real_browser(built):
    """Load assessment.html from file://, answer naively, assert failure; then
    answer correctly and assert the pass message.

    file:// has no secure context, so this also proves the pure-JS SHA-256
    fallback path works end to end in a real browser.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright for Python is not installed")

    assessment = built["assessment"]
    url = (built["package_dir"] / "assessment.html").as_uri()

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            page = browser.new_page()

            page.goto(url)
            for question in assessment.questions:
                page.locator(
                    'input[name="q_{0}"]'.format(question.id)).nth(0).check()
            page.click("#submit-button")
            page.wait_for_selector("#results.results.fail", timeout=15000)
            assert "Additional Study Required" in page.inner_text("#results")

            page.goto(url)
            for question in assessment.questions:
                page.locator('input[name="q_{0}"]'.format(question.id)).nth(
                    question.correct_answer).check()
            page.click("#submit-button")
            page.wait_for_selector("#results.results.pass", timeout=15000)
            assert "You Passed" in page.inner_text("#results")
        finally:
            browser.close()
