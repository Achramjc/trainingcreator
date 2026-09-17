"""
Web Application for Training Creator
Flask-based web interface for converting SOPs to training materials
"""

import html
import logging
import os
import secrets
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, render_template, jsonify, send_from_directory
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import json

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import (
    MIN_ASSESSMENT_QUESTIONS,
    AssessmentGenerator,
    MedicalDeviceAssessmentGenerator,
)
from src.scorm_exporter import SCORMExporter
from src.transparency_report import generate_transparency_report, create_html_report, create_json_report
from src.medical_device_config import MEDICAL_DEVICE_CONFIG

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
ALLOWED_OUTPUT_FORMATS = {'scorm', 'json', 'html'}
ALLOWED_SCORM_VERSIONS = {'1.2', '2004'}

DOWNLOAD_TOKEN_SALT = 'training-creator-download-v1'


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# --- Retention -------------------------------------------------------------
# M0 note: this is a simple synchronous local-disk sweep. M2 moves storage to
# object storage (e.g. S3) with bucket lifecycle rules instead of app-managed
# deletion; see DEPLOYMENT.md.

def _sweep_expired_outputs(output_folder, retention_seconds):
    """Delete job directories under output_folder older than retention_seconds."""
    folder = Path(output_folder)
    if not folder.exists():
        return
    now = time.time()
    for job_dir in folder.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            age = now - job_dir.stat().st_mtime
        except OSError:
            continue
        if age > retention_seconds:
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


@app.route('/')
def index():
    """Serve the main application page"""
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handle file upload and initiate processing"""
    upload_path = None
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
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.error(f"Error processing upload: {str(e)}")
        return jsonify({'error': f'Processing failed: {str(e)}'}), 500
    finally:
        if upload_path is not None:
            _cleanup_upload(upload_path)


def process_training(file_path, job_id, num_questions, passing_score, scorm_version, output_format):
    """Process SOP and generate training package"""
    if output_format not in ALLOWED_OUTPUT_FORMATS:
        raise ValueError(f"Unsupported output_format: {output_format}")
    if scorm_version not in ALLOWED_SCORM_VERSIONS:
        raise ValueError(f"Unsupported scorm_version: {scorm_version}")

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

        # Create output directory for this job
        output_dir = Path(app.config['OUTPUT_FOLDER']) / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate transparency report for medical device compliance
        transparency_report = generate_transparency_report(
            sop_content,
            training_module,
            assessment,
            file_path
        )

        # Save transparency report as HTML and JSON
        create_html_report(transparency_report, str(output_dir / 'transparency_report.html'))
        create_json_report(transparency_report, str(output_dir / 'transparency_report.json'))

        # Step 4: Export based on format
        if output_format == 'scorm':
            exporter = SCORMExporter(scorm_version=scorm_version)
            package_name = secure_filename(sop_content.title.replace(' ', '_')[:50])
            output_path = exporter.create_package(
                training_module,
                assessment,
                str(output_dir),
                package_name
            )
            download_filename = Path(output_path).name

        elif output_format == 'json':
            # Export as JSON
            json_data = {
                "sop_content": sop_content.to_dict(),
                "training_module": training_module.to_dict(),
                "assessment": assessment.to_dict(),
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "num_questions": num_questions,
                    "passing_score": passing_score
                }
            }
            download_filename = "training_data.json"
            output_path = output_dir / download_filename
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)

        elif output_format == 'html':
            # Export as standalone HTML
            download_filename = "training.html"
            output_path = output_dir / download_filename
            html_content = create_standalone_html(training_module, assessment)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

        else:
            # Defensive; unreachable because of the validation above.
            raise ValueError(f"Unsupported output_format: {output_format}")

        download_token = generate_download_token(job_id)

        # Return results
        return {
            'success': True,
            'job_id': job_id,
            'download_url': f'/api/download/{job_id}/{download_filename}?t={download_token}',
            'transparency_report_url': f'/api/download/{job_id}/transparency_report.html?t={download_token}',
            'metadata': {
                'title': sop_content.title,
                'version': sop_content.version,
                'num_procedures': len(sop_content.procedures),
                'num_safety_warnings': len(sop_content.safety_warnings),
                'num_questions': len(assessment.questions),
                'passing_score': passing_score,
                'estimated_duration': training_module.estimated_duration,
                'learning_objectives': len(training_module.learning_objectives),
                'medical_device_mode': True,
                'compliance_questions_included': True,
                'draft_watermarks_added': True
            }
        }

    except Exception as e:
        app.logger.error(f"[job_id={job_id}] Error in process_training: {str(e)}")
        raise


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

    token = request.args.get('t')
    if not token:
        return jsonify({'error': 'Missing download token'}), 403

    ttl = int(app.config.get('DOWNLOAD_TTL_SECONDS', 24 * 60 * 60))
    try:
        token_job_id = _serializer().loads(token, max_age=ttl)
    except SignatureExpired:
        return jsonify({'error': 'Download link has expired'}), 403
    except BadSignature:
        return jsonify({'error': 'Invalid download token'}), 403

    if token_job_id != job_id:
        return jsonify({'error': 'Invalid download token'}), 403

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

    return send_from_directory(job_dir, filename, as_attachment=True)


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


def create_standalone_html(training_module, assessment):
    """Create a standalone HTML file with all content.

    Note: this escapes text pulled from the document so it can't break out
    of the surrounding markup, but it still ships the full question payload
    (including correct_answer) to the browser. That is a learner-facing
    integrity issue the assessment module is expected to fix by exposing a
    learner-safe question payload; once it does, this export path should be
    updated to use it instead of `q.to_dict()`-shaped data.
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

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
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
