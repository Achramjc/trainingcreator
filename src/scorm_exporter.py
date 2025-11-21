"""
SCORM Exporter - Export training content to SCORM-compliant packages
"""

import os
import json
import zipfile
from pathlib import Path
from typing import Optional
from datetime import datetime
from lxml import etree

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
                       package_name: Optional[str] = None) -> str:
        """
        Create a SCORM package from training content

        Args:
            training_module: Training content
            assessment: Assessment questions
            output_path: Directory to create package in
            package_name: Optional custom package name

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
        self._create_content_files(package_dir, training_module, assessment)
        self._create_api_files(package_dir)

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
                             assessment: Assessment):
        """Create HTML content files"""
        # Create CSS file
        self._create_css_file(package_dir)

        # Create content pages for each section
        for idx, section in enumerate(training_module.sections, 1):
            html_content = self._create_content_html(section, training_module.title, idx)
            (package_dir / f"content_{idx}.html").write_text(html_content, encoding='utf-8')

        # Create assessment page
        assessment_html = self._create_assessment_html(assessment, training_module.title)
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

    def _create_content_html(self, section: dict, title: str, page_num: int) -> str:
        """Create HTML for a content section"""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title} - {section.get('title', '')}</title>
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
        <h1>{title}</h1>
        {section.get('content', '')}

        <div class="navigation">
            <button class="btn" onclick="markComplete()">Mark as Complete</button>
        </div>
    </div>
</body>
</html>"""
        return html

    def _create_assessment_html(self, assessment: Assessment, title: str) -> str:
        """Create HTML for assessment"""
        questions_html = ""
        for q in assessment.questions:
            questions_html += f"""
            <div class="question">
                <p><strong>Question {assessment.questions.index(q) + 1}:</strong> {q.text}</p>
                <div class="options">
            """
            for idx, option in enumerate(q.options):
                questions_html += f"""
                    <label class="option">
                        <input type="radio" name="q{q.id}" value="{idx}">
                        {option}
                    </label>
                """
            questions_html += """
                </div>
            </div>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title} - Assessment</title>
    <link rel="stylesheet" href="styles.css">
    <script src="scorm_api.js"></script>
    <script>
        const questions = {json.dumps([q.to_dict() for q in assessment.questions])};
        const passingScore = {assessment.passing_score};

        window.onload = function() {{
            initializeSCORM();
        }};

        function submitAssessment() {{
            let score = 0;
            let totalPoints = 0;

            questions.forEach((q, index) => {{
                totalPoints += q.points;
                const selected = document.querySelector('input[name="q' + q.id + '"]:checked');
                if (selected && parseInt(selected.value) === q.correct_answer) {{
                    score += q.points;
                }}
            }});

            const percentage = Math.round((score / totalPoints) * 100);
            setScore(percentage);

            const resultsDiv = document.getElementById('results');
            resultsDiv.style.display = 'block';

            if (percentage >= passingScore) {{
                resultsDiv.className = 'results pass';
                resultsDiv.innerHTML = '<h2>Congratulations! You Passed!</h2><p>Your score: ' + percentage + '%</p><p>Passing score: ' + passingScore + '%</p>';
                setComplete();
                setPassed();
            }} else {{
                resultsDiv.className = 'results fail';
                resultsDiv.innerHTML = '<h2>Additional Study Required</h2><p>Your score: ' + percentage + '%</p><p>Passing score: ' + passingScore + '%</p><p>Please review the material and try again.</p>';
                setFailed();
            }}
        }}
    </script>
</head>
<body>
    <div class="container">
        <h1>{assessment.title}</h1>
        <p>{assessment.description}</p>

        {questions_html}

        <div class="navigation">
            <button class="btn" onclick="submitAssessment()">Submit Assessment</button>
        </div>

        <div id="results" class="results"></div>
    </div>
</body>
</html>"""
        return html

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
