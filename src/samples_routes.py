"""
Sample-SOP gallery routes.

Coordinator note: the only integration point this module needs in app.py is

    from src.samples_routes import samples_bp
    app.register_blueprint(samples_bp)

added once, anywhere after ``app = Flask(__name__)``. Everything else -
validation constants, the processing pipeline, upload-dir handling - is
imported lazily *inside* the view functions below, specifically to avoid a
circular import at module load time (app.py will eventually import this
module, so this module must not import app.py at the top level).

Routes:
    GET  /api/samples                       -> the gallery catalog (no file paths)
    POST /api/samples/<sample_id>/generate   -> run the same pipeline as
                                                 /api/upload against a gallery
                                                 document instead of an upload
"""

import shutil
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from src.samples import UnknownSampleError, list_samples, sample_path

samples_bp = Blueprint("samples", __name__)

#: Keys from the catalog that are safe to expose to a browser. Deliberately
#: excludes "path" / "resolved_path" - the client never learns the on-disk
#: layout of the gallery.
_PUBLIC_CATALOG_KEYS = (
    "id",
    "title",
    "regime",
    "standard",
    "format",
    "description",
    "steps_expected",
    "warnings_expected",
)


def _public_catalog():
    return [
        {key: entry[key] for key in _PUBLIC_CATALOG_KEYS if key in entry}
        for entry in list_samples()
    ]


@samples_bp.route("/api/samples", methods=["GET"])
def list_samples_route():
    """The gallery catalog, safe for the browser: no file paths."""
    return jsonify({"samples": _public_catalog()}), 200


@samples_bp.route("/api/samples/<sample_id>/generate", methods=["POST"])
def generate_sample_route(sample_id):
    """Run the exact /api/upload pipeline against a gallery document.

    Accepts the same form fields as /api/upload (num_questions,
    passing_score, scorm_version, output_format), validated against the same
    constants app.py uses, so a sample and an upload are held to identical
    rules. The sample file is copied into the job's own upload directory
    (rather than pointed at directly) so retention and cleanup behave
    exactly like a real upload: process_training consumes it and the source
    copy is deleted afterwards, never the gallery original.
    """
    # Imported lazily to avoid a circular import (app.py imports this
    # module's blueprint); see the module docstring.
    from app import (
        ALLOWED_OUTPUT_FORMATS,
        ALLOWED_SCORM_VERSIONS,
        MIN_ASSESSMENT_QUESTIONS,
        RequestValidationError,
        _cleanup_upload,
        _internal_error_response,
        app as flask_app,
        process_training,
    )

    try:
        source_path = sample_path(sample_id)
    except (ValueError, UnknownSampleError):
        return jsonify({"error": f"Unknown sample id: {sample_id}"}), 404

    if not source_path.exists():
        # Catalog names a file that isn't actually on disk - a packaging bug,
        # not a caller error, but still not something to serve.
        return jsonify({"error": f"Unknown sample id: {sample_id}"}), 404

    try:
        num_questions = int(request.form.get("num_questions", 5))
        passing_score = int(request.form.get("passing_score", 70))
    except (TypeError, ValueError):
        return jsonify({"error": "num_questions and passing_score must be integers"}), 400

    scorm_version = request.form.get("scorm_version", "1.2")
    output_format = request.form.get("output_format", "scorm")

    if not MIN_ASSESSMENT_QUESTIONS <= num_questions <= 20:
        return jsonify({
            "error": f"Number of questions must be between {MIN_ASSESSMENT_QUESTIONS} and 20"
        }), 400
    if not 50 <= passing_score <= 100:
        return jsonify({"error": "Passing score must be between 50 and 100"}), 400
    if output_format not in ALLOWED_OUTPUT_FORMATS:
        return jsonify({
            "error": f"Invalid output_format. Must be one of: {', '.join(sorted(ALLOWED_OUTPUT_FORMATS))}"
        }), 400
    if scorm_version not in ALLOWED_SCORM_VERSIONS:
        return jsonify({
            "error": f"Invalid scorm_version. Must be one of: {', '.join(sorted(ALLOWED_SCORM_VERSIONS))}"
        }), 400

    job_id = str(uuid.uuid4())
    filename = secure_filename(source_path.name)
    upload_path = Path(flask_app.config["UPLOAD_FOLDER"]) / job_id / filename
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source_path), str(upload_path))

    flask_app.logger.info(f"[job_id={job_id}] Generating from sample: {sample_id}")

    try:
        result = process_training(
            str(upload_path),
            job_id,
            num_questions,
            passing_score,
            scorm_version,
            output_format,
        )
        flask_app.logger.info(f"[job_id={job_id}] Processing complete")
        return jsonify(result), 200
    except RequestValidationError as e:
        # Raised by the app's own validation: written for the caller.
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001 - mirrors /api/upload's error handling
        # Anything else is logged in full server-side; the client gets a
        # generic message plus the job id, never the exception text.
        return _internal_error_response(job_id, e)
    finally:
        _cleanup_upload(upload_path)
