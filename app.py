"""
Web Application for Training Creator
Flask-based web interface for converting SOPs to training materials
"""

import html
import logging
import os
import secrets
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, render_template, jsonify, send_from_directory
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import json

from src.audit import (
    Actor,
    AuditLog,
    content_hash,
    file_hash,
    global_log,
    hmac_key_from_env,
    verify_job,
)
from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import (
    MIN_ASSESSMENT_QUESTIONS,
    AssessmentGenerator,
    MedicalDeviceAssessmentGenerator,
    naive_strategies,
    strategy_pick,
    # `_assign_answer_positions` is deliberately a private helper of
    # src/assessments.py, imported here rather than reimplemented. Which
    # option is "correct" is SME content; *where* that option sits in the
    # option list is layout the server owns (CLAUDE.md invariant #1: "a
    # naive learner must fail"). This is the one place that invariant is
    # enforced at generation time, so re-running it on an SME edit - rather
    # than duplicating its logic - is the only way to guarantee an edited
    # assessment stays byte-for-byte inside the same guarantee a freshly
    # generated one is held to, with no risk of drifting out of sync with it.
    _assign_answer_positions,
)
from src.answer_key import document_key
from src.scorm_exporter import SCORMExporter
from src.transparency_report import (
    generate_transparency_report, create_html_report, create_json_report, audit_block_for_job,
)
from src.medical_device_config import MEDICAL_DEVICE_CONFIG
from src.llm import LLMConfig, enhance_assessment, enhance_module, merge_reports
from src.llm import build_provider as build_llm_provider
from src.serialization import sop_from_dict, module_from_dict, assessment_from_dict
from src.pilot_metrics import collect as collect_pilot_metrics

app = Flask(__name__)

# --- Configuration (env-overridable; sane local-dev defaults) ---------------

app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))  # 16MB default
app.config['UPLOAD_FOLDER'] = str(Path(os.environ.get('UPLOAD_FOLDER', 'uploads')).resolve())
app.config['OUTPUT_FOLDER'] = str(Path(os.environ.get('OUTPUT_FOLDER', 'outputs')).resolve())

# How long a signed download link remains valid, in seconds.
app.config['DOWNLOAD_TTL_SECONDS'] = int(os.environ.get('DOWNLOAD_TTL_SECONDS', 24 * 60 * 60))

# How long a job's output directory is retained before being swept away.
app.config['OUTPUT_RETENTION_SECONDS'] = int(os.environ.get('OUTPUT_RETENTION_SECONDS', 24 * 60 * 60))

_env_secret_key = os.environ.get('SECRET_KEY')
_secret_key_was_generated = not bool(_env_secret_key)
app.config['SECRET_KEY'] = _env_secret_key or secrets.token_hex(32)

# Audit trail HMAC key (src/audit.py, docs/AUDIT_TRAIL.md). Built once at
# import, same pattern as SECRET_KEY above, and stored on app.config (rather
# than a bare module constant) so tests can override it the same way they
# override SECRET_KEY. Unset AUDIT_HMAC_KEY is a supported "chain-only"
# configuration - entries still hash-chain and verify, they simply carry no
# `mac`; see DEPLOYMENT.md for what that does and doesn't protect against.
app.config['AUDIT_HMAC_KEY'] = hmac_key_from_env()

# --- Logging ------------------------------------------------------------

def _configure_logging(flask_app):
    """Configure structured logging: timestamp, level, logger name, message."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    ))
    flask_app.logger.handlers = [handler]
    flask_app.logger.setLevel(logging.INFO)
    flask_app.logger.propagate = False


_configure_logging(app)

if _secret_key_was_generated:
    app.logger.warning(
        "SECRET_KEY not set in environment; generated a random key at startup. "
        "Signed download links will stop working across a process restart. "
        "Set SECRET_KEY in the environment for stable links."
    )

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'md', 'pdf', 'docx'}

# Sample-SOP gallery routes (GET /api/samples, POST /api/samples/<id>/generate).
# The blueprint imports this module's processing helpers lazily inside its
# views, so registering it here creates no import cycle.
from src.samples_routes import samples_bp  # noqa: E402
app.register_blueprint(samples_bp)
ALLOWED_OUTPUT_FORMATS = {'scorm', 'json', 'html'}
ALLOWED_SCORM_VERSIONS = {'1.2', '2004'}

DOWNLOAD_TOKEN_SALT = 'training-creator-download-v1'


class RequestValidationError(ValueError):
    """A validation error this app itself raises about a request's own
    content (bad upload parameters, a malformed SME edit, ...).

    Its message is written for the person making the request and is safe to
    return verbatim. Anything else - a bare ValueError or any other
    exception raised deeper in the pipeline (parser, generator, exporter) -
    is an internal failure, not a validation message, and must never reach
    the client as text; see `_internal_error_response`.
    """


def _internal_error_response(job_id, exc):
    """Log an unexpected exception in full (with the job id, if there is
    one) and return a generic 500 body that carries no internal exception
    text - only the job id, so support can find the matching log line.

    `job_id` is not a secret (it is already echoed back on success and
    embedded in download URLs), so including it in the response is safe and
    is the whole point: it lets a person report a failure that support can
    actually locate in the logs.
    """
    if job_id:
        app.logger.error(f"[job_id={job_id}] Unhandled error: {exc}", exc_info=True)
        message = (
            'An internal error occurred while processing this request. '
            f'Please contact support with this job id: {job_id}'
        )
    else:
        app.logger.error(f"Unhandled error: {exc}", exc_info=True)
        message = 'An internal error occurred while processing this request.'
    return jsonify({'error': message}), 500


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# --- Retention -------------------------------------------------------------
# M0 note: this is a simple synchronous local-disk sweep. M2 moves storage to
# object storage (e.g. S3) with bucket lifecycle rules instead of app-managed
# deletion; see DEPLOYMENT.md.

def _sweep_expired_outputs(output_folder, retention_seconds):
    """Delete job directories under output_folder older than retention_seconds.

    Before a job directory (and the `audit.jsonl` inside it) is deleted, its
    last head hash is recorded as `retention.deleted` on the *global* log
    (`<output_folder>/audit-global.jsonl` - see `src.audit.global_log`),
    because that is all that survives of the job's own trail afterwards.

    This runs unattended (at process startup, or from a scheduler) rather
    than inside a request, so a job whose retention record can't be written
    - a bad log, a read-only disk - is skipped rather than crashing the
    whole sweep or the app's startup; it is picked up on the next sweep once
    the underlying problem is fixed, and the failure is logged loudly rather
    than the deletion silently going unrecorded.
    """
    folder = Path(output_folder)
    if not folder.exists():
        return
    now = time.time()
    audit_key = app.config.get('AUDIT_HMAC_KEY')
    for job_dir in folder.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            age = now - job_dir.stat().st_mtime
        except OSError:
            continue
        if age <= retention_seconds:
            continue

        job_id = job_dir.name
        try:
            head_hash = AuditLog.for_job(job_dir, job_id, audit_key).head_hash()
            global_log(output_folder, audit_key).append(
                'retention.deleted', Actor('retention-sweep', 'system', 'system'),
                {'job_id': job_id, 'head_hash': head_hash,
                 'retention_seconds': int(retention_seconds)},
            )
        except (OSError, ValueError) as e:
            app.logger.error(
                f"[job_id={job_id}] Could not record retention.deleted; "
                f"leaving the job directory for the next sweep: {e}")
            continue

        shutil.rmtree(job_dir, ignore_errors=True)


def _cleanup_upload(upload_path):
    """Delete the uploaded source file (and its now-empty job directory).

    The uploaded document is the customer's controlled source document; we
    don't retain it past the processing that needed it.
    """
    try:
        path = Path(upload_path)
        if path.exists():
            path.unlink()
        parent = path.parent
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as e:
        app.logger.warning(f"Could not clean up upload {upload_path}: {e}")


# Sweep once at process startup.
_sweep_expired_outputs(app.config['OUTPUT_FOLDER'], app.config['OUTPUT_RETENTION_SECONDS'])


# --- Signed download links ---------------------------------------------

def _serializer():
    # Built fresh on every call (not cached at import time) so app.secret_key
    # can be overridden after import, e.g. by tests.
    return URLSafeTimedSerializer(app.secret_key, salt=DOWNLOAD_TOKEN_SALT)


def generate_download_token(job_id):
    """Sign a token that authorizes downloads for this job_id."""
    return _serializer().dumps(job_id)


def _is_safe_path_component(value):
    """Reject anything that could be used for directory traversal."""
    if not value:
        return False
    if '/' in value or '\\' in value:
        return False
    if '..' in value:
        return False
    return True


def _verify_job_token(job_id):
    """Validate the ``t`` query-string token against ``job_id``.

    Shared by downloads, the review page and the review/approve APIs, so a
    single signed link (minted once per job, at generation time) authorizes
    all of them under the same rules: present, unexpired, and signed for this
    exact job_id.

    Returns ``None`` if the token checks out, otherwise an
    ``(error_message, status_code)`` pair.
    """
    token = request.args.get('t')
    if not token:
        return ('Missing download token', 403)

    ttl = int(app.config.get('DOWNLOAD_TTL_SECONDS', 24 * 60 * 60))
    try:
        token_job_id = _serializer().loads(token, max_age=ttl)
    except SignatureExpired:
        return ('Download link has expired', 403)
    except BadSignature:
        return ('Invalid download token', 403)

    if token_job_id != job_id:
        return ('Invalid download token', 403)

    return None


# --- Job persistence (draft / edited / approved) ----------------------------
# `job.json` is the current state of a job (regenerated on every edit and on
# approval). `draft.json` is a one-time snapshot of the *untouched* generated
# content, written the first time a job is edited, so `edits_count` is always
# measured against what the model actually produced - not against the last
# edit. `approval.json` is the immutable approval record. All three are
# written through `_atomic_write_json`, which never leaves a half-written
# file for a concurrent reader to see.

def _job_dir(job_id):
    return Path(app.config['OUTPUT_FOLDER']) / job_id


def _audit_log(job_id):
    """This job's `AuditLog` (docs/AUDIT_TRAIL.md). One per job, for its life;
    callers just call this wherever they need to append or read it - the file
    itself is what persists, not this object."""
    return AuditLog.for_job(_job_dir(job_id), job_id, app.config.get('AUDIT_HMAC_KEY'))


def _anonymous_web_actor():
    """Web actions before an approval carry no authenticated identity (that's
    M2). This is the trail's honest placeholder, not a real name - see
    docs/AUDIT_TRAIL.md and DEPLOYMENT.md."""
    return Actor('anonymous', 'author', 'web')


def _job_json_path(job_id):
    return _job_dir(job_id) / 'job.json'


def _draft_json_path(job_id):
    return _job_dir(job_id) / 'draft.json'


def _approval_json_path(job_id):
    return _job_dir(job_id) / 'approval.json'


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path, data):
    """Write JSON to `path` via a temp file + os.replace, so a reader never
    sees a partially-written file and a crash mid-write can't corrupt it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-job-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_job_json(job_id):
    """Load job.json for job_id, or None if the job doesn't exist / is unreadable."""
    path = _job_json_path(job_id)
    if not path.is_file():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_ABSENT = object()


def _count_edits(original, edited):
    """Count leaf-level differences between two JSON-like structures.

    Used to compute `edits_count`: how much a reviewer changed relative to
    the untouched generation. Dicts are compared key-by-key (union of keys on
    both sides), lists element-by-element by position; anything else is one
    diff if the values aren't equal. Not a semantic diff (an inserted item
    shifts every later index and looks like N changes) - it doesn't need to
    be exact, only representative of how much was touched.
    """
    if isinstance(original, dict) and isinstance(edited, dict):
        keys = set(original) | set(edited)
        return sum(_count_edits(original.get(k), edited.get(k)) for k in keys)
    if isinstance(original, list) and isinstance(edited, list):
        total = 0
        for i in range(max(len(original), len(edited))):
            o = original[i] if i < len(original) else _ABSENT
            e = edited[i] if i < len(edited) else _ABSENT
            total += _count_edits(o, e)
        return total
    return 0 if original == edited else 1


#: The five leaf categories `edits_by_category` breaks `edits_count` into,
#: plus `title`. Every job.json (from the first upload onward) carries this
#: dict with all six keys present, so pilot_metrics.collect() never has to
#: guess about a missing key.
_EDIT_CATEGORY_KEYS = (
    'title', 'objectives', 'sections', 'questions', 'question_options', 'question_answers',
)


def _zero_edit_categories():
    return {key: 0 for key in _EDIT_CATEGORY_KEYS}


def _correct_option_text(question):
    """The text of `question`'s correct option, or None if that can't be
    determined (missing/malformed options or correct_answer).

    Used to compare *which fact is asserted correct* across an edit, rather
    than *which index* is marked correct - `_relayout_assessment_answers`
    re-shuffles option order on every save, so comparing indices across a
    save would count the server's own layout as an SME edit.
    """
    if not isinstance(question, dict):
        return None
    options = question.get('options')
    correct = question.get('correct_answer')
    if not isinstance(options, list) or isinstance(correct, bool) or not isinstance(correct, int):
        return None
    if not (0 <= correct < len(options)):
        return None
    return options[correct]


def _count_edits_by_category(draft_module, module_dict, draft_assessment, assessment_dict):
    """Break `_count_edits`'s single total down by what kind of content
    changed, comparing (like `_count_edits`) the SME's raw submission
    against the untouched draft.

    Returns a dict with exactly `_EDIT_CATEGORY_KEYS`:
    - `title`: 1 if the module title text changed, else 0.
    - `objectives`: leaf diffs (`_count_edits`) within `learning_objectives`.
    - `sections`: leaf diffs within `sections` (title, content, id, ... all
      count, matched by list position - same semantics as the top-level
      `edits_count`).
    - `questions`: leaf diffs within each question *excluding* `options` and
      `correct_answer` (text, type, explanation, points, ... - those two
      fields are broken out below instead), matched by question list
      position.
    - `question_options`: leaf diffs within each question's `options` list,
      matched by position - i.e. option *text* rewrites. Moving which option
      is correct without changing any option's wording contributes 0 here.
    - `question_answers`: 1 per question whose correct option's *text*
      (`_correct_option_text`) differs between draft and submission, however
      that happened - the SME picking a different existing option as
      correct, or rewriting the text of the option that was already correct.
      Compared by text, not index, so the server's own answer-position
      layout (which runs *after* this count, in `_relayout_assessment_answers`)
      can never itself register as an edit.

    Questions added or removed between draft and submission (a list-length
    change) are counted in full, undifferentiated, under `questions` - the
    pilot's synthetic-fixture tests don't exercise this path, and a whole
    added/removed question is an edge case rare enough in real review
    sessions not to warrant its own category.
    """
    categories = _zero_edit_categories()

    draft_module = draft_module if isinstance(draft_module, dict) else {}
    module_dict = module_dict if isinstance(module_dict, dict) else {}
    categories['title'] = _count_edits(draft_module.get('title'), module_dict.get('title'))
    categories['objectives'] = _count_edits(
        draft_module.get('learning_objectives'), module_dict.get('learning_objectives'))
    categories['sections'] = _count_edits(draft_module.get('sections'), module_dict.get('sections'))

    draft_assessment = draft_assessment if isinstance(draft_assessment, dict) else {}
    assessment_dict = assessment_dict if isinstance(assessment_dict, dict) else {}
    draft_questions = draft_assessment.get('questions')
    new_questions = assessment_dict.get('questions')
    draft_questions = draft_questions if isinstance(draft_questions, list) else []
    new_questions = new_questions if isinstance(new_questions, list) else []

    for i in range(max(len(draft_questions), len(new_questions))):
        old_q = draft_questions[i] if i < len(draft_questions) else _ABSENT
        new_q = new_questions[i] if i < len(new_questions) else _ABSENT
        if old_q is _ABSENT or new_q is _ABSENT:
            categories['questions'] += _count_edits(old_q, new_q)
            continue

        old_fields = {k: v for k, v in old_q.items() if k not in ('options', 'correct_answer')} \
            if isinstance(old_q, dict) else old_q
        new_fields = {k: v for k, v in new_q.items() if k not in ('options', 'correct_answer')} \
            if isinstance(new_q, dict) else new_q
        categories['questions'] += _count_edits(old_fields, new_fields)

        old_options = old_q.get('options') if isinstance(old_q, dict) else None
        new_options = new_q.get('options') if isinstance(new_q, dict) else None
        categories['question_options'] += _count_edits(old_options, new_options)

        if _correct_option_text(old_q) != _correct_option_text(new_q):
            categories['question_answers'] += 1

    return categories


def _validate_module_dict(data):
    """Strictly validate an edited TrainingModule dict (the shape of
    TrainingModule.to_dict()). Raises RequestValidationError with a precise
    message safe to return to the caller verbatim."""
    if not isinstance(data, dict):
        raise RequestValidationError('module must be a JSON object')

    title = data.get('title')
    if not isinstance(title, str) or not title.strip():
        raise RequestValidationError('module.title must be a non-empty string')

    objectives = data.get('learning_objectives')
    if not isinstance(objectives, list) or not objectives:
        raise RequestValidationError('module.learning_objectives must be a non-empty list')
    for i, obj in enumerate(objectives):
        if not isinstance(obj, str) or not obj.strip():
            raise RequestValidationError(f'module.learning_objectives[{i}] must be a non-empty string')

    sections = data.get('sections')
    if not isinstance(sections, list) or not sections:
        raise RequestValidationError('module.sections must be a non-empty list')
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            raise RequestValidationError(f'module.sections[{i}] must be an object')
        if not isinstance(section.get('id'), str) or not section['id']:
            raise RequestValidationError(f'module.sections[{i}] must have a non-empty id')
        if not isinstance(section.get('content'), str):
            raise RequestValidationError(f'module.sections[{i}].content must be a string')

    if not isinstance(data.get('estimated_duration', 0), int):
        raise RequestValidationError('module.estimated_duration must be an integer')


def _validate_assessment_dict(data):
    """Strictly validate an edited Assessment dict (the shape of
    Assessment.to_dict()). Raises RequestValidationError with a precise
    message safe to return to the caller verbatim."""
    if not isinstance(data, dict):
        raise RequestValidationError('assessment must be a JSON object')

    passing_score = data.get('passing_score')
    if not isinstance(passing_score, int) or isinstance(passing_score, bool) \
            or not (0 < passing_score <= 100):
        raise RequestValidationError('assessment.passing_score must be an integer between 1 and 100')

    questions = data.get('questions')
    if not isinstance(questions, list):
        raise RequestValidationError('assessment.questions must be a list')
    if len(questions) < MIN_ASSESSMENT_QUESTIONS:
        raise RequestValidationError(
            f'assessment must have at least {MIN_ASSESSMENT_QUESTIONS} questions '
            f'(has {len(questions)})')

    seen_ids = set()
    for i, q in enumerate(questions):
        label = f'assessment.questions[{i}]'
        if not isinstance(q, dict):
            raise RequestValidationError(f'{label} must be an object')

        qid = q.get('id')
        if not isinstance(qid, str) or not qid:
            raise RequestValidationError(f'{label}.id must be a non-empty string')
        if qid in seen_ids:
            raise RequestValidationError(f'{label}.id "{qid}" is used by more than one question')
        seen_ids.add(qid)

        text = q.get('text')
        if not isinstance(text, str) or not text.strip():
            raise RequestValidationError(f'{label}.text must be a non-empty string')

        options = q.get('options')
        if not isinstance(options, list) or not (2 <= len(options) <= 4):
            raise RequestValidationError(f'{label}.options must be a list of 2 to 4 items')
        for j, opt in enumerate(options):
            if not isinstance(opt, str) or not opt.strip():
                raise RequestValidationError(f'{label}.options[{j}] must be a non-empty string')
        normalized = [opt.strip().lower() for opt in options]
        if len(set(normalized)) != len(normalized):
            raise RequestValidationError(f'{label}.options must not contain duplicate options')

        correct = q.get('correct_answer')
        if isinstance(correct, bool) or not isinstance(correct, int) \
                or not (0 <= correct < len(options)):
            raise RequestValidationError(
                f'{label}.correct_answer must be an integer index into options '
                f'(0..{len(options) - 1})')

        explanation = q.get('explanation', '')
        if not isinstance(explanation, str):
            raise RequestValidationError(f'{label}.explanation must be a string')


def _relayout_assessment_answers(assessment, sop_content):
    """Re-run the generator's own deterministic answer-position layout on an
    edited assessment, in place.

    An SME editing the review page chooses which option is *correct* - that's
    content, and legitimately theirs to change. Where that correct option
    *sits* in the option list is layout, and CLAUDE.md invariant #1 ("a naive
    learner must fail") depends entirely on the server controlling it. Without
    this, an SME setting every `correct_answer` to 0 while editing would
    silently reopen Defect 1 through the review UI.

    `_assign_answer_positions` (see src/assessments.py) expects each affected
    question's `options[0]` to already hold the correct text before it
    shuffles the rest and re-assigns `correct_answer` - exactly the
    "canonical form" `_Candidate.materialize` puts freshly generated
    questions in. So every non-true_false question is normalised into that
    form first: its SME-chosen correct option is moved to index 0 (the other
    options keep their relative order; the helper reshuffles them anyway with
    its own seeded RNG). True/false questions are left untouched -
    `_assign_answer_positions` already skips them, since their integrity
    comes from truth-value balancing at generation time, not position; if the
    SME flipped True/False, that's their call.
    """
    for q in assessment.questions:
        if q.type == 'true_false':
            continue
        if isinstance(q.correct_answer, bool) or not isinstance(q.correct_answer, int):
            continue
        if not (0 <= q.correct_answer < len(q.options)):
            continue
        correct_text = q.options[q.correct_answer]
        rest = [opt for i, opt in enumerate(q.options) if i != q.correct_answer]
        q.options = [correct_text] + rest
        q.correct_answer = 0

    doc_key = document_key(sop_content.title, sop_content.version)
    _assign_answer_positions(assessment.questions, doc_key)


def _naive_strategy_failures(assessment):
    """Score every fixed answering strategy a learner could execute without
    reading the SOP against this exact assessment (already laid out).

    Mirrors CLAUDE.md invariant #1 - "always option N", "always last",
    "always True", "always the longest/shortest option" - all must fall
    short of the assessment's own passing score. The position-based
    strategies reuse `naive_strategies`/`strategy_pick` from
    src/assessments.py (the same ones `_assign_answer_positions` optimises
    against); "longest"/"shortest"/"True" are layout-independent text
    strategies that position optimisation cannot fix, so they are modelled
    here directly, the same way tests/test_assessments.py does.

    Returns a list of (name, score_percent, question_numbers) for every
    strategy meeting or beating the passing score, worst (highest-scoring)
    first. `question_numbers` are the 1-based questions that strategy
    answers correctly, for a targeted fix suggestion.
    """
    questions = assessment.questions
    if not questions:
        return []
    total_points = sum(q.points for q in questions) or 1

    def score(picker):
        earned = 0
        hits = []
        for number, q in enumerate(questions, 1):
            if not q.options:
                continue
            try:
                pick = picker(q)
            except (ValueError, IndexError):
                pick = -1
            if pick == q.correct_answer:
                earned += q.points
                hits.append(number)
        return 100.0 * earned / total_points, hits

    strategies = []
    max_options = max(len(q.options) for q in questions if q.options)
    for strat in naive_strategies(max_options):
        kind, aim, _fallback = strat
        name = ('always choosing the last option' if kind == 'last'
                else f'always choosing option {aim + 1}')
        strategies.append((name, (lambda s: lambda q: strategy_pick(s, len(q.options)))(strat)))

    strategies.append((
        'always choosing the longest option',
        lambda q: max(range(len(q.options)), key=lambda i: (len(q.options[i]), -i)),
    ))
    strategies.append((
        'always choosing the shortest option',
        lambda q: min(range(len(q.options)), key=lambda i: (len(q.options[i]), i)),
    ))
    strategies.append((
        'always answering True',
        lambda q: q.options.index('True') if 'True' in q.options else -1,
    ))

    failures = []
    for name, picker in strategies:
        pct, hits = score(picker)
        if pct >= assessment.passing_score:
            failures.append((name, pct, hits))

    failures.sort(key=lambda item: item[1], reverse=True)
    return failures


def _naive_failure_message(failure, passing_score):
    """Build a precise, actionable error message for one failing strategy."""
    name, pct, hits = failure
    where = ', '.join(str(h) for h in hits[:12])
    if len(hits) > 12:
        where += ', ...'

    if 'longest' in name:
        fix = 'shorten the correct option or lengthen a distractor'
    elif 'shortest' in name:
        fix = 'lengthen the correct option or shorten a distractor'
    elif 'True' in name:
        fix = 'make some of these True statements False instead (or vice versa)'
    else:
        fix = "change which option is correct so it isn't always in the same position"

    plural = '' if len(hits) == 1 else 's'
    return (
        f'{name} would score {pct:.0f}%, at or above the passing score of '
        f'{passing_score}%: {fix} in question{plural} {where}.'
    )


@app.route('/')
def index():
    """Serve the main application page"""
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handle file upload and initiate processing"""
    upload_path = None
    job_id = None
    try:
        # Check if file is present
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': f'File type not supported. Allowed types: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

        # Get parameters
        try:
            num_questions = int(request.form.get('num_questions', 5))
            passing_score = int(request.form.get('passing_score', 70))
        except (TypeError, ValueError):
            return jsonify({'error': 'num_questions and passing_score must be integers'}), 400

        scorm_version = request.form.get('scorm_version', '1.2')
        output_format = request.form.get('output_format', 'scorm')

        # Validate parameters
        # The floor is a validity requirement, not a preference: an assessment
        # shorter than MIN_ASSESSMENT_QUESTIONS cannot spread its answers well
        # enough to stop a learner passing by clicking the same option every
        # time, so the generator would silently raise it anyway.
        if not MIN_ASSESSMENT_QUESTIONS <= num_questions <= 20:
            return jsonify({'error': 'Number of questions must be between '
                                     f'{MIN_ASSESSMENT_QUESTIONS} and 20'}), 400
        if not 50 <= passing_score <= 100:
            return jsonify({'error': 'Passing score must be between 50 and 100'}), 400
        if output_format not in ALLOWED_OUTPUT_FORMATS:
            return jsonify({
                'error': f'Invalid output_format. Must be one of: {", ".join(sorted(ALLOWED_OUTPUT_FORMATS))}'
            }), 400
        if scorm_version not in ALLOWED_SCORM_VERSIONS:
            return jsonify({
                'error': f'Invalid scorm_version. Must be one of: {", ".join(sorted(ALLOWED_SCORM_VERSIONS))}'
            }), 400

        # Generate unique job ID
        job_id = str(uuid.uuid4())

        # Save uploaded file
        filename = secure_filename(file.filename)
        upload_path = Path(app.config['UPLOAD_FOLDER']) / job_id / filename
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        file.save(str(upload_path))

        app.logger.info(f"[job_id={job_id}] Received upload: {filename}")

        # Process the file
        result = process_training(
            str(upload_path),
            job_id,
            num_questions,
            passing_score,
            scorm_version,
            output_format
        )

        app.logger.info(f"[job_id={job_id}] Processing complete")
        return jsonify(result), 200

    except RequestEntityTooLarge:
        # Let this propagate to the 413 error handler instead of becoming a 500.
        raise
    except RequestValidationError as e:
        # Raised by this app's own input checks - the message is written
        # for the caller and safe to return verbatim.
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        # Anything else (a parser/generator/exporter failure, a bare
        # ValueError from deeper in the pipeline, ...) is internal - never
        # echo str(e) to the client.
        return _internal_error_response(job_id, e)
    finally:
        if upload_path is not None:
            _cleanup_upload(upload_path)


def process_training(file_path, job_id, num_questions, passing_score, scorm_version, output_format,
                      sample_id=None):
    """Process SOP and generate training package.

    `sample_id` is set only when this run came from the sample-SOP gallery
    (src/samples_routes.py) rather than a real upload; it is recorded in the
    `job.created` audit entry's details and nowhere else.
    """
    if output_format not in ALLOWED_OUTPUT_FORMATS:
        raise RequestValidationError(f"Unsupported output_format: {output_format}")
    if scorm_version not in ALLOWED_SCORM_VERSIONS:
        raise RequestValidationError(f"Unsupported scorm_version: {scorm_version}")

    audit_log = _audit_log(job_id)
    # Hashed before parsing touches it, and definitely before the caller's
    # `finally` deletes it once this function returns.
    source_sha256 = file_hash(file_path)
    job_created_details = {
        'source_filename': Path(file_path).name,
        'num_questions': num_questions,
        'passing_score': passing_score,
        'scorm_version': scorm_version,
        'output_format': output_format,
        'source_sha256': source_sha256,
    }
    if sample_id is not None:
        job_created_details['sample_id'] = sample_id
    audit_log.append('job.created', _anonymous_web_actor(), job_created_details)

    try:
        # Step 1: Parse SOP
        parser = SOPParser()
        sop_content = parser.parse(file_path)

        # Step 2: Generate training content
        generator = TrainingGenerator()
        training_module = generator.generate(sop_content)

        # Step 3: Generate assessment (using medical device generator for compliance)
        assessment_gen = MedicalDeviceAssessmentGenerator()
        assessment = assessment_gen.generate(
            sop_content,
            num_questions=num_questions,
            passing_score=passing_score
        )

        # Step 3b: Optional grounded LLM enhancement. Off unless
        # TRAINING_CREATOR_LLM=anthropic (see docs/LLM.md). The provider is
        # built once so the calls share the cached document prefix; the
        # enhance functions never raise and return the inputs unchanged when
        # disabled or on any provider failure, with the reason in the report.
        llm_config = LLMConfig.from_env()
        enhancement_report = None
        if llm_config.enabled:
            provider = build_llm_provider(llm_config)
            training_module, module_report = enhance_module(
                training_module, sop_content, provider, llm_config)
            assessment, assessment_report = enhance_assessment(
                assessment, sop_content, provider, llm_config)
            enhancement_report = merge_reports(
                job_id, [module_report, assessment_report], llm_config)
            app.logger.info(
                f"[job_id={job_id}] LLM enhancement: "
                f"{enhancement_report.accepted_count} accepted, "
                f"{enhancement_report.rejected_count} rejected")
        llm_summary = {
            'enabled': bool(llm_config.enabled),
            'model': llm_config.model if llm_config.enabled else None,
            'accepted': enhancement_report.accepted_count if enhancement_report else 0,
            'rejected': enhancement_report.rejected_count if enhancement_report else 0,
        }

        # Create output directory for this job
        output_dir = Path(app.config['OUTPUT_FOLDER']) / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        if enhancement_report is not None:
            # Full per-item record (accepted and rejected, with reasons) for
            # the SME; the job record and API response carry only the summary.
            _atomic_write_json(output_dir / 'enhancement_report.json',
                               enhancement_report.to_dict())

        package_name = secure_filename(sop_content.title.replace(' ', '_')[:50])
        source_filename = Path(file_path).name

        # The exact dicts about to be persisted to job.json - content_hash is
        # computed from these, never re-serialized, so it hashes what job.json
        # (and, below, the export) actually carries.
        module_dict = training_module.to_dict()
        assessment_dict = assessment.to_dict()
        generated_hash = content_hash(module_dict, assessment_dict)
        audit_log.append(
            'content.generated', _anonymous_web_actor(),
            {'questions': len(assessment.questions), 'llm_enhancement': llm_summary},
            generated_hash,
        )

        # Persist the draft *before* exporting, so a job.json always exists
        # once generation has started - this is the SME review record, and it
        # legitimately carries the answer key (SOPContent/TrainingModule/
        # Assessment .to_dict()), same as the existing SME JSON export.
        now = _utcnow_iso()
        job_record = {
            'job_id': job_id,
            'status': 'draft',
            'created_at': now,
            'updated_at': now,
            'source_filename': source_filename,
            'package_name': package_name,
            'request': {
                'num_questions': num_questions,
                'passing_score': passing_score,
                'scorm_version': scorm_version,
                'output_format': output_format,
            },
            'sop_content': sop_content.to_dict(),
            'training_module': module_dict,
            'assessment': assessment_dict,
            'edits_count': 0,
            'edits_by_category': _zero_edit_categories(),
            'edit_rounds': 0,
            'review_opened_at': None,
            'first_edit_at': None,
            'approved_at': None,
            'approval': None,
            'llm_enhancement': llm_summary,
        }
        _atomic_write_json(_job_json_path(job_id), job_record)

        # Step 4: Generate the requested export and the transparency report
        # (this appends this export's own `package.exported` entry - see
        # `_export_outputs`).
        download_filename = _export_outputs(
            output_dir, package_name, sop_content, training_module, assessment,
            scorm_version, output_format, source_filename, audit_log,
            generated_hash, approval=None,
        )

        token = generate_download_token(job_id)

        # Return results
        return {
            'success': True,
            'job_id': job_id,
            'status': job_record['status'],
            'download_url': f'/api/download/{job_id}/{download_filename}?t={token}',
            'transparency_report_url': f'/api/download/{job_id}/transparency_report.html?t={token}',
            'review_url': f'/review/{job_id}?t={token}',
            'metadata': {
                'title': sop_content.title,
                'version': sop_content.version,
                'num_procedures': len(sop_content.procedures),
                'num_safety_warnings': len(sop_content.safety_warnings),
                'num_questions': len(assessment.questions),
                'passing_score': passing_score,
                'estimated_duration': training_module.estimated_duration,
                'learning_objectives': len(training_module.learning_objectives),
                'llm_enhancement': llm_summary,
                'medical_device_mode': True,
                'compliance_questions_included': True,
                'draft_watermarks_added': True
            }
        }

    except RequestValidationError:
        raise
    except Exception as e:
        app.logger.error(f"[job_id={job_id}] Error in process_training: {e}", exc_info=True)
        raise


def _export_outputs(output_dir, package_name, sop_content, training_module, assessment,
                     scorm_version, output_format, source_filename, audit_log,
                     content_hash_value, approval=None, audit_head=None):
    """(Re)generate the requested export and the transparency report for a job.

    Shared by the initial upload, an edit-and-regenerate, and an
    approve-and-regenerate, so all three produce the same file layout for a
    given output_format. `source_filename` is used only for the
    transparency report's "source document" section - it may no longer exist
    on disk by the time this runs (the upload is deleted right after initial
    processing), which `calculate_file_hash` already handles gracefully.

    `audit_log` is the caller's own `AuditLog` (see `_audit_log`) - this
    function never builds its own, so the job id it reads/writes against is
    always the caller's explicit one (`audit_log.job_id`), never guessed from
    `output_dir`'s name. `content_hash_value` is the `content_hash(...)` of
    the exact `training_module`/`assessment` dicts the caller already
    persisted or is about to persist - the same value the caller used for its
    own `content.generated`/`content.edited`/`content.approved` entry.

    `audit_head` (only meaningful for `output_format == 'scorm'`) is the audit
    trail's head hash to anchor into the package's metadata.json
    (`audit_head_hash`; see docs/AUDIT_TRAIL.md "Anchoring the head hash").
    Only `approve_job` passes one - it is the value the trail had immediately
    after `content.approved`, so anyone holding the package can confirm it is
    the content that was approved, not a later edit. The same value is also
    written into a JSON export's `audit_head_hash` key and an HTML export's
    `<meta name="audit-head-hash">` tag, both only when given.

    Ordering matters here and is load-bearing: the artifact is written first,
    then this export's own `package.exported` entry is appended (with the
    artifact's real file hash), and only then is the transparency report
    built and written - so a report on disk always reflects the export that
    produced it (including that very `package.exported` entry), and a failed
    artifact write leaves no report on disk claiming an export that never
    happened.

    Returns the filename of the primary download artifact.
    """
    output_dir = Path(output_dir)

    if output_format == 'scorm':
        exporter = SCORMExporter(scorm_version=scorm_version)
        output_path = exporter.create_package(
            training_module,
            assessment,
            str(output_dir),
            package_name,
            approval=approval,
            audit_head=audit_head,
        )
        download_filename = Path(output_path).name

    elif output_format == 'json':
        json_data = {
            "sop_content": sop_content.to_dict(),
            "training_module": training_module.to_dict(),
            "assessment": assessment.to_dict(),
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "num_questions": len(assessment.questions),
                "passing_score": assessment.passing_score
            }
        }
        if approval:
            json_data["approval"] = approval
        if audit_head:
            json_data["audit_head_hash"] = audit_head
        download_filename = "training_data.json"
        with open(output_dir / download_filename, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)

    elif output_format == 'html':
        download_filename = "training.html"
        html_content = create_standalone_html(training_module, assessment, audit_head=audit_head)
        with open(output_dir / download_filename, 'w', encoding='utf-8') as f:
            f.write(html_content)

    else:
        # Defensive; unreachable because of the validation upstream.
        raise RequestValidationError(f"Unsupported output_format: {output_format}")

    audit_log.append(
        'package.exported', Actor('training-creator', 'application', 'system'),
        {'package_sha256': file_hash(output_dir / download_filename),
         'format': output_format, 'scorm_version': scorm_version,
         'filename': download_filename},
        content_hash_value,
    )

    # Built only after the export's own package.exported entry above, so the
    # report's timeline and package_matches_approval reflect this export, not
    # the one before it.
    audit = audit_block_for_job(output_dir, audit_log.job_id, app.config.get('AUDIT_HMAC_KEY'))
    transparency_report = generate_transparency_report(
        sop_content, training_module, assessment, source_filename,
        approval=approval, audit=audit,
    )
    create_html_report(transparency_report, str(output_dir / 'transparency_report.html'))
    create_json_report(transparency_report, str(output_dir / 'transparency_report.json'))

    return download_filename


@app.route('/api/download/<job_id>/<filename>')
def download_file(job_id, filename):
    """Download generated training package.

    Requires a valid signed token (query param `t`) minted for this exact
    job_id and younger than DOWNLOAD_TTL_SECONDS. Filenames and job ids are
    checked against path traversal, and the resolved path is required to
    stay inside that job's own output directory.
    """
    if not _is_safe_path_component(job_id) or not _is_safe_path_component(filename):
        return jsonify({'error': 'Invalid request'}), 400

    token_error = _verify_job_token(job_id)
    if token_error:
        message, status = token_error
        return jsonify({'error': message}), status

    output_root = Path(app.config['OUTPUT_FOLDER']).resolve()
    job_dir = (output_root / job_id).resolve()

    try:
        job_dir.relative_to(output_root)
        target = (job_dir / filename).resolve()
        target.relative_to(job_dir)
    except ValueError:
        return jsonify({'error': 'Invalid request'}), 400

    if not target.is_file():
        return jsonify({'error': 'File not found'}), 404

    try:
        _audit_log(job_id).append(
            'download.served', _anonymous_web_actor(), {'filename': filename})
    except Exception as e:
        # Never swallow a trail-write failure (docs/AUDIT_TRAIL.md): the
        # request fails rather than a download leaving with no record of it.
        return _internal_error_response(job_id, e)

    return send_from_directory(job_dir, filename, as_attachment=True)


@app.route('/api/audit/<job_id>')
def get_audit_trail(job_id):
    """The job's audit trail plus its own verification result.

    Requires the same signed token as downloads (query param `t`) - 403
    without it, 404 if the job doesn't exist. `hmac_mode` says whether the
    entries carry a `mac` (`AUDIT_HMAC_KEY` set) or are chain-only (unset);
    see docs/AUDIT_TRAIL.md.
    """
    if not _is_safe_path_component(job_id):
        return jsonify({'error': 'Invalid request'}), 400

    token_error = _verify_job_token(job_id)
    if token_error:
        message, status = token_error
        return jsonify({'error': message}), status

    if _read_job_json(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404

    audit_key = app.config.get('AUDIT_HMAC_KEY')
    job_dir = _job_dir(job_id)
    entries = [entry.to_dict() for entry in AuditLog.for_job(job_dir, job_id, audit_key).entries()]
    verification = verify_job(job_dir, job_id, audit_key)

    return jsonify({
        'entries': entries,
        'verification': verification.to_dict(),
        'hmac_mode': 'hmac' if audit_key else 'chain-only',
    }), 200


# --- SME review and approval -------------------------------------------

@app.route('/review/<job_id>')
def review_page(job_id):
    """Server-rendered SME review page: source document vs. generated
    content, editable, with an approval status.

    Requires the same signed token as downloads (query param `t`).
    """
    if not _is_safe_path_component(job_id):
        return 'Invalid request', 400

    token_error = _verify_job_token(job_id)
    if token_error:
        message, status = token_error
        return message, status

    job = _read_job_json(job_id)
    if job is None:
        return 'Job not found', 404

    if job.get('review_opened_at') is None:
        job['review_opened_at'] = _utcnow_iso()
        _atomic_write_json(_job_json_path(job_id), job)
        _audit_log(job_id).append('review.opened', _anonymous_web_actor(), {})

    sop = job.get('sop_content') or {}
    source_lines = (sop.get('raw_content') or '').splitlines()

    return render_template(
        'review.html',
        job_id=job_id,
        token=request.args.get('t', ''),
        status=job.get('status', 'draft'),
        edits_count=job.get('edits_count', 0),
        approval=job.get('approval'),
        sop=sop,
        source_lines=source_lines,
        module=job.get('training_module') or {},
        assessment=job.get('assessment') or {},
        min_questions=MIN_ASSESSMENT_QUESTIONS,
    )


@app.route('/api/review/<job_id>', methods=['POST'])
def review_submit(job_id):
    """Accept SME edits to the generated module/assessment, validate them
    strictly, rebuild the real model objects, and regenerate every export.

    Requires the same signed token as downloads (query param `t`). Body is
    JSON: `{"module": <TrainingModule.to_dict() shape>,
             "assessment": <Assessment.to_dict() shape>}`.
    """
    if not _is_safe_path_component(job_id):
        return jsonify({'error': 'Invalid request'}), 400

    token_error = _verify_job_token(job_id)
    if token_error:
        message, status = token_error
        return jsonify({'error': message}), status

    job = _read_job_json(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Request body must be a JSON object with "module" and "assessment"'}), 400

    module_dict = body.get('module')
    assessment_dict = body.get('assessment')

    try:
        _validate_module_dict(module_dict)
        _validate_assessment_dict(assessment_dict)
    except RequestValidationError as e:
        return jsonify({'error': str(e)}), 400

    try:
        # The untouched generation, snapshotted the first time a job is
        # edited, so edits_count is always measured against what the model
        # produced.
        draft_path = _draft_json_path(job_id)
        if draft_path.is_file():
            try:
                with open(draft_path, encoding='utf-8') as f:
                    draft = json.load(f)
            except (OSError, json.JSONDecodeError):
                draft = {'training_module': job['training_module'], 'assessment': job['assessment']}
        else:
            draft = {'training_module': job['training_module'], 'assessment': job['assessment']}
            _atomic_write_json(draft_path, draft)

        edits_count = (
            _count_edits(draft.get('training_module'), module_dict)
            + _count_edits(draft.get('assessment'), assessment_dict)
        )
        edits_by_category = _count_edits_by_category(
            draft.get('training_module'), module_dict, draft.get('assessment'), assessment_dict)

        sop_content = sop_from_dict(job.get('sop_content'))
        training_module = module_from_dict(module_dict)
        assessment = assessment_from_dict(assessment_dict)

        # Answer *position* is server-owned layout, not SME content (see
        # _relayout_assessment_answers) - re-run it before anything else looks
        # at `correct_answer`. edits_count above was already computed against
        # the SME's raw submission, so this re-layout cannot inflate or hide it.
        _relayout_assessment_answers(assessment, sop_content)

        naive_failures = _naive_strategy_failures(assessment)
        if naive_failures:
            return jsonify({'error': _naive_failure_message(
                naive_failures[0], assessment.passing_score)}), 400

        now = _utcnow_iso()
        edited_assessment_dict = assessment.to_dict()
        job['training_module'] = module_dict
        job['assessment'] = edited_assessment_dict
        job['status'] = 'edited'
        job['edits_count'] = edits_count
        job['edits_by_category'] = edits_by_category
        job['edit_rounds'] = int(job.get('edit_rounds', 0) or 0) + 1
        if job.get('first_edit_at') is None:
            job['first_edit_at'] = now
        job['approval'] = None  # content changed - any prior approval no longer applies
        job['approved_at'] = None
        job['updated_at'] = now
        _atomic_write_json(_job_json_path(job_id), job)

        # `content_hash` is taken from exactly what was just persisted above -
        # the SME's module (as submitted) and the server-relaid-out assessment
        # - never a re-derived approximation of either.
        edited_hash = content_hash(module_dict, edited_assessment_dict)
        audit_log = _audit_log(job_id)
        audit_log.append(
            'content.edited', _anonymous_web_actor(),
            {'edits_count': edits_count, 'edits_by_category': edits_by_category,
             'edit_rounds': job['edit_rounds']},
            edited_hash,
        )

        req = job.get('request', {})
        download_filename = _export_outputs(
            _job_dir(job_id), job.get('package_name'), sop_content, training_module, assessment,
            req.get('scorm_version', '1.2'), req.get('output_format', 'scorm'),
            job.get('source_filename'), audit_log, edited_hash, approval=None,
        )
    except RequestValidationError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return _internal_error_response(job_id, e)

    token = request.args.get('t', '')
    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': job['status'],
        'edits_count': edits_count,
        'edits_by_category': edits_by_category,
        'edit_rounds': job['edit_rounds'],
        # The re-laid-out assessment (server-owned answer positions applied),
        # so the review page can re-render option order without a reload.
        'assessment': assessment.to_dict(),
        'download_url': f'/api/download/{job_id}/{download_filename}?t={token}',
        'transparency_report_url': f'/api/download/{job_id}/transparency_report.html?t={token}',
        'review_url': f'/review/{job_id}?t={token}',
        'metadata': {
            'title': sop_content.title,
            'version': sop_content.version,
            'num_procedures': len(sop_content.procedures),
            'num_safety_warnings': len(sop_content.safety_warnings),
            'num_questions': len(assessment.questions),
            'passing_score': assessment.passing_score,
            'estimated_duration': training_module.estimated_duration,
            'learning_objectives': len(training_module.learning_objectives),
            'medical_device_mode': True,
            'compliance_questions_included': True,
            'draft_watermarks_added': True,
        },
    }), 200


@app.route('/api/approve/<job_id>', methods=['POST'])
def approve_job(job_id):
    """Record a named human's approval of a job and regenerate its package
    with the DRAFT watermark replaced by an approval banner.

    Requires the same signed token as downloads (query param `t`). Body is
    JSON: `{"approved_by": str, "role": str, "notes": str}` - approved_by and
    role are required and non-empty. 409 if the job is already approved.
    """
    if not _is_safe_path_component(job_id):
        return jsonify({'error': 'Invalid request'}), 400

    token_error = _verify_job_token(job_id)
    if token_error:
        message, status = token_error
        return jsonify({'error': message}), status

    job = _read_job_json(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('status') == 'approved':
        return jsonify({'error': 'Job has already been approved'}), 409

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Request body must be a JSON object'}), 400

    approved_by = body.get('approved_by')
    role = body.get('role')
    notes = body.get('notes', '')

    if not isinstance(approved_by, str) or not approved_by.strip():
        return jsonify({'error': 'approved_by is required'}), 400
    if not isinstance(role, str) or not role.strip():
        return jsonify({'error': 'role is required'}), 400
    if notes is None:
        notes = ''
    if not isinstance(notes, str):
        return jsonify({'error': 'notes must be a string'}), 400

    approval = {
        'approved_by': approved_by.strip(),
        'role': role.strip(),
        'approved_at': _utcnow_iso(),
        'notes': notes,
        'edits_count': int(job.get('edits_count', 0)),
    }

    # Atomicity decision (see docs/AUDIT_TRAIL.md's general "append after the
    # state change succeeds" rule, deliberately reversed here): approval is
    # the one action in this app with no natural undo once written - once
    # approval.json exists, a downstream reader (an SME re-opening the review
    # page, a package export) may already treat the job as approved. So for
    # *this* endpoint the audit entry is the state change: `content.approved`
    # is appended to the trail first, and approval.json/job.json are written
    # only after a successful export. If the trail cannot be written, the
    # request fails (500) and nothing on disk records an approval that the
    # trail cannot back up - there is no unrecorded approval to roll back
    # because there is no approval yet. This is the opposite order from
    # content.edited/content.generated (state saved, then recorded), which is
    # fine there because an unrecorded edit is just an edit whose provenance
    # is momentarily thin, not a compliance signature.
    approved_hash = content_hash(job.get('training_module'), job.get('assessment'))
    audit_log = _audit_log(job_id)
    try:
        approval_entry = audit_log.append(
            'content.approved', Actor(approval['approved_by'], approval['role'], 'web'),
            dict(approval),  # the 5 approval keys, verbatim
            approved_hash,
        )
    except Exception as e:
        # Never swallow a trail-write failure: nothing else here has been
        # written yet (see the atomicity note above), so failing now leaves
        # no unrecorded approval on disk.
        return _internal_error_response(job_id, e)

    # The head hash as of this entry - anchored into the package below so a
    # holder of the package can confirm it carries the approved content (see
    # docs/AUDIT_TRAIL.md "Anchoring the head hash"). Computed now, before the
    # export's own package.exported entry extends the chain further.
    audit_head = approval_entry.hash

    # Export BEFORE approval.json/job.json are written (deliberately the
    # opposite of the naive "approve then export" order): `content.approved`
    # above is already permanent and cannot be un-appended, but nothing else
    # here has committed to "this job is approved" yet. If the export raises,
    # we return 500 and write nothing else - approval.json still doesn't
    # exist, job.json's status is still not 'approved' (so this endpoint can
    # be retried), and the trail shows a `content.approved` entry with no
    # `package.exported` after it. That is a legitimate, self-explaining
    # trail state, not a corrupted one: docs/AUDIT_TRAIL.md says plainly that
    # an approval entry without a following export means the export failed,
    # and the next successful approve+export supersedes it. This closes the
    # previous failure mode, where an export error after approval.json/
    # job.json were already written left an approved-looking job on disk
    # while the *previous*, unapproved package was still what a download
    # actually served.
    try:
        sop_content = sop_from_dict(job.get('sop_content'))
        training_module = module_from_dict(job.get('training_module'))
        assessment = assessment_from_dict(job.get('assessment'))

        req = job.get('request', {})
        download_filename = _export_outputs(
            _job_dir(job_id), job.get('package_name'), sop_content, training_module, assessment,
            req.get('scorm_version', '1.2'), req.get('output_format', 'scorm'),
            job.get('source_filename'), audit_log, approved_hash,
            approval=approval, audit_head=audit_head,
        )
    except RequestValidationError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return _internal_error_response(job_id, e)

    _atomic_write_json(_approval_json_path(job_id), approval)

    job['approval'] = approval
    job['status'] = 'approved'
    job['approved_at'] = approval['approved_at']
    job['updated_at'] = _utcnow_iso()
    _atomic_write_json(_job_json_path(job_id), job)

    token = request.args.get('t', '')
    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': job['status'],
        'edits_count': job.get('edits_count', 0),
        'approval': approval,
        'download_url': f'/api/download/{job_id}/{download_filename}?t={token}',
        'transparency_report_url': f'/api/download/{job_id}/transparency_report.html?t={token}',
        'review_url': f'/review/{job_id}?t={token}',
        'metadata': {
            'title': sop_content.title,
            'version': sop_content.version,
            'num_procedures': len(sop_content.procedures),
            'num_safety_warnings': len(sop_content.safety_warnings),
            'num_questions': len(assessment.questions),
            'passing_score': assessment.passing_score,
            'estimated_duration': training_module.estimated_duration,
            'learning_objectives': len(training_module.learning_objectives),
            'medical_device_mode': True,
            'compliance_questions_included': True,
            'draft_watermarks_added': False,
        },
    }), 200


# --- Pilot metrics --------------------------------------------------------
# GOAL.md's M1 exit criterion ("SMEs accept generated content with <30%
# edits") needs measurement across real pilot SMEs and SOPs; these two
# routes are that instrumentation. Both are disabled (404, not merely
# unauthorized) unless PILOT_METRICS_TOKEN is set in the environment, so a
# default deployment never exposes SME edit data - which can include
# free-text approval notes - to an unauthenticated caller.

def _check_pilot_token():
    """Return None if the request is authorized for the pilot metrics
    routes, else an (error_message, status_code) pair - 404 if the feature
    is disabled (no PILOT_METRICS_TOKEN configured), 403 if a token was
    required but the one supplied (bearer header or `?token=`) doesn't
    match."""
    configured = os.environ.get('PILOT_METRICS_TOKEN')
    if not configured:
        return ('Not found', 404)

    provided = request.args.get('token')
    if not provided:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            provided = auth_header[len('Bearer '):]

    if not provided or not secrets.compare_digest(provided, configured):
        return ('Forbidden', 403)

    return None


@app.route('/pilot/metrics')
def pilot_metrics_json():
    """JSON aggregate pilot metrics (see src/pilot_metrics.py for the exact
    shape and the edit-rate definition)."""
    token_error = _check_pilot_token()
    if token_error:
        message, status = token_error
        return jsonify({'error': message}), status

    metrics = collect_pilot_metrics(app.config['OUTPUT_FOLDER'])
    return jsonify(metrics), 200


@app.route('/pilot')
def pilot_dashboard():
    """Server-rendered pilot dashboard: the same aggregates as
    /pilot/metrics, plus a per-job table linking to each job's review page
    (tokens minted the same way /api/upload mints them)."""
    token_error = _check_pilot_token()
    if token_error:
        message, status = token_error
        return message, status

    metrics = collect_pilot_metrics(app.config['OUTPUT_FOLDER'])
    review_urls = {}
    for job in metrics.get('jobs', []):
        job_id = job.get('job_id')
        if job_id:
            review_urls[job_id] = f"/review/{job_id}?t={generate_download_token(job_id)}"

    return render_template('pilot.html', metrics=metrics, review_urls=review_urls)


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'version': '1.0.0'}), 200


@app.errorhandler(413)
def handle_file_too_large(e):
    return jsonify({'error': 'File too large. Reduce the file size and try again.'}), 413


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(e):
    return jsonify({'error': 'File too large. Reduce the file size and try again.'}), 413


def create_standalone_html(training_module, assessment, audit_head=None):
    """Create a standalone HTML file with all content.

    Note: this escapes text pulled from the document so it can't break out
    of the surrounding markup, but it still ships the full question payload
    (including correct_answer) to the browser. That is a learner-facing
    integrity issue the assessment module is expected to fix by exposing a
    learner-safe question payload; once it does, this export path should be
    updated to use it instead of `q.to_dict()`-shaped data.

    `audit_head` mirrors the SCORM export's `metadata.json` anchor
    (docs/AUDIT_TRAIL.md "Anchoring the head hash"): when given (only ever
    the case for an approved job - see `_export_outputs`), it is embedded
    verbatim as `<meta name="audit-head-hash">` so a holder of this file can
    compare it against the `content.approved` entry in the job's trail.
    """
    sections_html = ""
    for section in training_module.sections:
        # Section content is pre-rendered HTML from the generator, not
        # escaped here (it's already markup, not raw user/document text).
        sections_html += f"<div class='section'>{section.get('content', '')}</div>"

    questions_html = ""
    for idx, q in enumerate(assessment.questions, 1):
        questions_html += f"""
        <div class='question'>
            <p><strong>Question {idx}:</strong> {html.escape(q.text)}</p>
            <ul>
        """
        for option in q.options:
            questions_html += f"<li>{html.escape(str(option))}</li>"
        questions_html += "</ul></div>"

    title = html.escape(training_module.title)
    audit_meta = (
        f'\n    <meta name="audit-head-hash" content="{html.escape(audit_head)}">'
        if audit_head else ""
    )

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>{audit_meta}
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        .section {{ margin: 30px 0; padding: 20px; background: #f8f9fa; border-radius: 5px; }}
        .question {{ margin: 20px 0; padding: 15px; background: #fff; border-left: 4px solid #3498db; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    {sections_html}
    <h2>Assessment</h2>
    {questions_html}
</body>
</html>"""
    return html_doc


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=debug, host=host, port=port)
