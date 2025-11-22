"""
Generate transparency reports for auditor review

This module creates detailed reports showing exactly how training content
was generated, supporting ISO 13485 and FDA audit requirements.
"""

import json
from datetime import datetime
from pathlib import Path
import hashlib


def generate_transparency_report(sop_content, training_module, assessment, source_file):
    """
    Generate report showing exactly how training was created

    Args:
        sop_content: Parsed SOP content
        training_module: Generated training module
        assessment: Generated assessment
        source_file: Path to source document

    Returns:
        Dictionary containing complete transparency report
    """

    report = {
        "report_title": "Training Generation Transparency Report",
        "generated_timestamp": datetime.now().isoformat(),
        "disclaimer": "DRAFT CONTENT - Requires SME and Quality Review",

        "source_document": {
            "filename": Path(source_file).name if source_file else "Unknown",
            "md5_hash": calculate_file_hash(source_file),
            "parsing_method": "Automated text extraction and pattern matching",
            "sections_found": {
                "title": sop_content.title or "Not identified",
                "purpose": "Found" if sop_content.purpose else "Not found - manual review required",
                "scope": "Found" if sop_content.scope else "Not found - manual review required",
                "procedures": f"{len(sop_content.procedures)} steps identified",
                "safety_warnings": f"{len(sop_content.safety_warnings)} warnings identified",
                "definitions": f"{len(sop_content.definitions)} terms identified"
            }
        },

        "generation_process": {
            "content_generation": {
                "method": "Template-based restructuring of source content",
                "sections_created": len(training_module.sections),
                "learning_objectives_generated": len(training_module.learning_objectives),
                "estimated_duration": f"{training_module.estimated_duration} minutes"
            },
            "assessment_generation": {
                "method": "Rule-based question generation from identified content",
                "total_questions": len(assessment.questions),
                "question_types": count_question_types(assessment),
                "passing_score": assessment.passing_score,
                "medical_device_questions_added": 2  # Required compliance questions
            }
        },

        "review_checklist": {
            "technical_accuracy": "[ ] Subject Matter Expert review required",
            "compliance_alignment": "[ ] Quality Assurance review required",
            "effectiveness_criteria": "[ ] Training Manager review required",
            "content_completeness": "[ ] All critical steps included",
            "assessment_validity": "[ ] Questions accurately test knowledge"
        },

        "limitations": [
            "Automated parsing may miss context-dependent information",
            "Complex procedures may require additional clarification",
            "Visual elements (diagrams, images) not processed",
            "Regulatory references should be independently verified",
            "Assessment questions are suggestions requiring validation"
        ],

        "usage_instructions": "This DRAFT training must be reviewed and approved before use in production training"
    }

    return report


def calculate_file_hash(filepath):
    """
    Calculate MD5 hash of source file for traceability

    Args:
        filepath: Path to file

    Returns:
        MD5 hash string or error message
    """
    if not filepath:
        return "No source file provided"

    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        return f"Could not calculate: {str(e)}"


def count_question_types(assessment):
    """
    Count different question types in assessment

    Args:
        assessment: Assessment object

    Returns:
        Dictionary of question type counts
    """
    types = {}
    for q in assessment.questions:
        q_type = q.type
        types[q_type] = types.get(q_type, 0) + 1
    return types


def create_html_report(report_data, output_path):
    """
    Create HTML version of transparency report

    Args:
        report_data: Report dictionary from generate_transparency_report
        output_path: Path to save HTML report
    """
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{report_data['report_title']}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        .warning {{
            background: #fff3cd;
            border: 2px solid #ffc107;
            padding: 15px;
            margin: 20px 0;
            border-radius: 5px;
        }}
        .section {{
            margin: 20px 0;
            padding: 15px;
            border-left: 4px solid #007bff;
            background: #f8f9fa;
        }}
        .checklist {{
            background: #e8f5e9;
            padding: 15px;
            border-left: 4px solid #4caf50;
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #34495e;
            margin-top: 20px;
        }}
        pre {{
            background: white;
            padding: 10px;
            border-radius: 4px;
            overflow-x: auto;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 10px 0;
        }}
        td, th {{
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }}
        th {{
            background-color: #007bff;
            color: white;
        }}
        ul {{
            line-height: 1.8;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 2px solid #ddd;
            color: #666;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <h1>{report_data['report_title']}</h1>

    <div class="warning">
        <h3 style="margin-top: 0;">⚠️ {report_data['disclaimer']}</h3>
        <p><strong>This training content was automatically generated and requires formal review before use.</strong></p>
    </div>

    <div class="section">
        <h2>Source Document Analysis</h2>
        <pre>{json.dumps(report_data['source_document'], indent=2)}</pre>
    </div>

    <div class="section">
        <h2>Content Generation Process</h2>
        <h3>Training Content</h3>
        <pre>{json.dumps(report_data['generation_process']['content_generation'], indent=2)}</pre>

        <h3>Assessment Generation</h3>
        <pre>{json.dumps(report_data['generation_process']['assessment_generation'], indent=2)}</pre>
    </div>

    <div class="checklist">
        <h2>✓ Required Reviews Before Use</h2>
        <ul>
            {chr(10).join(f"<li>{item}</li>" for item in report_data['review_checklist'].values())}
        </ul>
    </div>

    <div class="section">
        <h2>Known Limitations and Considerations</h2>
        <ul>
            {chr(10).join(f"<li>{limitation}</li>" for limitation in report_data['limitations'])}
        </ul>
    </div>

    <div class="section">
        <h2>Usage Instructions</h2>
        <p><strong>{report_data['usage_instructions']}</strong></p>
        <p>This tool is a <strong>training development aid</strong>, not a validated system.
        All training records and compliance documentation must be maintained in your validated LMS.</p>
    </div>

    <div class="footer">
        <p><strong>Report Generated:</strong> {report_data['generated_timestamp']}</p>
        <p><strong>For ISO 13485 / 21 CFR 820 Compliance:</strong> This report provides
        transparency into the automated training generation process to support quality
        system audits and regulatory inspections.</p>
    </div>
</body>
</html>
"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)


def create_json_report(report_data, output_path):
    """
    Create JSON version of transparency report

    Args:
        report_data: Report dictionary from generate_transparency_report
        output_path: Path to save JSON report
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
