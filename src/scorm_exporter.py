"""
SCORM Exporter - Export training content to SCORM-compliant packages
"""

import html
import json
import os
import zipfile
from pathlib import Path
from typing import Optional
from datetime import datetime
from lxml import etree

from .answer_key import CLIENT_VERIFIER_JS, normalize_option_text
from .generator import TrainingModule
from .assessments import Assessment

# ---------------------------------------------------------------------------
# Content Aggregation Model namespaces.
#
# SCORM 1.2 and SCORM 2004 are *different* binding vocabularies, not one
# vocabulary with a version string: different namespace URIs, a differently
# cased scormType attribute, a different schemaversion token, and - in 2004 -
# the IMS Simple Sequencing and ADL Navigation namespaces on top.  Emitting a
# 1.2 manifest with "2004" in <schemaversion> produces a package that no 2004
# LMS will accept, which is exactly what this exporter used to do.
# ---------------------------------------------------------------------------
NS_CP_12 = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"
NS_ADLCP_12 = "http://www.adlnet.org/xsd/adlcp_rootv1p2"

NS_CP_2004 = "http://www.imsglobal.org/xsd/imscp_v1p1"
NS_ADLCP_2004 = "http://www.adlnet.org/xsd/adlcp_v1p3"
NS_ADLSEQ_2004 = "http://www.adlnet.org/xsd/adlseq_v1p3"
NS_ADLNAV_2004 = "http://www.adlnet.org/xsd/adlnav_v1p3"
NS_IMSSS_2004 = "http://www.imsglobal.org/xsd/imsss"

NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: The exact <schemaversion> token each SCORM version requires.  "2004" on its
#: own is not a value the specification defines.
SCHEMA_VERSION_TOKEN = {"1.2": "1.2", "2004": "2004 4th Edition"}

#: Files every page in the package loads.  They have to be declared as
#: <file> elements on every resource that needs them, or a CAM-conformant LMS
#: that deploys only declared files serves a SCO with no stylesheet and,
#: worse, no SCORM API wrapper - so the course silently reports nothing.
SHARED_FILES = ("styles.css", "scorm_api.js")

# ---------------------------------------------------------------------------
# cmi.interactions - per-question evidence in the LMS record.
#
# The LMS owns the training record (GOAL.md: "not an LMS" is an explicit
# non-goal).  Reporting only a score and a status leaves an auditor with "70%",
# not "which question about the emergency stop did this operator get wrong".
# These constants describe the shape of that evidence; the run-time half is
# recordInteractions() in scorm_api.js, and docs/SCORM_CONFORMANCE.md states
# the format rules each version imposes.
#
# What is deliberately NOT written, in either version: the element that would
# carry the expected answer pattern.  That element *is* the answer key, and
# CLAUDE.md invariant 2 says the key never travels through the learner's
# browser.  The cost is stated plainly in the docs - an auditor can see what
# was answered and whether it was right, but not what the right answer was.
# ---------------------------------------------------------------------------

#: Stable per-option identifier, by *rendered* position.  Option order is baked
#: into ``Question.options`` at generation time and the renderer emits them in
#: that order, so "b" means "the second option as the learner saw it" and keeps
#: that meaning for as long as the package exists.
INTERACTION_OPTION_IDS = "abcdefghijklmnopqrstuvwxyz"

#: SCORM interaction type per generated question type.  A "sequence" question
#: renders as a single-select list of step titles, so to the data model it is a
#: ``choice``, not an ``ordering`` (ordering expects a whole permutation).
INTERACTION_TYPES = {
    "multiple_choice": "choice",
    "sequence": "choice",
    "true_false": "true-false",
}

#: SCORM 2004 caps ``cmi.interactions.n.description`` at 250 characters (the
#: SPM for localized_string_type).  Longer prompts are cut rather than risk a
#: rejected write on a strict LMS.
INTERACTION_DESCRIPTION_MAX = 250

#: SCORM 1.2 caps ``cmi.suspend_data`` at 4096 characters and 2004 keeps the
#: same SPM.  The page enforces it itself instead of discovering it as a
#: "false" return from an LMS that silently drops the write.
SUSPEND_DATA_MAX = 4096


def _interaction_objective_id(question) -> str:
    """The objective an interaction rolls up to, or "" when there is none.

    SCORM 2004's ``cmi.interactions.n.objectives.0.id`` is a long identifier,
    so the question's ``source_ref["kind"]`` ("step", "safety", "sequence",
    "md_required", ...) is used in preference to its human ``topic``; the topic
    is slugified as a fallback.  Neither reveals anything about the answer -
    both are already visible to the learner in the post-submission feedback.
    """
    source_ref = getattr(question, "source_ref", None) or {}
    kind = str(source_ref.get("kind") or "").strip()
    if kind:
        return kind[:255]
    topic = str(getattr(question, "topic", "") or "").strip().lower()
    slug = "".join(ch if ch.isalnum() else "_" for ch in topic).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:255]


def _interaction_description(text: str) -> str:
    """The question prompt, flattened to one line and clipped to the SPM."""
    collapsed = " ".join(str(text or "").split())
    return collapsed[:INTERACTION_DESCRIPTION_MAX]


def build_interaction_metadata(assessment: Assessment) -> list:
    """Per-question metadata the page needs to write ``cmi.interactions``.

    One entry per question, in the order the questions are rendered, so entry
    *n* becomes ``cmi.interactions.n.*``.  Everything here is either already on
    the page (the prompt, the option count) or a non-revealing label; nothing
    derived from ``correct_answer`` is present, which is why this can be
    serialised into the learner's browser at all.

    ``responses_12`` and ``responses_2004`` map a *rendered option index* to the
    response token that version's data model expects:

    * choice - the option identifier by position ("a", "b", "c", "d"); both
      versions use a bare identifier, 1.2 as ``CMIFeedback`` and 2004 as a
      ``choice`` ``learner_response``.
    * true-false - 1.2 wants ``CMIFeedback`` ``"t"``/``"f"``; 2004 wants
      ``"true"``/``"false"``.  Which rendered option is which is decided by
      normalising the option text, so a package that ever renders False first
      still reports the truth value the learner picked, not its position.
    """
    metadata = []
    for question in assessment.questions:
        options = list(getattr(question, "options", None) or [])
        interaction_type = INTERACTION_TYPES.get(question.type, "choice")

        if interaction_type == "true-false":
            responses_12 = []
            responses_2004 = []
            for option in options:
                truthy = normalize_option_text(option) == "true"
                responses_12.append("t" if truthy else "f")
                responses_2004.append("true" if truthy else "false")
        else:
            responses_12 = [INTERACTION_OPTION_IDS[index]
                            if index < len(INTERACTION_OPTION_IDS) else ""
                            for index in range(len(options))]
            responses_2004 = list(responses_12)

        metadata.append({
            "id": question.id,
            "type": interaction_type,
            "weighting": str(question.points),
            "description": _interaction_description(question.text),
            "objective": _interaction_objective_id(question),
            "responses12": responses_12,
            "responses2004": responses_2004,
        })
    return metadata


class SCORMExporter:
    """Export training content to SCORM 1.2/2004 format"""

    def __init__(self, scorm_version: str = "1.2"):
        """
        Initialize SCORM exporter

        Args:
            scorm_version: "1.2" or "2004" (1.2 is more compatible)
        """
        self.scorm_version = scorm_version
        self.supported_versions = ["1.2", "2004"]

        if scorm_version not in self.supported_versions:
            raise ValueError(f"SCORM version must be one of: {self.supported_versions}")

    def create_package(self, training_module: TrainingModule,
                       assessment: Assessment,
                       output_path: str,
                       package_name: Optional[str] = None,
                       approval: Optional[dict] = None) -> str:
        """
        Create a SCORM package from training content

        Args:
            training_module: Training content
            assessment: Assessment questions
            output_path: Directory to create package in
            package_name: Optional custom package name
            approval: Optional SME approval record - ``{"approved_by", "role",
                "approved_at", "notes", "edits_count"}``. When present, every
                page's DRAFT watermark is replaced with an approval banner and
                the record is embedded in metadata.json. When ``None`` (the
                default), output is byte-identical to the unapproved package.

        Returns:
            Path to created ZIP file
        """
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)

        # Create package name
        if not package_name:
            package_name = self._sanitize_filename(training_module.title)

        package_dir = output_path / package_name
        package_dir.mkdir(exist_ok=True)

        # Create SCORM structure
        self._create_manifest(package_dir, training_module, assessment)
        self._create_content_files(package_dir, training_module, assessment, approval=approval)
        self._create_api_files(package_dir)

        # Create metadata file for transparency
        self._create_metadata_file(package_dir, training_module, assessment, package_name,
                                    approval=approval)

        # Create ZIP package
        zip_path = output_path / f"{package_name}.zip"
        self._create_zip(package_dir, zip_path)

        return str(zip_path)

    def _create_manifest(self, package_dir: Path, training_module: TrainingModule,
                        assessment: Assessment):
        """Create imsmanifest.xml, in the binding the requested SCORM version
        actually defines.

        SCORM 1.2 (CAM 1.2) and SCORM 2004 4th Edition (CAM 1.3) share a shape
        but nothing else: the namespaces, the ``scormtype``/``scormType``
        spelling and the ``<schemaversion>`` token all differ, and 2004 carries
        IMS Simple Sequencing.  Both forms are validated against the official
        XSDs in ``tests/conformance/``.
        """
        if self.scorm_version == "2004":
            manifest = self._manifest_2004(training_module, assessment)
        else:
            manifest = self._manifest_12(training_module, assessment)

        tree = etree.ElementTree(manifest)
        manifest_path = package_dir / "imsmanifest.xml"
        tree.write(str(manifest_path), pretty_print=True, xml_declaration=True,
                  encoding='UTF-8')

    def _section_pages(self, training_module: TrainingModule):
        """(item id, resource id, page href, title) for every content page."""
        for idx, section in enumerate(training_module.sections, 1):
            yield (f"ITEM-{idx}", f"RES-{idx}", f"content_{idx}.html",
                   section.get('title', f"Section {idx}"))

    @staticmethod
    def _declare_files(resource, primary: str):
        """Declare the resource's own page plus the shared assets it loads.

        Every page in the package does ``<link href="styles.css">`` and
        ``<script src="scorm_api.js">``.  CAM requires a <file> for each, and
        an LMS is entitled to deploy only what is declared.
        """
        for href in (primary,) + SHARED_FILES:
            element = etree.SubElement(resource, "file")
            element.set("href", href)

    def _manifest_12(self, training_module: TrainingModule,
                     assessment: Assessment):
        """SCORM 1.2 Content Aggregation Model manifest."""
        nsmap = {None: NS_CP_12, "adlcp": NS_ADLCP_12, "xsi": NS_XSI}
        manifest = etree.Element("manifest", nsmap=nsmap)
        manifest.set("identifier", "MANIFEST-01")
        manifest.set("version", "1.0")
        manifest.set("{%s}schemaLocation" % NS_XSI, " ".join([
            NS_CP_12, "imscp_rootv1p1p2.xsd",
            NS_ADLCP_12, "adlcp_rootv1p2.xsd",
        ]))

        metadata = etree.SubElement(manifest, "metadata")
        etree.SubElement(metadata, "schema").text = "ADL SCORM"
        etree.SubElement(metadata, "schemaversion").text = SCHEMA_VERSION_TOKEN["1.2"]

        organizations = etree.SubElement(manifest, "organizations")
        organizations.set("default", "ORG-01")
        organization = etree.SubElement(organizations, "organization")
        organization.set("identifier", "ORG-01")
        etree.SubElement(organization, "title").text = training_module.title

        for item_id, res_id, _href, title in self._section_pages(training_module):
            item = etree.SubElement(organization, "item")
            item.set("identifier", item_id)
            item.set("identifierref", res_id)
            item.set("isvisible", "true")
            etree.SubElement(item, "title").text = title

        item = etree.SubElement(organization, "item")
        item.set("identifier", "ITEM-ASSESSMENT")
        item.set("identifierref", "RES-ASSESSMENT")
        item.set("isvisible", "true")
        etree.SubElement(item, "title").text = "Assessment"
        # The LMS can apply the same pass mark the page applies.
        mastery = etree.SubElement(item, "{%s}masteryscore" % NS_ADLCP_12)
        mastery.text = str(assessment.passing_score)

        resources = etree.SubElement(manifest, "resources")
        for _item_id, res_id, href, _title in self._section_pages(training_module):
            resource = etree.SubElement(resources, "resource")
            resource.set("identifier", res_id)
            resource.set("type", "webcontent")
            resource.set("{%s}scormtype" % NS_ADLCP_12, "sco")
            resource.set("href", href)
            self._declare_files(resource, href)

        resource = etree.SubElement(resources, "resource")
        resource.set("identifier", "RES-ASSESSMENT")
        resource.set("type", "webcontent")
        resource.set("{%s}scormtype" % NS_ADLCP_12, "sco")
        resource.set("href", "assessment.html")
        self._declare_files(resource, "assessment.html")

        # The transparency record is part of the deliverable, so it is declared
        # rather than left to survive on an LMS's goodwill.  It is an asset: no
        # launchable href, nothing to track.
        audit = etree.SubElement(resources, "resource")
        audit.set("identifier", "RES-METADATA")
        audit.set("type", "webcontent")
        audit.set("{%s}scormtype" % NS_ADLCP_12, "asset")
        etree.SubElement(audit, "file").set("href", "metadata.json")

        return manifest

    def _manifest_2004(self, training_module: TrainingModule,
                       assessment: Assessment):
        """SCORM 2004 4th Edition Content Aggregation Model manifest."""
        nsmap = {
            None: NS_CP_2004,
            "adlcp": NS_ADLCP_2004,
            "adlseq": NS_ADLSEQ_2004,
            "adlnav": NS_ADLNAV_2004,
            "imsss": NS_IMSSS_2004,
            "xsi": NS_XSI,
        }
        manifest = etree.Element("manifest", nsmap=nsmap)
        manifest.set("identifier", "MANIFEST-01")
        manifest.set("version", "1.0")
        manifest.set("{%s}schemaLocation" % NS_XSI, " ".join([
            NS_CP_2004, "imscp_v1p1.xsd",
            NS_ADLCP_2004, "adlcp_v1p3.xsd",
            NS_ADLSEQ_2004, "adlseq_v1p3.xsd",
            NS_ADLNAV_2004, "adlnav_v1p3.xsd",
            NS_IMSSS_2004, "imsss_v1p0.xsd",
        ]))

        metadata = etree.SubElement(manifest, "metadata")
        etree.SubElement(metadata, "schema").text = "ADL SCORM"
        etree.SubElement(metadata, "schemaversion").text = SCHEMA_VERSION_TOKEN["2004"]

        organizations = etree.SubElement(manifest, "organizations")
        organizations.set("default", "ORG-01")
        organization = etree.SubElement(organizations, "organization")
        organization.set("identifier", "ORG-01")
        etree.SubElement(organization, "title").text = training_module.title

        for item_id, res_id, _href, title in self._section_pages(training_module):
            item = etree.SubElement(organization, "item")
            item.set("identifier", item_id)
            item.set("identifierref", res_id)
            item.set("isvisible", "true")
            etree.SubElement(item, "title").text = title

        item = etree.SubElement(organization, "item")
        item.set("identifier", "ITEM-ASSESSMENT")
        item.set("identifierref", "RES-ASSESSMENT")
        item.set("isvisible", "true")
        etree.SubElement(item, "title").text = "Assessment"
        # The pass mark, expressed the way 2004 expresses it: satisfaction is
        # decided by cmi.score.scaled against a normalised measure.  This is
        # the 2004 equivalent of 1.2's <adlcp:masteryscore>.
        sequencing = etree.SubElement(item, "{%s}sequencing" % NS_IMSSS_2004)
        objectives = etree.SubElement(sequencing, "{%s}objectives" % NS_IMSSS_2004)
        primary = etree.SubElement(objectives,
                                   "{%s}primaryObjective" % NS_IMSSS_2004)
        primary.set("objectiveID", "OBJ-ASSESSMENT")
        primary.set("satisfiedByMeasure", "true")
        measure = etree.SubElement(primary,
                                   "{%s}minNormalizedMeasure" % NS_IMSSS_2004)
        measure.text = "{0:.4f}".format(assessment.passing_score / 100.0)

        # Learners may move between pages freely and the LMS may flow them
        # through in order; nothing here gates one page behind another.
        org_sequencing = etree.SubElement(organization,
                                          "{%s}sequencing" % NS_IMSSS_2004)
        control = etree.SubElement(org_sequencing,
                                   "{%s}controlMode" % NS_IMSSS_2004)
        control.set("choice", "true")
        control.set("flow", "true")

        resources = etree.SubElement(manifest, "resources")
        for _item_id, res_id, href, _title in self._section_pages(training_module):
            resource = etree.SubElement(resources, "resource")
            resource.set("identifier", res_id)
            resource.set("type", "webcontent")
            resource.set("{%s}scormType" % NS_ADLCP_2004, "sco")
            resource.set("href", href)
            self._declare_files(resource, href)

        resource = etree.SubElement(resources, "resource")
        resource.set("identifier", "RES-ASSESSMENT")
        resource.set("type", "webcontent")
        resource.set("{%s}scormType" % NS_ADLCP_2004, "sco")
        resource.set("href", "assessment.html")
        self._declare_files(resource, "assessment.html")

        audit = etree.SubElement(resources, "resource")
        audit.set("identifier", "RES-METADATA")
        audit.set("type", "webcontent")
        audit.set("{%s}scormType" % NS_ADLCP_2004, "asset")
        etree.SubElement(audit, "file").set("href", "metadata.json")

        return manifest

    def _create_content_files(self, package_dir: Path, training_module: TrainingModule,
                             assessment: Assessment, approval: Optional[dict] = None):
        """Create HTML content files"""
        # Create CSS file
        self._create_css_file(package_dir)

        # Create content pages for each section
        for idx, section in enumerate(training_module.sections, 1):
            html_content = self._create_content_html(section, training_module.title, idx,
                                                       approval=approval)
            (package_dir / f"content_{idx}.html").write_text(html_content, encoding='utf-8')

        # Create assessment page
        assessment_html = self._create_assessment_html(assessment, training_module.title,
                                                         approval=approval)
        (package_dir / "assessment.html").write_text(assessment_html, encoding='utf-8')

    def _create_css_file(self, package_dir: Path):
        """Create stylesheet for content"""
        css_content = """
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
            line-height: 1.6;
        }
        .container {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1, h2, h3 {
            color: #2c3e50;
        }
        h1 {
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }
        .safety-alert {
            background-color: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 20px 0;
            border-radius: 4px;
        }
        .safety-warnings li {
            color: #d32f2f;
            margin: 10px 0;
        }
        .procedure-steps li {
            margin: 15px 0;
            padding: 10px;
            background-color: #f8f9fa;
            border-radius: 4px;
        }
        .definitions dt {
            font-weight: bold;
            color: #2c3e50;
            margin-top: 15px;
        }
        .definitions dd {
            margin-left: 20px;
            margin-bottom: 10px;
        }
        .navigation {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #dee2e6;
            text-align: center;
        }
        .btn {
            display: inline-block;
            padding: 10px 20px;
            background-color: #3498db;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            margin: 5px;
            cursor: pointer;
            border: none;
            font-size: 16px;
        }
        .btn:hover {
            background-color: #2980b9;
        }
        .question {
            background-color: #f8f9fa;
            padding: 20px;
            margin: 20px 0;
            border-radius: 4px;
            border-left: 4px solid #3498db;
        }
        .options {
            margin: 15px 0;
        }
        .option {
            display: block;
            padding: 10px;
            margin: 8px 0;
            background-color: white;
            border: 2px solid #dee2e6;
            border-radius: 4px;
            cursor: pointer;
        }
        .option:hover {
            background-color: #e9ecef;
        }
        .option input[type="radio"] {
            margin-right: 10px;
        }
        .results {
            display: none;
            padding: 20px;
            margin: 20px 0;
            border-radius: 4px;
        }
        .results.pass {
            background-color: #d4edda;
            border: 2px solid #28a745;
        }
        .results.fail {
            background-color: #f8d7da;
            border: 2px solid #dc3545;
        }
        """
        (package_dir / "styles.css").write_text(css_content)

    def _create_content_html(self, section: dict, title: str, page_num: int,
                            approval: Optional[dict] = None) -> str:
        """Create HTML for a content section"""
        # Add watermark to content
        content_with_watermark = self._add_draft_watermark(section.get('content', ''),
                                                             approval=approval)

        page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{html.escape(title)} - {html.escape(section.get('title', ''))}</title>
    <link rel="stylesheet" href="styles.css">
    <script src="scorm_api.js"></script>
    <script>
        window.onload = function() {{
            initializeSCORM();
        }};

        window.onbeforeunload = function() {{
            finishSCORM();
        }};

        function markComplete() {{
            setComplete();
            alert('Section completed!');
        }}
    </script>
</head>
<body>
    <div class="container">
        <h1>{html.escape(title)}</h1>
        {content_with_watermark}

        <div class="navigation">
            <button class="btn" onclick="markComplete()">Mark as Complete</button>
        </div>
    </div>
</body>
</html>"""
        return page

    def _create_assessment_html(self, assessment: Assessment, title: str,
                               approval: Optional[dict] = None) -> str:
        """Create HTML for assessment.

        INTEGRITY: this page is built from ``assessment.to_learner_dict()`` only.
        The correct-answer index and the explanation never reach the learner.
        Each question carries a public salt and
        sha256(salt + "|" + normalise(correct option text)); the page hashes the
        option the learner selected and compares.  ``src/answer_key.py`` documents
        exactly what that protects against and what it does not - in short, it
        defeats "view source" but not a determined learner with dev tools, and
        server-verified scoring is the M2 fix.

        Options are emitted in the order the Python model holds them, which is
        already deterministically shuffled at generation time with the correct
        answer's position balanced across the assessment.
        """
        # Add watermark (or, once approved, the approval banner in its place)
        watermark = self._add_draft_watermark("", approval=approval)

        learner_payload = assessment.to_learner_dict()

        questions_html = ""
        for number, q in enumerate(assessment.questions, 1):
            questions_html += f"""
            <div class="question" id="question-{number}">
                <p><strong>Question {number}:</strong> {html.escape(q.text)}</p>
                <div class="options">
            """
            for idx, option in enumerate(q.options):
                safe_option = html.escape(option)
                questions_html += f"""
                    <label class="option">
                        <input type="radio" name="q_{html.escape(q.id)}" value="{idx}"
                               data-option="{safe_option}">
                        {safe_option}
                    </label>
                """
            questions_html += """
                </div>
            </div>
            """

        # Escape the characters that could close the surrounding <script> tag.
        # \u003c etc. are valid JSON and valid JavaScript, so a step body that
        # literally contains "</script>" cannot break out of the payload.
        def _payload_json(value):
            return (
                json.dumps(value)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )

        questions_json = _payload_json(learner_payload["questions"])
        interactions_json = _payload_json(build_interaction_metadata(assessment))

        page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{html.escape(title)} - Assessment</title>
    <link rel="stylesheet" href="styles.css">
    <script src="scorm_api.js"></script>
    <script>
{CLIENT_VERIFIER_JS}
    </script>
    <script>
        /* Learner payload. Deliberately carries no answer key and no SME
         * rationale - only the public salt and the salted hash of the right
         * option's normalised text. */
        var questions = {questions_json};
        var passingScore = {assessment.passing_score};

        /* Per-question metadata for the LMS interaction record, entry n ->
         * cmi.interactions.n.*.  Prompt, weighting, interaction type and the
         * response token each rendered option maps to - no answer key, and no
         * expected-response pattern, which is why it can live here at all. */
        var interactionMeta = {interactions_json};

        window.onload = function() {{
            initializeSCORM();
        }};

        /* If the learner navigates away mid-attempt the session still has to
         * be closed, or an LMS may discard everything committed so far. */
        window.onbeforeunload = function() {{
            finishSCORM();
        }};

        function showResults(percentage, missed, outcomes) {{
            var resultsDiv = document.getElementById('results');
            resultsDiv.style.display = 'block';

            /* Per-question evidence goes in FIRST: every hash has resolved by
             * the time showResults runs, and the interactions have to be on
             * the wire before the commit that ends the attempt.  With no LMS
             * present this is a silent no-op and the page still renders. */
            recordInteractions(interactionMeta, outcomes || []);

            setScore(percentage);

            var detail = '';
            if (missed.length) {{
                missed.sort(function(a, b) {{ return a.number - b.number; }});
                var items = '';
                for (var i = 0; i < missed.length; i++) {{
                    var topic = missed[i].topic ? ' &ndash; ' + missed[i].topic : '';
                    items += '<li>Question ' + missed[i].number + topic + '</li>';
                }}
                /* Which questions were missed, so the learner knows what to
                 * re-read. Not the correct answers: this assessment can be
                 * retaken, and handing over the key here would defeat that. */
                detail = '<p>Review the procedure for these questions before ' +
                         'retaking the assessment:</p><ul>' + items + '</ul>';
            }}

            if (percentage >= passingScore) {{
                resultsDiv.className = 'results pass';
                resultsDiv.innerHTML = '<h2>Congratulations! You Passed!</h2>' +
                    '<p>Your score: ' + percentage + '%</p>' +
                    '<p>Passing score: ' + passingScore + '%</p>' + detail;
                /* Success first, then completion: in SCORM 1.2 both live in
                 * cmi.core.lesson_status, and setComplete() deliberately
                 * refuses to overwrite a recorded passed/failed. */
                setPassed();
                setComplete();
            }} else {{
                resultsDiv.className = 'results fail';
                resultsDiv.innerHTML = '<h2>Additional Study Required</h2>' +
                    '<p>Your score: ' + percentage + '%</p>' +
                    '<p>Passing score: ' + passingScore + '%</p>' +
                    '<p>Please review the material and try again.</p>' + detail;
                setFailed();
            }}

            /* The attempt is over and everything is recorded: commit and end
             * the session. Several LMSs discard an attempt that is never
             * terminated. */
            finishSCORM();
        }}

        function submitAssessment() {{
            var button = document.getElementById('submit-button');
            if (button) {{ button.disabled = true; }}

            var totalPoints = 0;
            for (var i = 0; i < questions.length; i++) {{
                totalPoints += questions[i].points;
            }}
            if (!questions.length || !totalPoints) {{
                showResults(0, [], []);
                return;
            }}

            var score = 0;
            var missed = [];
            var resolved = 0;
            /* One slot per question, filled as its hash resolves.  This is
             * what becomes the LMS interaction record, so it carries the
             * rendered position the learner picked and nothing else. */
            var outcomes = new Array(questions.length);

            function settle() {{
                resolved += 1;
                if (resolved < questions.length) {{ return; }}
                var percentage = Math.round((score / totalPoints) * 100);
                showResults(percentage, missed, outcomes);
            }}

            questions.forEach(function (q, index) {{
                var selected = document.querySelector(
                    'input[name="q_' + q.id + '"]:checked');
                if (!selected) {{
                    outcomes[index] = {{ answered: false, option: -1, right: false }};
                    missed.push({{ number: index + 1, topic: q.topic }});
                    settle();
                    return;
                }}
                var position = parseInt(selected.value, 10);
                var chosen = selected.getAttribute('data-option');
                AnswerKey.verify(q.salt, chosen, q.answer_hash, function (correct) {{
                    outcomes[index] = {{
                        answered: true,
                        option: isFinite(position) ? position : -1,
                        right: !!correct
                    }};
                    if (correct) {{
                        score += q.points;
                    }} else {{
                        missed.push({{ number: index + 1, topic: q.topic }});
                    }}
                    settle();
                }});
            }});
        }}
    </script>
</head>
<body>
    <div class="container">
        <h1>{html.escape(assessment.title)}</h1>
        <p>{html.escape(assessment.description)}</p>

        {watermark}

        {questions_html}

        <div class="navigation">
            <button class="btn" id="submit-button" onclick="submitAssessment()">Submit Assessment</button>
        </div>

        <div id="results" class="results"></div>
    </div>
</body>
</html>"""
        return page

    def _create_api_files(self, package_dir: Path):
        """Write ``scorm_api.js``: one wrapper that speaks both run-time APIs.

        A SCO does not get told which SCORM version its LMS implements - it
        discovers it.  SCORM 1.2 exposes an object called ``API`` with
        ``LMSInitialize``/``LMSSetValue``/``LMSCommit``/``LMSFinish`` over the
        ``cmi.core.*`` data model; SCORM 2004 exposes ``API_1484_11`` with
        ``Initialize``/``SetValue``/``Commit``/``Terminate`` over ``cmi.score.*``,
        ``cmi.completion_status`` and ``cmi.success_status``.  This wrapper
        finds whichever is there (walking up to seven frames of ancestors, then
        the opener's, as the specification's pseudo-code does) and maps the
        page's calls onto it.  With no LMS at all every entry point is a no-op
        that returns false, so the page still scores and still renders.
        """
        api_js = """
        /* SCORM run-time wrapper: SCORM 1.2 (API / cmi.core.*) and
         * SCORM 2004 (API_1484_11 / cmi.*), discovered at run time.
         * Exercised against a recording fake LMS in tests/conformance/. */
        var scorm = {
            api: null,          /* the LMS-provided API object */
            version: null,      /* "1.2" | "2004" | null when no LMS is present */
            initialized: false,
            terminated: false
        };

        /* The specification's API discovery limit: give up after seven
         * ancestors rather than climbing a pathological frameset forever. */
        var SCORM_MAX_PARENTS = 7;

        /* Page load.  Every interaction's latency and the session time are
         * measured from here, so one attempt reports one elapsed figure. */
        var SCORM_STARTED_AT = new Date();

        /* cmi.suspend_data is capped at 4096 characters in both bindings. */
        var SCORM_SUSPEND_DATA_MAX = __SUSPEND_DATA_MAX__;

        function findAPIInWindow(win) {
            /* Cross-origin frames throw on property access; that is a "no API
             * here", not a failure of the SCO. */
            try {
                if (win.API_1484_11) {
                    return { api: win.API_1484_11, version: "2004" };
                }
            } catch (e) { /* cross-origin ancestor */ }
            try {
                if (win.API) {
                    return { api: win.API, version: "1.2" };
                }
            } catch (e) { /* cross-origin ancestor */ }
            return null;
        }

        function findAPIUpFrom(win) {
            var current = win;
            var depth = 0;
            while (current && depth <= SCORM_MAX_PARENTS) {
                var found = findAPIInWindow(current);
                if (found) { return found; }
                var next = null;
                try { next = current.parent; } catch (e) { next = null; }
                if (!next || next === current) { return null; }
                current = next;
                depth += 1;
            }
            return null;
        }

        function getAPI() {
            var found = findAPIUpFrom(window);
            if (!found) {
                var opener = null;
                try { opener = window.opener; } catch (e) { opener = null; }
                if (opener) {
                    try {
                        if (!opener.closed) { found = findAPIUpFrom(opener); }
                    } catch (e) { /* cross-origin opener */ }
                }
            }
            scorm.api = found ? found.api : null;
            scorm.version = found ? found.version : null;
            return scorm.api;
        }

        function scormUsable() {
            return !!(scorm.api && scorm.initialized && !scorm.terminated);
        }

        function scormGet(element) {
            if (!scormUsable()) { return ""; }
            var value = (scorm.version === "2004")
                ? scorm.api.GetValue(element)
                : scorm.api.LMSGetValue(element);
            return (value === null || value === undefined) ? "" : String(value);
        }

        function scormSet(element, value) {
            /* Every value crossing the API boundary is a string: the data model
             * is CMIString/CMIDecimal, and an LMS handed a JS number may store
             * "85.00000001" or reject the call outright. */
            if (!scormUsable()) { return false; }
            var result = (scorm.version === "2004")
                ? scorm.api.SetValue(element, String(value))
                : scorm.api.LMSSetValue(element, String(value));
            return String(result) === "true";
        }

        function scormCommit() {
            if (!scormUsable()) { return false; }
            var result = (scorm.version === "2004")
                ? scorm.api.Commit("")
                : scorm.api.LMSCommit("");
            return String(result) === "true";
        }

        function initializeSCORM() {
            if (scorm.initialized) { return true; }
            getAPI();
            if (!scorm.api) { return false; }

            var started = (scorm.version === "2004")
                ? scorm.api.Initialize("")
                : scorm.api.LMSInitialize("");
            if (String(started) !== "true") {
                scorm.api = null;
                scorm.version = null;
                return false;
            }
            scorm.initialized = true;
            scorm.terminated = false;

            /* Mark the attempt in progress WITHOUT overwriting a status the
             * learner already earned.  Setting "incomplete" unconditionally on
             * every launch - which this wrapper used to do - erases a recorded
             * pass the moment the learner reopens the module. */
            if (scorm.version === "2004") {
                var completion = scormGet("cmi.completion_status");
                if (completion === "" || completion === "unknown" ||
                        completion === "not attempted") {
                    scormSet("cmi.completion_status", "incomplete");
                }
            } else {
                var status = scormGet("cmi.core.lesson_status");
                if (status === "" || status === "not attempted") {
                    scormSet("cmi.core.lesson_status", "incomplete");
                }
            }
            scormCommit();
            return true;
        }

        /* ------------------------------------------------------------------
         * Time formats.  The two bindings disagree about every one of them,
         * and an LMS that type-checks will reject the other version's spelling
         * outright, so each is built explicitly rather than reused.
         * ---------------------------------------------------------------- */
        function scormPad(value, width) {
            var text = String(value);
            while (text.length < width) { text = "0" + text; }
            return text;
        }

        function scormElapsedSeconds() {
            var elapsed = (new Date().getTime() - SCORM_STARTED_AT.getTime()) / 1000;
            return (isFinite(elapsed) && elapsed > 0) ? elapsed : 0;
        }

        /* SCORM 1.2 CMITimespan: HHHH:MM:SS.SS, hours zero-padded to four so
         * the field is the same width whatever the attempt took. */
        function scormTimespan12(seconds) {
            var cs = Math.floor(seconds * 100);
            if (!isFinite(cs) || cs < 0) { cs = 0; }
            if (cs > 3599999999) { cs = 3599999999; }
            return scormPad(Math.floor(cs / 360000), 4) + ":" +
                   scormPad(Math.floor((cs % 360000) / 6000), 2) + ":" +
                   scormPad(Math.floor((cs % 6000) / 100), 2) + "." +
                   scormPad(cs % 100, 2);
        }

        /* SCORM 2004 timeinterval(second,10,2): an ISO 8601 duration.  All
         * three components are always emitted so the string is unambiguous
         * and always ends in "S". */
        function scormDuration2004(seconds) {
            var cs = Math.floor(seconds * 100);
            if (!isFinite(cs) || cs < 0) { cs = 0; }
            return "PT" + Math.floor(cs / 360000) + "H" +
                   Math.floor((cs % 360000) / 6000) + "M" +
                   ((cs % 6000) / 100).toFixed(2) + "S";
        }

        /* SCORM 1.2 CMITime: HH:MM:SS, the learner's local wall clock. */
        function scormClock12(when) {
            return scormPad(when.getHours(), 2) + ":" +
                   scormPad(when.getMinutes(), 2) + ":" +
                   scormPad(when.getSeconds(), 2);
        }

        /* SCORM 2004 time(second,10,0): ISO 8601, UTC, no sub-second part. */
        function scormTimestamp2004(when) {
            try {
                return when.toISOString().replace(/\.[0-9]+Z$/, "Z");
            } catch (e) {
                return "";
            }
        }

        /* ------------------------------------------------------------------
         * Per-question evidence.
         *
         * cmi.interactions is what turns "this learner scored 75%" into "this
         * learner answered b to the emergency-stop question and it was wrong",
         * which is the record a regulated buyer's auditor asks for.
         *
         * One element is deliberately absent in both bindings: the one that
         * states the expected answer pattern.  Writing it would mean shipping
         * the key through the learner's browser, which this package does not
         * do (see src/answer_key.py).  The trade-off is written down in
         * docs/SCORM_CONFORMANCE.md rather than left to be discovered.
         * ---------------------------------------------------------------- */
        function scormSuspendAttempt() {
            var raw = scormGet("cmi.suspend_data");
            if (!raw) { return 1; }
            try {
                var previous = JSON.parse(raw);
                var n = parseInt(previous.attempt, 10);
                return (isFinite(n) && n > 0) ? n + 1 : 1;
            } catch (e) {
                return 1;
            }
        }

        /* {attempt: n, responses: {question id: response token}}, clipped to
         * the 4096-character cap by dropping responses from the end - a short
         * record the LMS accepts beats a long one it silently refuses. */
        function scormWriteSuspendData(pairs) {
            var payload = { attempt: scormSuspendAttempt(), responses: {} };
            var kept = [];
            var i;
            for (i = 0; i < pairs.length; i++) {
                payload.responses[pairs[i][0]] = pairs[i][1];
                kept.push(pairs[i][0]);
            }
            var text = JSON.stringify(payload);
            while (text.length > SCORM_SUSPEND_DATA_MAX && kept.length) {
                delete payload.responses[kept.pop()];
                text = JSON.stringify(payload);
            }
            if (text.length > SCORM_SUSPEND_DATA_MAX) { return false; }
            return scormSet("cmi.suspend_data", text);
        }

        function recordInteractions(meta, outcomes) {
            if (!scormUsable()) { return false; }
            if (!meta || !meta.length) { return false; }

            var is2004 = (scorm.version === "2004");
            var now = new Date();
            var latency = is2004 ? scormDuration2004(scormElapsedSeconds())
                                 : scormTimespan12(scormElapsedSeconds());
            var when = is2004 ? scormTimestamp2004(now) : scormClock12(now);
            var pairs = [];

            for (var i = 0; i < meta.length; i++) {
                var item = meta[i];
                var outcome = (outcomes && outcomes[i]) ? outcomes[i] : null;
                var base = "cmi.interactions." + i + ".";
                /* id first: an LMS creates the interaction on that write. */
                scormSet(base + "id", item.id);
                scormSet(base + "type", item.type);

                var table = is2004 ? item.responses2004 : item.responses12;
                var response = "";
                if (outcome && outcome.answered && table &&
                        outcome.option >= 0 && outcome.option < table.length) {
                    response = table[outcome.option];
                }

                var result;
                if (!outcome || !outcome.answered) {
                    result = "neutral";
                } else if (outcome.right) {
                    result = "correct";
                } else {
                    /* 1.2 spells it "wrong"; 2004 spells it "incorrect". */
                    result = is2004 ? "incorrect" : "wrong";
                }

                /* An unanswered question gets no response element at all: the
                 * empty string is not a legal choice/true-false response, and
                 * result "neutral" already says the learner skipped it. */
                if (response !== "") {
                    scormSet(base + (is2004 ? "learner_response"
                                            : "student_response"), response);
                    pairs.push([item.id, response]);
                }
                scormSet(base + "result", result);
                scormSet(base + "weighting", item.weighting);
                scormSet(base + "latency", latency);

                if (is2004) {
                    scormSet(base + "timestamp", when);
                    if (item.description) {
                        scormSet(base + "description", item.description);
                    }
                    if (item.objective) {
                        scormSet(base + "objectives.0.id", item.objective);
                    }
                } else {
                    /* 1.2 has no description and no timestamp - only "time",
                     * and every interaction element in 1.2 is write-only. */
                    scormSet(base + "time", when);
                }
            }

            scormWriteSuspendData(pairs);
            return true;
        }

        function finishSCORM() {
            if (!scormUsable()) { return false; }
            /* How long the learner had the SCO open, in this version's
             * spelling, before the attempt is closed. */
            if (scorm.version === "2004") {
                scormSet("cmi.session_time",
                         scormDuration2004(scormElapsedSeconds()));
                scormSet("cmi.exit", "normal");
            } else {
                scormSet("cmi.core.session_time",
                         scormTimespan12(scormElapsedSeconds()));
                /* "" is the 1.2 vocabulary for an ordinary end of session -
                 * not a suspend, not a time-out, not a logout. */
                scormSet("cmi.core.exit", "");
            }
            scormCommit();
            /* Set the flag before the call so a re-entrant unload handler
             * cannot terminate twice. */
            scorm.terminated = true;
            var result = (scorm.version === "2004")
                ? scorm.api.Terminate("")
                : scorm.api.LMSFinish("");
            return String(result) === "true";
        }

        function setComplete() {
            if (!scormUsable()) { return false; }
            if (scorm.version === "2004") {
                scormSet("cmi.completion_status", "completed");
            } else {
                /* In SCORM 1.2 one element carries both completion and
                 * success, so "completed" must never overwrite a
                 * passed/failed the assessment has already recorded. */
                var status = scormGet("cmi.core.lesson_status");
                if (status !== "passed" && status !== "failed") {
                    scormSet("cmi.core.lesson_status", "completed");
                }
            }
            return scormCommit();
        }

        function setPassed() {
            if (!scormUsable()) { return false; }
            if (scorm.version === "2004") {
                scormSet("cmi.success_status", "passed");
                scormSet("cmi.completion_status", "completed");
            } else {
                scormSet("cmi.core.lesson_status", "passed");
            }
            return scormCommit();
        }

        function setFailed() {
            if (!scormUsable()) { return false; }
            if (scorm.version === "2004") {
                scormSet("cmi.success_status", "failed");
                scormSet("cmi.completion_status", "completed");
            } else {
                scormSet("cmi.core.lesson_status", "failed");
            }
            return scormCommit();
        }

        function setScore(score) {
            if (!scormUsable()) { return false; }
            var raw = Number(score);
            if (!isFinite(raw)) { raw = 0; }
            if (raw < 0) { raw = 0; }
            if (raw > 100) { raw = 100; }

            if (scorm.version === "2004") {
                scormSet("cmi.score.min", "0");
                scormSet("cmi.score.max", "100");
                scormSet("cmi.score.raw", String(raw));
                /* 2004 rolls up on the normalised measure, not on raw. */
                scormSet("cmi.score.scaled", String(raw / 100));
            } else {
                scormSet("cmi.core.score.min", "0");
                scormSet("cmi.core.score.max", "100");
                scormSet("cmi.core.score.raw", String(raw));
            }
            return scormCommit();
        }
        """
        api_js = api_js.replace("__SUSPEND_DATA_MAX__", str(SUSPEND_DATA_MAX))
        (package_dir / "scorm_api.js").write_text(api_js)

    def _create_zip(self, source_dir: Path, output_zip: Path):
        """Create ZIP file from package directory"""
        with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(source_dir)
                    zipf.write(file_path, arcname)

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for file system"""
        # Remove invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        return filename.strip()

    def _add_draft_watermark(self, html_content: str, approval: Optional[dict] = None) -> str:
        """
        Prepend a DRAFT watermark to training content, or - once a named
        human has approved the job - an approval banner in its place.

        Args:
            html_content: Original HTML content
            approval: Optional approval record (``approved_by``, ``role``,
                ``approved_at``, plus whatever else the caller stores; only
                these three are shown). All values are HTML-escaped, since an
                approver's name is operator-entered text, not trusted markup.
                ``None`` (the default) keeps today's DRAFT watermark exactly
                as before.

        Returns:
            HTML content with the watermark/banner prepended
        """
        if approval:
            return self._add_approval_banner(html_content, approval)

        watermark = """
    <div style="border: 3px solid #ff9800; background: #fff3cd; padding: 15px; margin: 10px 0; border-radius: 5px;">
        <h3 style="color: #ff6f00; margin: 0 0 10px 0;">⚠️ DRAFT TRAINING - REVIEW REQUIRED</h3>
        <p style="margin: 5px 0;"><strong>This training content was auto-generated and requires review by:</strong></p>
        <ul style="margin: 5px 0;">
            <li>Subject Matter Expert - Technical accuracy</li>
            <li>Quality Assurance - Compliance verification</li>
            <li>Training Manager - Learning effectiveness</li>
        </ul>
        <p style="margin: 10px 0 0 0; font-size: 0.9em;">
            Generated: {date} | Tool: Training Creator (Non-Validated) |
            <strong>Your validated LMS will maintain all training records</strong>
        </p>
    </div>
    """.format(date=datetime.now().strftime("%Y-%m-%d %H:%M"))

        return watermark + html_content

    def _add_approval_banner(self, html_content: str, approval: dict) -> str:
        """Build the approval banner that replaces the DRAFT watermark.

        Nothing here is trusted markup: ``approved_by``, ``role`` and the
        formatted date are all HTML-escaped before being embedded.
        """
        approved_by = html.escape(str(approval.get("approved_by", "")))
        role = html.escape(str(approval.get("role", "")))
        approved_at = approval.get("approved_at", "")
        display_date = str(approved_at)
        try:
            parsed = datetime.fromisoformat(str(approved_at).replace("Z", "+00:00"))
            display_date = parsed.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
        display_date = html.escape(display_date)

        banner = """
    <div style="border: 3px solid #28a745; background: #d4edda; padding: 15px; margin: 10px 0; border-radius: 5px;">
        <h3 style="color: #1e7e34; margin: 0 0 10px 0;">✅ APPROVED TRAINING</h3>
        <p style="margin: 5px 0;"><strong>Approved by {approved_by} ({role}) on {date}</strong></p>
        <p style="margin: 10px 0 0 0; font-size: 0.9em;">
            Tool: Training Creator (Non-Validated) |
            <strong>Your validated LMS will maintain all training records</strong>
        </p>
    </div>
    """.format(approved_by=approved_by, role=role, date=display_date)

        return banner + html_content

    def _create_metadata_file(self, package_dir: Path, training_module: TrainingModule,
                             assessment: Assessment, source_info: str = "Unknown",
                             approval: Optional[dict] = None):
        """
        Create metadata.json for full transparency

        This file provides complete transparency about how the training
        was generated, supporting ISO 13485 and FDA audit requirements.

        Args:
            package_dir: Directory where SCORM package is being created
            training_module: Generated training module
            assessment: Generated assessment
            source_info: Information about source document
            approval: Optional approval record. When present it is embedded
                verbatim under the "approval" key and ``review_status``
                switches from DRAFT to APPROVED. ``None`` (the default)
                leaves metadata.json exactly as before.
        """
        try:
            from .medical_device_config import MEDICAL_DEVICE_CONFIG

            metadata = {
                "generation_info": MEDICAL_DEVICE_CONFIG['transparency_metadata'],
                "source_file": source_info,
                "content_created": {
                    "training_sections": len(training_module.sections),
                    "assessment_questions": len(assessment.questions),
                    "assessment_questions_requested": getattr(
                        assessment, "requested_questions", len(assessment.questions)),
                    "assessment_notes": list(getattr(assessment, "notes", [])),
                    "passing_score": assessment.passing_score,
                    "estimated_duration_minutes": training_module.estimated_duration
                },
                # Stated up front so an auditor reads it here rather than
                # discovering it. See src/answer_key.py.
                "assessment_integrity": MEDICAL_DEVICE_CONFIG.get(
                    "assessment_integrity", {}),
                "review_status": "APPROVED" if approval else "DRAFT - Requires SME Review",
                "lms_notes": "Import to validated LMS for training record management per 21 CFR 820.25",
                "generated_timestamp": datetime.now().isoformat(),
                "compliance_context": {
                    "iso_13485": "Training development tool for ISO 13485:2016 section 6.2",
                    "cfr_820_25": "Personnel training content generator - LMS maintains records",
                    "validation_status": "Non-validated content generation tool"
                }
            }
            if approval:
                metadata["approval"] = approval

            with open(package_dir / 'metadata.json', 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

        except ImportError:
            # If medical_device_config not available, create basic metadata
            metadata = {
                "generation_info": {
                    "tool_name": "Training Creator",
                    "validation_status": "Non-validated system"
                },
                "source_file": source_info,
                "content_created": {
                    "training_sections": len(training_module.sections),
                    "assessment_questions": len(assessment.questions)
                },
                "review_status": "APPROVED" if approval else "DRAFT",
                "generated_timestamp": datetime.now().isoformat()
            }
            if approval:
                metadata["approval"] = approval

            with open(package_dir / 'metadata.json', 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
