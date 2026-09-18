"""Pilot instrumentation for GOAL.md's M1 exit criterion:

    "SMEs accept generated content with <30% edits across 20 real SOPs
    from 3 pilot customers."

`app.py` writes the raw signal into each job's `job.json` (`edits_count`,
`edits_by_category`, `edit_rounds`, and the `review_opened_at` /
`first_edit_at` / `approved_at` timestamps). This module turns a directory
of job.json files into the aggregate numbers a pilot review needs:
`collect()` computes them, `summary_table()` renders them for a terminal,
and running this file as a script does both.

Usage:
    python3 -m src.pilot_metrics <output_folder> [--json]
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from .audit import hmac_key_from_env, verify_job, AuditLog

#: The six keys every job's `edits_by_category` carries (see
#: `app._count_edits_by_category`'s docstring for what each one counts).
EDIT_CATEGORY_KEYS = (
    'title', 'objectives', 'sections', 'questions', 'question_options', 'question_answers',
)


def _zero_categories():
    return {key: 0 for key in EDIT_CATEGORY_KEYS}


def _seconds_between(start_iso, end_iso):
    """Seconds from `start_iso` to `end_iso` (both ISO-8601 strings), or
    None if either is missing or unparseable. Never raises."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (ValueError, TypeError):
        return None
    return max(0.0, (end - start).total_seconds())


def _editable_items(module, assessment):
    """The size of the reviewable surface a job's edit rate is measured
    against.

    editable_items = len(learning_objectives) + len(sections)
                    + sum over questions of (1 + len(options))

    Each learning objective and each section is one editable item. Each
    question contributes 1 (its own text/type/explanation, as a unit) plus
    one per option - an option's wording is independently editable, and so,
    in effect, is which one is marked correct, since picking a different
    existing option as correct is a real edit to this surface even though it
    doesn't rewrite any text.
    """
    module = module if isinstance(module, dict) else {}
    assessment = assessment if isinstance(assessment, dict) else {}

    objectives = module.get('learning_objectives')
    sections = module.get('sections')
    questions = assessment.get('questions')

    n_objectives = len(objectives) if isinstance(objectives, list) else 0
    n_sections = len(sections) if isinstance(sections, list) else 0

    n_question_items = 0
    if isinstance(questions, list):
        for q in questions:
            options = q.get('options') if isinstance(q, dict) else None
            n_options = len(options) if isinstance(options, list) else 0
            n_question_items += 1 + n_options

    return n_objectives + n_sections + n_question_items


def _coerce_int(value, default=0):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _audit_status(job_dir, job_id, hmac_key):
    """`(audit_verified, audit_problems)` for one job's audit trail.

    `audit_verified` is `None` when the job has no `audit.jsonl` at all (a
    job predating the audit trail, or one the app hasn't touched yet with
    that instrumentation) -- never conflated with `False`, which means a
    trail exists and failed verification. Never raises: a trail this module
    cannot even read counts as a failed verification, not a crashed
    dashboard, since `collect()` must tolerate arbitrary garbage on disk.
    """
    try:
        if not AuditLog.for_job(job_dir, job_id, hmac_key).exists():
            return None, []
        result = verify_job(job_dir, job_id, hmac_key)
        return result.ok, list(result.problems)
    except Exception as exc:  # pragma: no cover - defensive, mirrors collect()'s tolerance
        return False, [f"audit trail could not be read: {exc}"]


def _job_summary(job_id, data):
    """Build one job's metrics record from its parsed job.json.

    Raises if `data` isn't even a JSON object; every other field is read
    defensively with `.get` and type checks so a partially-written or
    older-schema job.json degrades to sensible defaults instead of raising.
    `collect()` is what turns a raise here into an `unreadable_jobs` count.
    """
    if not isinstance(data, dict):
        raise ValueError('job.json must be a JSON object')

    status = data.get('status')
    if not isinstance(status, str) or not status:
        status = 'unknown'

    source_filename = data.get('source_filename')
    created_at = data.get('created_at')
    first_edit_at = data.get('first_edit_at')

    approved_at = data.get('approved_at')
    if not approved_at:
        approval = data.get('approval')
        if isinstance(approval, dict):
            approved_at = approval.get('approved_at')

    edits_count = _coerce_int(data.get('edits_count', 0))
    edit_rounds = _coerce_int(data.get('edit_rounds', 0))

    raw_categories = data.get('edits_by_category')
    raw_categories = raw_categories if isinstance(raw_categories, dict) else {}
    categories = _zero_categories()
    for key in EDIT_CATEGORY_KEYS:
        categories[key] = _coerce_int(raw_categories.get(key, 0))

    module = data.get('training_module')
    assessment = data.get('assessment')

    num_questions = 0
    if isinstance(assessment, dict) and isinstance(assessment.get('questions'), list):
        num_questions = len(assessment['questions'])

    editable_items = _editable_items(module, assessment)
    edit_rate = (edits_count / editable_items) if editable_items > 0 else None

    llm_enhancement = data.get('llm_enhancement')
    llm_enhancement = llm_enhancement if isinstance(llm_enhancement, dict) else None

    return {
        'job_id': job_id,
        'status': status,
        'source_filename': source_filename,
        'created_at': created_at,
        'num_questions': num_questions,
        'edits_count': edits_count,
        'edits_by_category': categories,
        'edit_rounds': edit_rounds,
        'editable_items': editable_items,
        'edit_rate': edit_rate,
        'time_to_first_edit_s': _seconds_between(created_at, first_edit_at),
        'time_to_approve_s': _seconds_between(created_at, approved_at),
        'approved_at': approved_at,
        'llm_enhancement': llm_enhancement,
    }


def collect(output_folder, hmac_key=None):
    """Scan every job directory under `output_folder` and aggregate SME
    review/edit/approve metrics.

    Tolerant by construction: this runs against real pilot data, which will
    include half-written jobs, jobs already swept by retention, and jobs
    predating this instrumentation (missing keys). A job directory with no
    `job.json`, a `job.json` that isn't valid JSON, or one whose fields blow
    up while being summarised is counted in `unreadable_jobs` and otherwise
    skipped. `collect()` itself never raises for a single bad job -- and that
    includes a bad or missing audit trail: `_audit_status()` degrades to a
    failed-verification finding rather than propagating an exception.

    `hmac_key` is the key used to verify each job's audit trail (see
    `src/audit.py`). Defaults to `hmac_key_from_env()` -- the same key the
    application itself would use -- so a caller normally doesn't pass one.

    Returns a dict:
    ```
    {
      "total_jobs": int,               # readable job.json files found
      "unreadable_jobs": int,
      "jobs_by_status": {"draft": n, "edited": n, "approved": n, ...},
      "approval_rate": float | None,           # approved / total_jobs
      "mean_edits_per_approved_job": float | None,
      "median_edits_per_approved_job": float | None,
      "edit_rate": float | None,               # THE <30% number - see below
      "mean_edit_rate": float | None,           # unweighted per-job mean, for comparison
      "edits_by_category": {category: total, ...},   # summed over approved jobs
      "median_time_to_approve_s": float | None,
      "by_source_document": {
          filename: {"jobs": int, "approved": int, "approval_rate": float | None,
                     "mean_edits_per_approved_job": float | None,
                     "mean_edit_rate": float | None},
          ...
      },
      "jobs_with_failed_audit": int,           # jobs whose trail exists and failed verification
      "failed_audit_jobs": [ {"job_id": ..., "problems": [...]}, ... ],
      "jobs": [ per-job summary dicts, shape as in _job_summary(), ... ],
    }
    ```
    Each per-job summary also carries:
      "audit_verified": True | False | None,   # None = no audit trail for this job
      "audit_problems": [str, ...],            # from verify_job(); [] when verified or absent

    **`edit_rate` is GOAL.md's "<30% edits" number.** It is computed only
    over *approved* jobs - editing during review is expected and is not
    itself a problem; what the exit criterion asks is how much of the
    content an SME actually signed off on differs from what the model
    produced. It is the *weighted* rate:

        edit_rate = sum(edits_count for approved jobs)
                  / sum(editable_items for approved jobs)

    not a plain average of each job's own rate - so one long SOP with many
    editable items doesn't get diluted to the same weight as a three-item
    edge case, or vice versa. `mean_edit_rate` (the unweighted mean of each
    approved job's own `edit_rate`) is reported alongside it so a pilot
    reviewer can see whether the two diverge (a sign that a few jobs are
    dominating the weighted number). Both are None when there are no
    approved jobs with a nonzero editable surface.
    """
    folder = Path(output_folder)
    jobs = []
    unreadable = 0
    if hmac_key is None:
        hmac_key = hmac_key_from_env()

    if folder.exists() and folder.is_dir():
        for job_dir in sorted(folder.iterdir()):
            if not job_dir.is_dir():
                continue
            job_json_path = job_dir / 'job.json'
            if not job_json_path.is_file():
                continue
            try:
                with open(job_json_path, encoding='utf-8') as f:
                    data = json.load(f)
                summary = _job_summary(job_dir.name, data)
            except Exception:
                unreadable += 1
                continue
            audit_verified, audit_problems = _audit_status(job_dir, job_dir.name, hmac_key)
            summary['audit_verified'] = audit_verified
            summary['audit_problems'] = audit_problems
            jobs.append(summary)

    total_jobs = len(jobs)

    jobs_by_status = {}
    for job in jobs:
        jobs_by_status[job['status']] = jobs_by_status.get(job['status'], 0) + 1

    approved_jobs = [j for j in jobs if j['status'] == 'approved']
    approval_rate = (len(approved_jobs) / total_jobs) if total_jobs else None

    approved_edit_counts = [j['edits_count'] for j in approved_jobs]
    mean_edits_per_approved_job = (
        statistics.fmean(approved_edit_counts) if approved_edit_counts else None)
    median_edits_per_approved_job = (
        statistics.median(approved_edit_counts) if approved_edit_counts else None)

    rated_jobs = [j for j in approved_jobs if j['editable_items'] > 0]
    if rated_jobs:
        total_edits = sum(j['edits_count'] for j in rated_jobs)
        total_items = sum(j['editable_items'] for j in rated_jobs)
        edit_rate = (total_edits / total_items) if total_items else None
        mean_edit_rate = statistics.fmean(j['edit_rate'] for j in rated_jobs)
    else:
        edit_rate = None
        mean_edit_rate = None

    category_totals = _zero_categories()
    for job in approved_jobs:
        for key in EDIT_CATEGORY_KEYS:
            category_totals[key] += job['edits_by_category'].get(key, 0)

    approve_times = [
        j['time_to_approve_s'] for j in approved_jobs if j['time_to_approve_s'] is not None]
    median_time_to_approve_s = statistics.median(approve_times) if approve_times else None

    by_source_document = {}
    for job in jobs:
        name = job.get('source_filename') or '(unknown source)'
        bucket = by_source_document.setdefault(name, {
            'jobs': 0, 'approved': 0, '_edits': [], '_rates': [],
        })
        bucket['jobs'] += 1
        if job['status'] == 'approved':
            bucket['approved'] += 1
            bucket['_edits'].append(job['edits_count'])
            if job['edit_rate'] is not None:
                bucket['_rates'].append(job['edit_rate'])

    for bucket in by_source_document.values():
        edits = bucket.pop('_edits')
        rates = bucket.pop('_rates')
        bucket['approval_rate'] = (bucket['approved'] / bucket['jobs']) if bucket['jobs'] else None
        bucket['mean_edits_per_approved_job'] = statistics.fmean(edits) if edits else None
        bucket['mean_edit_rate'] = statistics.fmean(rates) if rates else None

    failed_audit_jobs = [
        {'job_id': job['job_id'], 'problems': job['audit_problems']}
        for job in jobs if job['audit_verified'] is False
    ]

    return {
        'total_jobs': total_jobs,
        'unreadable_jobs': unreadable,
        'jobs_by_status': jobs_by_status,
        'approval_rate': approval_rate,
        'mean_edits_per_approved_job': mean_edits_per_approved_job,
        'median_edits_per_approved_job': median_edits_per_approved_job,
        'edit_rate': edit_rate,
        'mean_edit_rate': mean_edit_rate,
        'edits_by_category': category_totals,
        'median_time_to_approve_s': median_time_to_approve_s,
        'by_source_document': by_source_document,
        'jobs_with_failed_audit': len(failed_audit_jobs),
        'failed_audit_jobs': failed_audit_jobs,
        'jobs': jobs,
    }


def _fmt_pct(value):
    return 'n/a' if value is None else f'{value * 100:.1f}%'


def _fmt_num(value):
    return 'n/a' if value is None else f'{value:.2f}'


def summary_table(metrics):
    """Render `collect()`'s output as a plain-text table for a terminal."""
    lines = []
    lines.append('Pilot metrics')
    lines.append('=============')
    lines.append(f"Jobs: {metrics['total_jobs']} readable, "
                 f"{metrics['unreadable_jobs']} unreadable")
    status_line = ', '.join(
        f'{status}={count}' for status, count in sorted(metrics['jobs_by_status'].items()))
    lines.append(f"By status: {status_line or '(none)'}")
    lines.append('')
    lines.append(f"Approval rate: {_fmt_pct(metrics['approval_rate'])}")
    lines.append(f"Mean edits / approved job: {_fmt_num(metrics['mean_edits_per_approved_job'])}")
    lines.append(
        f"Median edits / approved job: {_fmt_num(metrics['median_edits_per_approved_job'])}")
    lines.append(
        f"Edit rate (weighted, approved jobs) - GOAL.md M1 target is <30%: "
        f"{_fmt_pct(metrics['edit_rate'])}")
    lines.append(f"Edit rate (mean of per-job rates): {_fmt_pct(metrics['mean_edit_rate'])}")
    lines.append(
        f"Median time to approve: {_fmt_num(metrics['median_time_to_approve_s'])} s")
    lines.append('')
    lines.append(f"Jobs with failed audit-trail verification: {metrics['jobs_with_failed_audit']}")
    for failed in metrics.get('failed_audit_jobs', []):
        lines.append(f"  {failed['job_id']}: {'; '.join(failed['problems']) or '(no detail)'}")
    lines.append('')
    lines.append('Edits by category (approved jobs, summed):')
    for key in EDIT_CATEGORY_KEYS:
        lines.append(f"  {key}: {metrics['edits_by_category'].get(key, 0)}")
    lines.append('')
    lines.append('By source document:')
    by_source = metrics['by_source_document']
    if not by_source:
        lines.append('  (none)')
    for name in sorted(by_source):
        bucket = by_source[name]
        lines.append(
            f"  {name}: {bucket['jobs']} job(s), {bucket['approved']} approved, "
            f"approval_rate={_fmt_pct(bucket['approval_rate'])}, "
            f"mean_edit_rate={_fmt_pct(bucket['mean_edit_rate'])}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate SME pilot review metrics from an app output folder.")
    parser.add_argument(
        'output_folder', help="Path to the app's OUTPUT_FOLDER (contains one directory per job).")
    parser.add_argument(
        '--json', action='store_true', help='Print the raw metrics dict as JSON instead of a table.')
    args = parser.parse_args(argv)

    metrics = collect(args.output_folder)
    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(summary_table(metrics))


if __name__ == '__main__':
    main()
