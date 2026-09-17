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

from .audit import AuditLog, verify_job


REQUIRED_APPROVAL_KEYS = ("approved_by", "role", "approved_at", "notes", "edits_count")

#: What each verification mode proves, stated plainly (docs/AUDIT_TRAIL.md
#: "What verification proves" / "What verification cannot prove"). Shown
#: wherever the audit trail is presented, because it changes what an auditor
#: may conclude from an "intact" result.
AUDIT_MODE_STATEMENTS = {
    "chain-only": (
        "Chain-only mode (no HMAC key configured): this detects alteration of "
        "recorded entries; it does not by itself prove entries were not "
        "removed from the end or the whole log regenerated."
    ),
    "hmac": (
        "HMAC mode: this detects alteration of recorded entries, and "
        "regeneration without the server key is detected."
    ),
}


# ---------------------------------------------------------------------------
# What the LMS record will contain
#
# We are not the system of record: the customer's validated LMS is (GOAL.md
# non-goal 1, 21 CFR 820.25). An auditor reading this report therefore needs to
# know which evidence lands *there* rather than here, and - just as important -
# which evidence does not. The SCORM package writes the block below; the exact
# element names per version are in docs/SCORM_CONFORMANCE.md and the behaviour
# is pinned by tests/conformance/test_runtime.py against a recording LMS.
# ---------------------------------------------------------------------------
LMS_RECORD = {
    "written_by": "the SCORM package, from the learner's browser, at submission",
    "score": (
        "Percentage score: cmi.core.score.raw, .min and .max (SCORM 1.2); "
        "cmi.score.raw, .min, .max and cmi.score.scaled (SCORM 2004)."
    ),
    "status": (
        "cmi.core.lesson_status passed/failed/completed (SCORM 1.2); "
        "cmi.success_status plus cmi.completion_status (SCORM 2004)."
    ),
    "per_question_evidence": (
        "One cmi.interactions entry per assessment question, in the order the "
        "questions were presented: the question id, the interaction type, the "
        "response the learner selected, whether it was correct, the question's "
        "weighting, the latency and the time of submission. SCORM 2004 also "
        "carries the question text and an objective id."
    ),
    "session": (
        "Session time and exit, plus cmi.suspend_data holding "
        "{attempt, responses} as compact JSON within the 4096-character limit."
    ),
    "not_written": (
        "The answer key. cmi.interactions.n.correct_responses is never written, "
        "in either version, because the package would have to carry the "
        "expected answer through the learner's browser to write it. An auditor "
        "can see what each learner answered and whether it was judged correct; "
        "the correct answer itself stays in this report and in the SME review "
        "record, not in the LMS."
    ),
    "verification_boundary": (
        "Scoring is performed by the package in the learner's browser and "
        "reported to the LMS; it is not independently verified by the LMS or by "
        "a server. See src/answer_key.py for what that does and does not "
        "protect against."
    ),
}


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


def audit_block_for_job(job_dir, job_id, hmac_key=None):
    """Build the ``audit`` dict :func:`generate_transparency_report` expects,
    from a job's on-disk ``audit.jsonl`` (see ``docs/AUDIT_TRAIL.md``).

    Returns ``None`` when there is no audit trail file at all -- a job
    produced before the trail existed, or by the CLI, which may not write
    one -- so every caller has one line to write and still degrades
    gracefully: ``generate_transparency_report(..., audit=audit_block_for_job(...))``.

    Verification is read-only and re-run fresh every call: nothing here is
    cached, so a report always reflects the trail as it stands right now.
    """
    log = AuditLog.for_job(job_dir, job_id, hmac_key)
    if not log.exists():
        return None
    result = verify_job(job_dir, job_id, hmac_key)
    entries = [entry.to_dict() for entry in log.entries()]
    return {
        "entries": entries,
        "verification": result.to_dict(),
        "hmac_mode": "hmac" if hmac_key else "chain-only",
    }


def _audit_details_summary(event, details):
    """A one-line, human-readable summary of one audit entry's ``details``,
    tailored per event type per docs/AUDIT_TRAIL.md's "What is recorded"
    table. Falls back to a generic key=value listing for anything else."""
    details = details or {}

    def _get(key, default="?"):
        value = details.get(key)
        return default if value in (None, "") else value

    if event == "package.exported":
        sha = str(_get("package_sha256", ""))[:12]
        return f"format={_get('format')} version={_get('scorm_version', '')} package_sha={sha}"
    if event == "content.edited":
        categories = details.get("categories")
        if not isinstance(categories, (dict, list)):
            categories = details.get("edits_by_category")
        cat_text = ""
        if isinstance(categories, dict):
            cat_text = ", ".join(f"{k}={v}" for k, v in categories.items() if v)
        elif isinstance(categories, list) and categories:
            cat_text = ", ".join(str(c) for c in categories)
        summary = f"edits_count={_get('edits_count', 0)}"
        return f"{summary}, categories: {cat_text}" if cat_text else summary
    if event == "content.approved":
        return f"approved_by={_get('approved_by')} role={_get('role', '')}"
    if event == "job.created":
        return f"source_filename={_get('source_filename')}"
    if event == "download.served":
        return f"filename={_get('filename')}"
    if event == "content.generated":
        return f"questions={_get('questions')}"
    if event == "retention.deleted":
        head = str(_get("head_hash", ""))[:12]
        return f"job_id={_get('job_id')} head_hash={head}"
    if not details:
        return ""
    return " ".join(f"{k}={v}" for k, v in sorted(details.items()))


def _build_audit_trail_block(audit):
    """Normalise the ``audit`` argument into ``report["audit_trail"]``.

    ``audit`` is ``None`` (no trail exists) or the shape
    :func:`audit_block_for_job` builds: ``{"entries": [...], "verification":
    ..., "hmac_mode": "hmac" | "chain-only"}``.
    """
    if not audit:
        return {"status": "no_audit_trail"}

    verification = audit.get("verification") or {}
    entries = audit.get("entries") or []
    hmac_mode = audit.get("hmac_mode")

    timeline = []
    for entry in entries:
        actor = entry.get("actor") or {}
        timeline.append({
            "seq": entry.get("seq"),
            "ts": entry.get("ts"),
            "event": entry.get("event"),
            "actor": {
                "name": actor.get("name", ""),
                "role": actor.get("role", ""),
                "source": actor.get("source", ""),
            },
            "content_hash": entry.get("content_hash"),
            "details_summary": _audit_details_summary(entry.get("event"), entry.get("details")),
        })

    return {
        "status": "intact" if verification.get("ok") else "failed",
        "hmac_mode": hmac_mode,
        "entries_count": verification.get("entries", len(entries)),
        "head_hash": verification.get("head_hash", ""),
        "package_matches_approval": verification.get("package_matches_approval"),
        "problems": list(verification.get("problems") or []),
        "timeline": timeline,
    }


def generate_transparency_report(sop_content, training_module, assessment, source_file,
                                 approval=None, audit=None):
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
        audit: Optional dict shaped like :func:`audit_block_for_job`'s
            return value (``{"entries": [...], "verification": ...,
            "hmac_mode": "hmac" | "chain-only"}``). When given, an
            "Audit Trail" block is added to the report. When omitted (the
            default), ``report["audit_trail"]`` states there is no trail --
            this module never reads a job directory itself; the caller
            supplies this once, typically via ``audit_block_for_job``.

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

        # Where the training evidence actually lives once the package is
        # imported, and what is deliberately absent from it.
        "lms_record": dict(LMS_RECORD),

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

    report["audit_trail"] = _build_audit_trail_block(audit)

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


def _render_lms_record_html(report_data):
    """Render the "LMS record" block: what the package reports to the LMS.

    Everything here is fixed text from this module, but it is escaped anyway -
    a report that escapes some of its inputs and not others is one refactor
    away from a hole.
    """
    record = report_data.get("lms_record") or {}
    if not record:
        return ""

    labels = (
        ("written_by", "Written by"),
        ("score", "Score"),
        ("status", "Completion / success status"),
        ("per_question_evidence", "Per-question evidence"),
        ("session", "Session"),
        ("not_written", "Deliberately NOT written"),
        ("verification_boundary", "Verification boundary"),
    )
    rows = "\n".join(
        f"<tr><th>{_escape(label)}</th><td>{_escape(str(record.get(key, '')))}</td></tr>"
        for key, label in labels if record.get(key)
    )
    return f"""
    <div class="section">
        <h2>LMS Record</h2>
        <p>This tool is not the system of record. Once the package is imported, the
        customer's validated LMS holds the training evidence below (21 CFR 820.25).
        The element names for each SCORM version are listed in
        <code>docs/SCORM_CONFORMANCE.md</code>.</p>
        <table>
            {rows}
        </table>
    </div>
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


def _render_audit_trail_html(report_data):
    """Render the audit-trail block: verification status, timeline and
    anchor. A failed verification must be impossible to miss (a red banner),
    and the mode statement must say plainly what the mode does and does not
    prove (docs/AUDIT_TRAIL.md)."""
    audit = report_data.get("audit_trail") or {"status": "no_audit_trail"}

    if audit.get("status") == "no_audit_trail":
        return """
    <div class="section audit-trail">
        <h2>Audit Trail</h2>
        <p><em>No audit trail is available for this content. This happens for content
        produced before the audit trail existed (e.g. by the CLI, or by an earlier
        version of this application).</em></p>
    </div>
    """

    status = audit.get("status", "failed")
    banner = ""
    if status == "failed":
        banner = (
            '<div class="audit-banner-failed">'
            "&#9888; AUDIT TRAIL VERIFICATION FAILED &mdash; this record may have been "
            "altered, or describes a workflow that broke its own rules. See the problems "
            "listed below before relying on anything else in this report."
            "</div>"
        )

    package_matches = audit.get("package_matches_approval")
    if package_matches is True:
        package_matches_text = "Yes"
    elif package_matches is False:
        package_matches_text = "No &mdash; the exported package does not match the approved content"
    else:
        package_matches_text = "Not applicable (no approval, or no export recorded after the last approval, yet)"

    hmac_mode = audit.get("hmac_mode")
    mode_statement = _escape(AUDIT_MODE_STATEMENTS.get(
        hmac_mode, "Unknown verification mode; treat this trail's integrity as unproven."))
    hmac_mode_label = _escape(str(hmac_mode or "unknown"))

    if audit.get("problems"):
        problems_html = "<ul class='audit-problems'>" + "".join(
            f"<li>{_escape(str(problem))}</li>" for problem in audit["problems"]
        ) + "</ul>"
    else:
        problems_html = "<p>No problems found.</p>"

    rows = []
    for item in audit.get("timeline") or []:
        actor = item.get("actor") or {}
        actor_text = _escape(
            f"{actor.get('name', '')} ({actor.get('role', '')}, {actor.get('source', '')})")
        content_hash = item.get("content_hash") or ""
        short_hash = _escape(content_hash[:12]) if content_hash else "&mdash;"
        full_hash = _escape(content_hash)
        rows.append(
            f"<tr><td>{item.get('seq', '')}</td>"
            f"<td>{_escape(str(item.get('ts', '')))}</td>"
            f"<td>{_escape(str(item.get('event', '')))}</td>"
            f"<td>{actor_text}</td>"
            f"<td title='{full_hash}'>{short_hash}</td>"
            f"<td>{_escape(item.get('details_summary', ''))}</td></tr>"
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='6'><em>No entries</em></td></tr>"

    head_hash = _escape(audit.get("head_hash", ""))

    return f"""
    <div class="section audit-trail">
        <h2>Audit Trail</h2>
        {banner}
        <table>
            <tr><th>Verification status</th><td>{_escape(status)}</td></tr>
            <tr><th>Entries</th><td>{audit.get('entries_count', 0)}</td></tr>
            <tr><th>Head hash (as of this export)</th><td><code>{head_hash}</code></td></tr>
            <tr><th>Package matches approval</th><td>{package_matches_text}</td></tr>
            <tr><th>Verification mode</th><td><strong>{hmac_mode_label}</strong> &mdash; {mode_statement}</td></tr>
        </table>
        <h3>Problems found</h3>
        {problems_html}
        <h3>Event timeline</h3>
        <table>
            <thead><tr><th>Seq</th><th>Timestamp</th><th>Event</th><th>Actor</th>
            <th>Content hash</th><th>Details</th></tr></thead>
            <tbody>
            {rows_html}
            </tbody>
        </table>
        <p>Anchor: the head hash above commits to this trail as of the export that produced
        this report - it is <em>not</em> the same value as the package's
        <code>metadata.json</code> <code>audit_head_hash</code>, which anchors the trail as of
        <em>approval</em> (the <code>content.approved</code> entry), one or more entries earlier.
        To confirm the package is unaltered, run <code>python3 -m src.audit show &lt;job_dir&gt;</code>
        and check that the <code>content.approved</code> entry's own hash equals
        <code>metadata.json</code>'s <code>audit_head_hash</code>; then compare this report's
        head hash above (or a copy stored outside this system) with the trail's current head hash
        to confirm no trailing entries were later removed (see <code>docs/AUDIT_TRAIL.md</code>).</p>
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
        .audit-trail {{
            border-left: 4px solid #6c757d;
        }}
        .audit-banner-failed {{
            background: #f8d7da;
            color: #721c24;
            border: 2px solid #dc3545;
            padding: 12px 15px;
            margin-bottom: 12px;
            border-radius: 5px;
            font-weight: bold;
        }}
        .audit-problems {{
            color: #c62828;
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

    {_render_lms_record_html(report_data)}

    {_render_audit_trail_html(report_data)}

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
