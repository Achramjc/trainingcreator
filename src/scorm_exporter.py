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

from .answer_key import CLIENT_VERIFIER_JS
from .generator import TrainingModule
from .assessments import Assessment


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
        """Create imsmanifest.xml file"""
        # Create manifest root
        nsmap = {
            None: "http://www.imsproject.org/xsd/imscp_rootv1p1p2",
            "adlcp": "http://www.adlnet.org/xsd/adlcp_rootv1p2",
            "xsi": "http://www.w3.org/2001/XMLSchema-instance"
        }

        manifest = etree.Element("manifest", nsmap=nsmap)
        manifest.set("identifier", "MANIFEST-01")
        manifest.set("version", "1.0")

        # Metadata
        metadata = etree.SubElement(manifest, "metadata")
        schema = etree.SubElement(metadata, "schema")
        schema.text = "ADL SCORM"
        schemaversion = etree.SubElement(metadata, "schemaversion")
        schemaversion.text = self.scorm_version

        # Organizations
        organizations = etree.SubElement(manifest, "organizations")
        organizations.set("default", "ORG-01")

        organization = etree.SubElement(organizations, "organization")
        organization.set("identifier", "ORG-01")

        title = etree.SubElement(organization, "title")
        title.text = training_module.title

        # Add items for each section
        for idx, section in enumerate(training_module.sections, 1):
            item = etree.SubElement(organization, "item")
            item.set("identifier", f"ITEM-{idx}")
            item.set("identifierref", f"RES-{idx}")
            item_title = etree.SubElement(item, "title")
            item_title.text = section.get('title', f"Section {idx}")

        # Add assessment item
        assessment_item = etree.SubElement(organization, "item")
        assessment_item.set("identifier", "ITEM-ASSESSMENT")
        assessment_item.set("identifierref", "RES-ASSESSMENT")
        assessment_title = etree.SubElement(assessment_item, "title")
        assessment_title.text = "Assessment"

        # Resources
        resources = etree.SubElement(manifest, "resources")

        # Add resource for each section
        for idx, section in enumerate(training_module.sections, 1):
            resource = etree.SubElement(resources, "resource")
            resource.set("identifier", f"RES-{idx}")
            resource.set("type", "webcontent")
            resource.set("{http://www.adlnet.org/xsd/adlcp_rootv1p2}scormtype", "sco")
            resource.set("href", f"content_{idx}.html")

            file_elem = etree.SubElement(resource, "file")
            file_elem.set("href", f"content_{idx}.html")

        # Add assessment resource
        assessment_resource = etree.SubElement(resources, "resource")
        assessment_resource.set("identifier", "RES-ASSESSMENT")
        assessment_resource.set("type", "webcontent")
        assessment_resource.set("{http://www.adlnet.org/xsd/adlcp_rootv1p2}scormtype", "sco")
        assessment_resource.set("href", "assessment.html")

        assessment_file = etree.SubElement(assessment_resource, "file")
        assessment_file.set("href", "assessment.html")

        # Write manifest
        tree = etree.ElementTree(manifest)
        manifest_path = package_dir / "imsmanifest.xml"
        tree.write(str(manifest_path), pretty_print=True, xml_declaration=True,
                  encoding='UTF-8')

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
        questions_json = (
            json.dumps(learner_payload["questions"])
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )

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

        window.onload = function() {{
            initializeSCORM();
        }};

        function showResults(percentage, missed) {{
            var resultsDiv = document.getElementById('results');
            resultsDiv.style.display = 'block';
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
                setComplete();
                setPassed();
            }} else {{
                resultsDiv.className = 'results fail';
                resultsDiv.innerHTML = '<h2>Additional Study Required</h2>' +
                    '<p>Your score: ' + percentage + '%</p>' +
                    '<p>Passing score: ' + passingScore + '%</p>' +
                    '<p>Please review the material and try again.</p>' + detail;
                setFailed();
            }}
        }}

        function submitAssessment() {{
            var button = document.getElementById('submit-button');
            if (button) {{ button.disabled = true; }}

            var totalPoints = 0;
            for (var i = 0; i < questions.length; i++) {{
                totalPoints += questions[i].points;
            }}
            if (!questions.length || !totalPoints) {{
                showResults(0, []);
                return;
            }}

            var score = 0;
            var missed = [];
            var resolved = 0;

            function settle() {{
                resolved += 1;
                if (resolved < questions.length) {{ return; }}
                var percentage = Math.round((score / totalPoints) * 100);
                showResults(percentage, missed);
            }}

            questions.forEach(function (q, index) {{
                var selected = document.querySelector(
                    'input[name="q_' + q.id + '"]:checked');
                if (!selected) {{
                    missed.push({{ number: index + 1, topic: q.topic }});
                    settle();
                    return;
                }}
                var chosen = selected.getAttribute('data-option');
                AnswerKey.verify(q.salt, chosen, q.answer_hash, function (correct) {{
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
        """Create SCORM API wrapper JavaScript"""
        api_js = """
        // SCORM API Wrapper
        var scorm = {
            version: null,
            api: null
        };

        function initializeSCORM() {
            scorm.api = getAPI();
            if (scorm.api) {
                scorm.api.LMSInitialize("");
                scorm.api.LMSSetValue("cmi.core.lesson_status", "incomplete");
            }
        }

        function finishSCORM() {
            if (scorm.api) {
                scorm.api.LMSCommit("");
                scorm.api.LMSFinish("");
            }
        }

        function setComplete() {
            if (scorm.api) {
                scorm.api.LMSSetValue("cmi.core.lesson_status", "completed");
                scorm.api.LMSCommit("");
            }
        }

        function setPassed() {
            if (scorm.api) {
                scorm.api.LMSSetValue("cmi.core.lesson_status", "passed");
                scorm.api.LMSCommit("");
            }
        }

        function setFailed() {
            if (scorm.api) {
                scorm.api.LMSSetValue("cmi.core.lesson_status", "failed");
                scorm.api.LMSCommit("");
            }
        }

        function setScore(score) {
            if (scorm.api) {
                scorm.api.LMSSetValue("cmi.core.score.raw", score.toString());
                scorm.api.LMSSetValue("cmi.core.score.min", "0");
                scorm.api.LMSSetValue("cmi.core.score.max", "100");
                scorm.api.LMSCommit("");
            }
        }

        function getAPI() {
            var api = null;

            // Check current window
            if (window.API) {
                return window.API;
            }

            // Check parent windows
            var parent = window.parent;
            while (parent && parent != window) {
                if (parent.API) {
                    return parent.API;
                }
                parent = parent.parent;
            }

            // Check opener
            if (window.opener && window.opener.API) {
                return window.opener.API;
            }

            return null;
        }
        """
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
