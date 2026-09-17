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
    for token in ("correct_answer", "correctAnswer", "explanation", "to_dict",
                  # cmi.interactions.n.correct_responses.0.pattern IS the key.
                  # The package reports what was answered, never what was
                  # expected - see docs/SCORM_CONFORMANCE.md.
                  "correct_responses", "correctResponses"):
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
#
# The ``chromium`` fixture (a headless Chromium on the session's one shared
# Playwright driver, or a clean skip when none is installed) is defined once,
# in tests/conftest.py, and shared with tests/conformance/.
# ---------------------------------------------------------------------------
def test_naive_learner_fails_in_a_real_browser(built, chromium):
    """Load assessment.html from file://, answer naively, assert failure; then
    answer correctly and assert the pass message.

    file:// has no secure context, so this also proves the pure-JS SHA-256
    fallback path works end to end in a real browser.
    """
    assessment = built["assessment"]
    url = (built["package_dir"] / "assessment.html").as_uri()

    page = chromium.new_page()

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


# ---------------------------------------------------------------------------
# cmi.interactions - per-question evidence for the LMS record
#
# The run-time behaviour is asserted in tests/conformance/test_runtime.py,
# against a real browser and a recording LMS.  What is checked here is the
# Python half (the metadata the page is built with) and the two properties
# that must hold whatever an LMS does: the answer key is not in the payload,
# and the page is still reproducible.
# ---------------------------------------------------------------------------
#: The answer key, in each of the spellings it could reach a learner-facing
#: file by.  ``correct_responses`` is the SCORM element that states the
#: expected answer pattern - writing it would hand the key to anyone with the
#: browser's network tab, which is the same defect as Defect 2.
FORBIDDEN_IN_LEARNER_FILES = ("correct_responses", "correct_answer",
                              "explanation")


@pytest.mark.parametrize("version", ["1.2", "2004"])
@pytest.mark.parametrize("generator", [AssessmentGenerator,
                                       MedicalDeviceAssessmentGenerator])
def test_key_bearing_strings_appear_in_no_learner_file(tmp_path, sop, version,
                                                       generator):
    """Grep, deliberately: no parsing, no interpretation, just "is this string
    anywhere in the two files the learner's browser downloads"."""
    training = TrainingGenerator().generate(sop)
    assessment = generator().generate(sop, num_questions=8)
    root = tmp_path / version / generator.__name__
    SCORMExporter(scorm_version=version).create_package(
        training, assessment, str(root), PACKAGE_NAME)
    package = root / PACKAGE_NAME

    for filename in ("assessment.html", "scorm_api.js"):
        blob = (package / filename).read_text(encoding="utf-8")
        for token in FORBIDDEN_IN_LEARNER_FILES:
            assert token not in blob, "{0} contains {1!r}".format(filename, token)


def shipped_interaction_metadata(assessment_html):
    """The interaction metadata as the browser sees it."""
    match = re.search(r"var interactionMeta = (\[.*?\]);\s*\n",
                      assessment_html, re.S)
    assert match, "interaction metadata not found in assessment.html"
    return json.loads(match.group(1).replace("\\u003c", "<")
                      .replace("\\u003e", ">").replace("\\u0026", "&"))


def test_interaction_metadata_has_one_entry_per_question(built):
    meta = shipped_interaction_metadata(built["assessment_html"])
    questions = built["assessment"].questions
    assert len(meta) == len(questions)
    assert [entry["id"] for entry in meta] == [q.id for q in questions]


def test_interaction_metadata_types_and_weighting(built):
    meta = shipped_interaction_metadata(built["assessment_html"])
    for entry, question in zip(meta, built["assessment"].questions):
        expected = "true-false" if question.type == "true_false" else "choice"
        assert entry["type"] == expected, question.id
        assert entry["weighting"] == str(question.points), question.id


def test_interaction_metadata_response_tokens_are_per_version(built):
    """1.2 wants CMIFeedback ("a"/"t"), 2004 wants a choice identifier or the
    literal "true"/"false".  One table per version, both by rendered
    position, so an option's token cannot drift from what the learner saw."""
    meta = shipped_interaction_metadata(built["assessment_html"])
    for entry, question in zip(meta, built["assessment"].questions):
        assert len(entry["responses12"]) == len(question.options), question.id
        assert len(entry["responses2004"]) == len(question.options), question.id
        if question.type == "true_false":
            for index, option in enumerate(question.options):
                truthy = option.strip().lower() == "true"
                assert entry["responses12"][index] == ("t" if truthy else "f")
                assert entry["responses2004"][index] == \
                    ("true" if truthy else "false")
        else:
            expected = list("abcdefghijklmnopqrstuvwxyz"[:len(question.options)])
            assert entry["responses12"] == expected, question.id
            assert entry["responses2004"] == expected, question.id


def test_interaction_metadata_carries_no_answer_key(built):
    """The metadata is derived from the question, never from its key: nothing
    in it distinguishes the correct option from a distractor."""
    meta = shipped_interaction_metadata(built["assessment_html"])
    for entry, question in zip(meta, built["assessment"].questions):
        assert set(entry) == {"id", "type", "weighting", "description",
                              "objective", "responses12", "responses2004"}
        blob = json.dumps(entry)
        if question.type == "true_false":
            # Both truth values are present, for both positions, so which one
            # is right cannot be read off the entry.
            assert sorted(entry["responses2004"]) == ["false", "true"]
            assert sorted(entry["responses12"]) == ["f", "t"]
        else:
            # A choice question's option text appears nowhere in the entry.
            for option in question.options:
                assert option not in blob, question.id
        if len(question.explanation) > 25:
            assert question.explanation not in blob, question.id


def test_interaction_description_is_the_prompt_clipped_to_the_spm(built):
    """SCORM 2004 caps cmi.interactions.n.description at 250 characters."""
    from src.scorm_exporter import INTERACTION_DESCRIPTION_MAX

    assert INTERACTION_DESCRIPTION_MAX == 250
    meta = shipped_interaction_metadata(built["assessment_html"])
    for entry, question in zip(meta, built["assessment"].questions):
        assert len(entry["description"]) <= 250, question.id
        assert entry["description"] == " ".join(question.text.split())[:250]
        assert "\n" not in entry["description"]


def test_interaction_objective_comes_from_the_source_ref(built):
    meta = shipped_interaction_metadata(built["assessment_html"])
    for entry, question in zip(meta, built["assessment"].questions):
        kind = (question.source_ref or {}).get("kind")
        if kind:
            assert entry["objective"] == kind, question.id
        assert " " not in entry["objective"], question.id


def test_interaction_objective_falls_back_to_a_slugged_topic():
    """A question with no source_ref kind still rolls up to something, and
    that something is a legal identifier rather than a sentence."""
    from src.scorm_exporter import _interaction_objective_id

    class _Q:
        source_ref = {}
        topic = "Safety warnings"

    assert _interaction_objective_id(_Q()) == "safety_warnings"

    class _Empty:
        source_ref = None
        topic = ""

    assert _interaction_objective_id(_Empty()) == ""


def test_the_page_writes_interactions_before_it_ends_the_session(built):
    """Order is the whole point: an interaction written after LMSFinish is
    rejected and the evidence is lost."""
    page = built["assessment_html"]
    record_at = page.index("recordInteractions(interactionMeta")
    finish_at = page.index("finishSCORM();", record_at)
    assert record_at < page.index("setScore(percentage)") < finish_at


def test_scorm_api_writes_the_interaction_data_model(built):
    api = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    for token in ("cmi.interactions.", "student_response", "learner_response",
                  "objectives.0.id", "cmi.suspend_data",
                  "cmi.core.session_time", "cmi.session_time",
                  "cmi.core.exit", "cmi.exit", "recordInteractions"):
        assert token in api, token
    from src.scorm_exporter import SUSPEND_DATA_MAX

    assert SUSPEND_DATA_MAX == 4096
    assert "SCORM_SUSPEND_DATA_MAX = 4096" in api
    assert "__SUSPEND_DATA_MAX__" not in api


def test_interaction_metadata_is_reproducible(tmp_path, sop):
    pages = []
    for run in range(2):
        training = TrainingGenerator().generate(sop)
        assessment = AssessmentGenerator().generate(sop, num_questions=8)
        SCORMExporter().create_package(
            training, assessment, str(tmp_path / str(run)), PACKAGE_NAME)
        page = (tmp_path / str(run) / PACKAGE_NAME / "assessment.html").read_text(
            encoding="utf-8")
        pages.append(shipped_interaction_metadata(page))
    assert pages[0] == pages[1]


# ---------------------------------------------------------------------------
# The shipped wrapper, run for real under node
# ---------------------------------------------------------------------------
_INTERACTION_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console: console };
sandbox.window = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const model = {};
const written = [];
const api = {};
const get = k => (Object.prototype.hasOwnProperty.call(model, k) ? model[k] : "");
const set = (k, v) => { model[k] = v; written.push([k, v]); return "true"; };
if (spec.version === "2004") {
    api.GetValue = get; api.SetValue = set;
    api.Commit = () => "true"; api.Terminate = () => "true";
} else {
    api.LMSGetValue = get; api.LMSSetValue = set;
    api.LMSCommit = () => "true"; api.LMSFinish = () => "true";
}
sandbox.scorm.api = api;
sandbox.scorm.version = spec.version;
sandbox.scorm.initialized = true;
sandbox.scorm.terminated = false;

Object.keys(spec.preset || {}).forEach(k => { model[k] = spec.preset[k]; });

sandbox.recordInteractions(spec.meta, spec.outcomes);
sandbox.finishSCORM();

process.stdout.write(JSON.stringify({ model: model, written: written }));
"""


def _run_wrapper_under_node(tmp_path, api_js, spec):
    (tmp_path / "scorm_api.js").write_text(api_js, encoding="utf-8")
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (tmp_path / "runner.js").write_text(_INTERACTION_RUNNER, encoding="utf-8")
    result = subprocess.run(
        ["node", str(tmp_path / "runner.js"), str(tmp_path / "scorm_api.js"),
         str(tmp_path / "spec.json")],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _wide_spec(version, count=400):
    """Enough answered questions that the 4096-character cap actually bites."""
    meta = []
    outcomes = []
    for index in range(count):
        meta.append({
            "id": "question_with_a_deliberately_long_identifier_{0:04d}".format(index),
            "type": "choice",
            "weighting": "2",
            "description": "prompt {0}".format(index),
            "objective": "step",
            "responses12": ["a", "b", "c", "d"],
            "responses2004": ["a", "b", "c", "d"],
        })
        outcomes.append({"answered": True, "option": index % 4, "right": True})
    return {"version": version, "meta": meta, "outcomes": outcomes}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_suspend_data_is_truncated_to_the_cap(built, tmp_path, version):
    """4096 characters is a hard limit in both bindings.  The page has to fit
    inside it itself: an LMS that rejects the write loses the whole record,
    and several accept it and silently store a prefix."""
    api_js = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    spec = _wide_spec(version)
    result = _run_wrapper_under_node(tmp_path, api_js, spec)

    suspend = result["model"]["cmi.suspend_data"]
    assert len(suspend) <= 4096, len(suspend)
    parsed = json.loads(suspend)
    assert parsed["attempt"] == 1
    # Responses were dropped from the end, and the ones kept are a prefix of
    # what was answered - not an arbitrary subset.
    kept = list(parsed["responses"])
    assert kept, "the cap must not empty the record entirely"
    assert kept == [entry["id"] for entry in spec["meta"][:len(kept)]]
    assert len(kept) < len(spec["meta"]), "this case is meant to overflow"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_the_wrapper_writes_the_documented_elements_and_no_others(built,
                                                                  tmp_path,
                                                                  version):
    """Run the shipped wrapper for real and read back the exact element names
    it puts on the wire, including the version-specific time formats."""
    api_js = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    spec = {
        "version": version,
        "meta": [
            {"id": "step_mc_2", "type": "choice", "weighting": "2",
             "description": "According to Step 2, what must be done?",
             "objective": "step",
             "responses12": ["a", "b", "c", "d"],
             "responses2004": ["a", "b", "c", "d"]},
            {"id": "safety_tf_1", "type": "true-false", "weighting": "1",
             "description": "True or False: the guard may be removed.",
             "objective": "safety",
             "responses12": ["t", "f"], "responses2004": ["true", "false"]},
            {"id": "md_req_1", "type": "choice", "weighting": "4",
             "description": "Who do you notify?", "objective": "md_required",
             "responses12": ["a", "b"], "responses2004": ["a", "b"]},
        ],
        "outcomes": [
            {"answered": True, "option": 1, "right": False},
            {"answered": True, "option": 1, "right": True},
            {"answered": False, "option": -1, "right": False},
        ],
    }
    result = _run_wrapper_under_node(tmp_path, api_js, spec)
    model = result["model"]

    if version == "2004":
        response, when = "learner_response", "timestamp"
        assert model["cmi.interactions.0.result"] == "incorrect"
        assert model["cmi.interactions.1." + response] == "false"
        assert model["cmi.interactions.0.description"] == \
            spec["meta"][0]["description"]
        assert model["cmi.interactions.2.objectives.0.id"] == "md_required"
        assert re.fullmatch(r"PT\d+H\d+M\d+\.\d{2}S",
                            model["cmi.interactions.0.latency"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-2]\d:[0-5]\d:[0-5]\dZ",
                            model["cmi.interactions.0." + when])
        assert re.fullmatch(r"PT\d+H\d+M\d+\.\d{2}S", model["cmi.session_time"])
        assert model["cmi.exit"] == "normal"
    else:
        response, when = "student_response", "time"
        assert model["cmi.interactions.0.result"] == "wrong"
        assert model["cmi.interactions.1." + response] == "f"
        assert "cmi.interactions.0.description" not in model
        assert "cmi.interactions.0.objectives.0.id" not in model
        assert re.fullmatch(r"\d{4}:[0-5]\d:[0-5]\d\.\d{2}",
                            model["cmi.interactions.0.latency"])
        assert re.fullmatch(r"[0-2]\d:[0-5]\d:[0-5]\d",
                            model["cmi.interactions.0." + when])
        assert re.fullmatch(r"\d{4}:[0-5]\d:[0-5]\d\.\d{2}",
                            model["cmi.core.session_time"])
        assert model["cmi.core.exit"] == ""

    assert model["cmi.interactions.0.id"] == "step_mc_2"
    assert model["cmi.interactions.0." + response] == "b"
    assert model["cmi.interactions.0.weighting"] == "2"
    assert model["cmi.interactions.1.type"] == "true-false"
    assert model["cmi.interactions.1.result"] == "correct"
    # The unanswered one: recorded, weighted, and explicitly neutral.
    assert model["cmi.interactions.2.result"] == "neutral"
    assert model["cmi.interactions.2.weighting"] == "4"
    assert "cmi.interactions.2." + response not in model

    assert json.loads(model["cmi.suspend_data"]) == {
        "attempt": 1, "responses": {"step_mc_2": "b",
                                    "safety_tf_1": spec["meta"][1][
                                        "responses2004" if version == "2004"
                                        else "responses12"][1]},
    }
    assert not any(element.startswith("correct_responses")
                   or ".correct_responses" in element for element in model)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_suspend_data_increments_the_attempt_counter(built, tmp_path, version):
    api_js = (built["package_dir"] / "scorm_api.js").read_text(encoding="utf-8")
    spec = {
        "version": version,
        "preset": {"cmi.suspend_data": json.dumps(
            {"attempt": 3, "responses": {"step_mc_2": "a"}})},
        "meta": [{"id": "step_mc_2", "type": "choice", "weighting": "2",
                  "description": "d", "objective": "step",
                  "responses12": ["a", "b"], "responses2004": ["a", "b"]}],
        "outcomes": [{"answered": True, "option": 0, "right": True}],
    }
    result = _run_wrapper_under_node(tmp_path, api_js, spec)
    assert json.loads(result["model"]["cmi.suspend_data"])["attempt"] == 4

    spec["preset"] = {"cmi.suspend_data": "not json at all"}
    result = _run_wrapper_under_node(tmp_path, api_js, spec)
    assert json.loads(result["model"]["cmi.suspend_data"])["attempt"] == 1
