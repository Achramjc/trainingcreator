"""
Generate transparency reports for auditor review

This module creates detailed reports showing exactly how training content
was generated, supporting ISO 13485 and FDA audit requirements.

M1 adds a **citation coverage** block: for every generated learning
objective, section, and assessment question, we attempt to resolve its
``source_ref`` back to a line span in the parsed SOP (``sop_content.lines``)
and report what fraction actually resolves. This is the measurable half of
GOAL.md criterion 1 ("every generated sentence linked to a source line").
"""

import json
from datetime import datetime
from pathlib import Path
from html import escape as _escape
import hashlib


REQUIRED_APPROVAL_KEYS = ("approved_by", "role", "approved_at", "notes", "edits_count")


# ---------------------------------------------------------------------------
# Citation resolution
# ---------------------------------------------------------------------------

def _valid_span(span, sop_content):
    """Return `span` unchanged if it is a well-formed, in-bounds [s, e] pair
    against `sop_content.lines`, else None."""
    if not span or len(span) != 2:
        return None
    s, e = span
    lines = getattr(sop_content, "lines", None) or []
    if lines and isinstance(s, int) and isinstance(e, int) and 1 <= s <= e <= len(lines):
        return [s, e]
    return None


def _resolve_span_ref(source_ref, sop_content):
    """Resolve a `{"span": [s, e], ...}`-shaped source_ref (objectives,
    section citations). Returns (span_or_None, attempted=True) -- these
    refs always carry an intended span, so a missing/invalid one is a real
    grounding defect, never "not applicable"."""
    span = (source_ref or {}).get("span")
    return _valid_span(span, sop_content), True


def _resolve_question_ref(source_ref, sop_content):
    """Resolve an assessment Question's `source_ref` (see src/assessments.py
    `Question.source_ref`) to a span, using the SOP's provenance and
    procedure list -- this shape predates and differs from the objective/
    section `source_ref` shape (no direct "span" key for most kinds).

    Returns (span_or_None, attempted): `attempted` is False for kinds that
    are legitimately not grounded in this document (e.g. medical-device
    required compliance questions, which come from fixed regulatory text),
    so those count as "not grounded" rather than "unresolvable".
    """
    source_ref = source_ref or {}
    kind = source_ref.get("kind")
    provenance = getattr(sop_content, "provenance", None) or {}
    procedures = getattr(sop_content, "procedures", None) or []

    if kind == "purpose":
        return _valid_span(provenance.get("purpose"), sop_content), True
    if kind == "scope":
        return _valid_span(provenance.get("scope"), sop_content), True
    if kind == "definition":
        term = source_ref.get("term")
        span = (provenance.get("definitions") or {}).get(term)
        return _valid_span(span, sop_content), True
    if kind == "step":
        span = _valid_span(source_ref.get("source_lines"), sop_content)
        if span:
            return span, True
        step_number = source_ref.get("step_number")
        for proc in procedures:
            if str(proc.get("step_number")) == str(step_number):
                return _valid_span(proc.get("source_lines"), sop_content), True
        return None, True
    if kind == "sequence":
        target = source_ref.get("answer_step") or source_ref.get("after_step")
        for proc in procedures:
            if str(proc.get("step_number")) == str(target):
                return _valid_span(proc.get("source_lines"), sop_content), True
        return None, True
    if kind in ("safety", "safety_contradiction"):
        idx = source_ref.get("warning_index")
        spans = provenance.get("safety_warnings") or []
        if isinstance(idx, int) and 0 <= idx < len(spans):
            return _valid_span(spans[idx], sop_content), True
        return None, True

    # md_required and any unrecognised kind: intentionally not grounded in
    # the source document (fixed regulatory boilerplate).
    return None, False


def _coverage_bucket(items, sop_content, excerpt_context=0):
    """
    Build one citation-coverage bucket.

    `items` is a list of (label, source_ref, span_or_None, attempted) tuples.
    Returns a dict with total/cited/unresolvable/not_grounded/coverage_pct
    and a per-item breakdown (including the cited excerpt where resolvable).
    """
    total = len(items)
    cited = 0
    unresolvable = 0
    not_grounded = 0
    detail = []

    for label, source_ref, span, attempted in items:
        entry = {"label": label, "source_ref": source_ref or {}}
        if span is not None:
            cited += 1
            entry["status"] = "cited"
            entry["span"] = span
            if hasattr(sop_content, "excerpt"):
                entry["excerpt"] = sop_content.excerpt(span, context=excerpt_context)
        elif attempted:
            unresolvable += 1
            entry["status"] = "unresolvable"
        else:
            not_grounded += 1
            entry["status"] = "not_grounded"
        detail.append(entry)

    coverage_pct = round((cited / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "cited": cited,
        "unresolvable": unresolvable,
        "not_grounded": not_grounded,
        "coverage_pct": coverage_pct,
        "items": detail,
    }


def _build_citation_coverage(sop_content, training_module, assessment):
    objective_items = []
    for obj in getattr(training_module, "objectives", None) or []:
        span, attempted = _resolve_span_ref(obj.get("source_ref"), sop_content)
        objective_items.append((obj.get("text", ""), obj.get("source_ref"), span, attempted))

    section_items = []
    for section in getattr(training_module, "sections", None) or []:
        section_id = section.get("id", "")
        for i, ref in enumerate(section.get("citations") or []):
            span, attempted = _resolve_span_ref(ref, sop_content)
            section_items.append((f"{section_id}[{i}]", ref, span, attempted))

    question_items = []
    for q in getattr(assessment, "questions", None) or []:
        span, attempted = _resolve_question_ref(getattr(q, "source_ref", None), sop_content)
        question_items.append((q.id, q.source_ref, span, attempted))

    objectives_bucket = _coverage_bucket(objective_items, sop_content)
    sections_bucket = _coverage_bucket(section_items, sop_content)
    questions_bucket = _coverage_bucket(question_items, sop_content)

    overall_total = objectives_bucket["total"] + sections_bucket["total"] + questions_bucket["total"]
    overall_cited = objectives_bucket["cited"] + sections_bucket["cited"] + questions_bucket["cited"]
    overall_pct = round((overall_cited / overall_total) * 100, 1) if overall_total else 0.0

    return {
        "objectives": objectives_bucket,
        "sections": sections_bucket,
        "assessment_questions": questions_bucket,
        "overall": {
            "total": overall_total,
            "cited": overall_cited,
            "coverage_pct": overall_pct,
        },
    }


def _build_approval_block(approval):
    """Normalise an `approval` dict to exactly REQUIRED_APPROVAL_KEYS."""
    return {
        "approved_by": approval.get("approved_by", ""),
        "role": approval.get("role", ""),
        "approved_at": approval.get("approved_at", ""),
        "notes": approval.get("notes", ""),
        "edits_count": approval.get("edits_count", 0),
    }


def generate_transparency_report(sop_content, training_module, assessment, source_file, approval=None):
    """
    Generate report showing exactly how training was created

    Args:
        sop_content: Parsed SOP content
        training_module: Generated training module
        assessment: Generated assessment
        source_file: Path to source document
        approval: Optional dict with keys approved_by, role, approved_at,
            notes, edits_count. When given, an "Approval" block is added to
            the report. When omitted, the report states plainly that the
            content is an unreviewed draft. Nothing in this module writes or
            fetches this value -- the caller (the SME review workflow) is
            responsible for supplying it once a human has actually approved.

    Returns:
        Dictionary containing complete transparency report
    """

    citation_coverage = _build_citation_coverage(sop_content, training_module, assessment)

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
                "requested_questions": getattr(assessment, "requested_questions", len(assessment.questions)),
                "question_types": count_question_types(assessment),
                "passing_score": assessment.passing_score,
                "medical_device_questions_added": 2,  # Required compliance questions
                "notes": list(getattr(assessment, "notes", None) or []),
            }
        },

        "citation_coverage": citation_coverage,

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

    if approval:
        report["approval"] = _build_approval_block(approval)
    else:
        report["approval"] = {"status": "unreviewed_draft"}
        report["approval_statement"] = (
            "This content has NOT been approved by a named human reviewer. "
            "It is an unreviewed draft and must not be used for training records."
        )

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


def _render_citation_bucket_html(title, bucket):
    """Render one citation-coverage bucket as a collapsible table.

    All document-derived text (labels, excerpts) is untrusted and is
    HTML-escaped before being written into the page.
    """
    rows = []
    for item in bucket["items"]:
        label = _escape(str(item.get("label", "")))
        status = item.get("status", "")
        span = item.get("span")
        span_text = f"{span[0]}–{span[1]}" if span else "—"
        excerpt = _escape(item.get("excerpt", "") or "")
        css_class = {
            "cited": "cite-ok",
            "unresolvable": "cite-bad",
            "not_grounded": "cite-na",
        }.get(status, "")
        rows.append(
            f"<tr class='{css_class}'><td>{label}</td><td>{status}</td>"
            f"<td>{span_text}</td><td>{excerpt}</td></tr>"
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='4'><em>None generated</em></td></tr>"

    return f"""
    <details class="citation-bucket">
        <summary>{_escape(title)}: {bucket['cited']}/{bucket['total']} cited
            ({bucket['coverage_pct']}%){
                ' &mdash; ' + str(bucket['unresolvable']) + ' unresolvable' if bucket['unresolvable'] else ''
            }{
                ' &mdash; ' + str(bucket['not_grounded']) + ' not grounded in this document'
                if bucket['not_grounded'] else ''
            }
        </summary>
        <table>
            <thead><tr><th>Item</th><th>Status</th><th>Source lines</th><th>Cited excerpt</th></tr></thead>
            <tbody>
            {rows_html}
            </tbody>
        </table>
    </details>
    """


def _render_approval_html(report_data):
    approval = report_data.get("approval")
    if not approval or approval.get("status") == "unreviewed_draft":
        statement = _escape(report_data.get(
            "approval_statement",
            "This content has NOT been approved by a named human reviewer. "
            "It is an unreviewed draft.",
        ))
        return f"""
    <div class="section approval-pending">
        <h2>Approval</h2>
        <p><strong>⚠ UNREVIEWED DRAFT</strong> &mdash; {statement}</p>
    </div>
    """
    return f"""
    <div class="section approval-done">
        <h2>Approval</h2>
        <table>
            <tr><th>Approved by</th><td>{_escape(str(approval.get('approved_by', '')))}</td></tr>
            <tr><th>Role</th><td>{_escape(str(approval.get('role', '')))}</td></tr>
            <tr><th>Approved at</th><td>{_escape(str(approval.get('approved_at', '')))}</td></tr>
            <tr><th>Edits made before approval</th><td>{_escape(str(approval.get('edits_count', 0)))}</td></tr>
            <tr><th>Notes</th><td>{_escape(str(approval.get('notes', '')))}</td></tr>
        </table>
    </div>
    """


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
        .citation-bucket {{
            margin: 12px 0;
            padding: 10px 15px;
            background: #f8f9fa;
            border: 1px solid #ddd;
            border-radius: 4px;
        }}
        .citation-bucket summary {{
            cursor: pointer;
            font-weight: bold;
        }}
        tr.cite-ok td {{ background: #e8f5e9; }}
        tr.cite-bad td {{ background: #fdecea; }}
        tr.cite-na td {{ background: #f1f1f1; color: #666; }}
        .approval-pending {{
            border-left: 4px solid #ffc107;
            background: #fff8e1;
        }}
        .approval-done {{
            border-left: 4px solid #4caf50;
            background: #e8f5e9;
        }}
    </style>
</head>
<body>
    <h1>{report_data['report_title']}</h1>

    <div class="warning">
        <h3 style="margin-top: 0;">⚠️ {report_data['disclaimer']}</h3>
        <p><strong>This training content was automatically generated and requires formal review before use.</strong></p>
    </div>

    {_render_approval_html(report_data)}

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

    <div class="section">
        <h2>Citation Coverage</h2>
        <p>Every generated learning objective, training section, and assessment question is expected
        to carry a citation back to a specific line span in the source document. This measures how
        many actually resolve.</p>
        <p><strong>Overall: {report_data['citation_coverage']['overall']['cited']}/{report_data['citation_coverage']['overall']['total']}
        cited ({report_data['citation_coverage']['overall']['coverage_pct']}%)</strong></p>
        {_render_citation_bucket_html("Learning objectives", report_data['citation_coverage']['objectives'])}
        {_render_citation_bucket_html("Section content", report_data['citation_coverage']['sections'])}
        {_render_citation_bucket_html("Assessment questions", report_data['citation_coverage']['assessment_questions'])}
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
