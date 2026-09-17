"""
Flask app tests using Flask's test client.

Covers: health check, upload validation, output-format/scorm-version
validation, oversized upload handling, the full upload->download flow for
each output format, download-token authorization (missing/tampered/expired),
path traversal rejection, and upload retention (source file removed after
processing).
"""

import io
import time

import json
from pathlib import Path

import app as app_module


def _upload(client, sample_sop_path, **form_overrides):
    form = {
        "num_questions": "5",
        "passing_score": "80",
        "scorm_version": "1.2",
        "output_format": "scorm",
    }
    form.update(form_overrides)

    with open(sample_sop_path, "rb") as f:
        data = dict(form)
        data["file"] = (io.BytesIO(f.read()), "sample_sop.txt")
        return client.post("/api/upload", data=data, content_type="multipart/form-data")


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "healthy"


def test_upload_no_file(client):
    resp = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_disallowed_extension(client):
    data = {
        "file": (io.BytesIO(b"not allowed"), "malware.exe"),
    }
    resp = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_bad_num_questions(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, num_questions="not-a-number")
    assert resp.status_code == 400

    resp2 = _upload(client, sample_sop_path, num_questions="9999")
    assert resp2.status_code == 400


def test_upload_bad_output_format(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="xapi")
    assert resp.status_code == 400


def test_upload_bad_scorm_version(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, scorm_version="3.0")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Internal errors must not leak into the response
# ---------------------------------------------------------------------------

def test_upload_internal_error_hides_exception_text_but_keeps_job_id(
        client, sample_sop_path, monkeypatch):
    """An unexpected failure deep in the pipeline (here: the parser) must
    not leak its exception text to the client - only a generic message and
    the job id, so support can find the matching log line."""
    fixed_job_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(app_module.uuid, "uuid4", lambda: fixed_job_id)

    def boom(self, file_path):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app_module.SOPParser, "parse", boom)

    resp = _upload(client, sample_sop_path)
    assert resp.status_code == 500

    body = resp.get_data(as_text=True)
    assert "secret internal detail" not in body
    assert fixed_job_id in body


def test_upload_validation_error_still_returns_precise_message(client, sample_sop_path):
    """A validation error the app itself raises (not an internal failure)
    keeps its precise, user-facing message."""
    resp = _upload(client, sample_sop_path, num_questions="9999")
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "Number of questions must be between" in error


def test_oversized_upload_returns_json_413(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 100  # tiny, for the test
    try:
        data = {
            "file": (io.BytesIO(b"x" * 1000), "sample_sop.txt"),
        }
        resp = client.post("/api/upload", data=data, content_type="multipart/form-data")
        assert resp.status_code == 413
        assert resp.is_json
        assert "error" in resp.get_json()
    finally:
        app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


def test_upload_success_each_format(client, sample_sop_path):
    for fmt in ("scorm", "json", "html"):
        resp = _upload(client, sample_sop_path, output_format=fmt)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        payload = resp.get_json()
        assert payload["success"] is True
        assert "download_url" in payload
        assert "transparency_report_url" in payload
        assert "review_url" in payload
        assert payload["review_url"].startswith(f"/review/{payload['job_id']}?t=")

        download_resp = client.get(payload["download_url"])
        assert download_resp.status_code == 200

        report_resp = client.get(payload["transparency_report_url"])
        assert report_resp.status_code == 200


def test_download_requires_token(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="json")
    payload = resp.get_json()
    download_url = payload["download_url"]

    # Strip the token entirely.
    base_url = download_url.split("?")[0]
    stripped_resp = client.get(base_url)
    assert stripped_resp.status_code == 403

    # Tamper with the token.
    # Flip a character well inside the signature, not the last one: the final
    # base64url character of an itsdangerous token has slack bits, so several
    # values decode to the same HMAC byte and the "tampered" link would still
    # verify roughly 7% of the time.
    cut = len(download_url) - 8
    tampered_url = download_url[:cut] + ("x" if download_url[cut] != "x" else "y") + download_url[cut + 1:]
    tampered_resp = client.get(tampered_url)
    assert tampered_resp.status_code == 403


def test_download_token_expired(app, client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="json")
    payload = resp.get_json()
    download_url = payload["download_url"]

    # Force every token to be considered expired regardless of age.
    app.config["DOWNLOAD_TTL_SECONDS"] = -1
    try:
        expired_resp = client.get(download_url)
        assert expired_resp.status_code == 403
    finally:
        app.config["DOWNLOAD_TTL_SECONDS"] = 24 * 60 * 60


def test_download_token_job_id_mismatch(client, sample_sop_path):
    resp1 = _upload(client, sample_sop_path, output_format="json")
    resp2 = _upload(client, sample_sop_path, output_format="json")

    job1_id = resp1.get_json()["job_id"]
    token2 = resp2.get_json()["download_url"].split("t=")[1]

    # Use job 2's token against job 1's download URL.
    mismatched_url = f"/api/download/{job1_id}/training_data.json?t={token2}"
    resp = client.get(mismatched_url)
    assert resp.status_code == 403


def test_download_path_traversal_rejected(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="json")
    payload = resp.get_json()
    job_id = payload["job_id"]
    token = payload["download_url"].split("t=")[1]

    traversal_url = f"/api/download/{job_id}/..%2f..%2fapp.py?t={token}"
    resp = client.get(traversal_url)
    assert 400 <= resp.status_code < 500

    dotdot_url = f"/api/download/{job_id}/..?t={token}"
    resp2 = client.get(dotdot_url)
    assert 400 <= resp2.status_code < 500


def test_download_missing_file_is_404(client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="json")
    payload = resp.get_json()
    job_id = payload["job_id"]
    token = payload["download_url"].split("t=")[1]

    missing_url = f"/api/download/{job_id}/does_not_exist.json?t={token}"
    resp = client.get(missing_url)
    assert resp.status_code == 404


def test_uploaded_source_file_removed_after_processing(app, client, sample_sop_path):
    resp = _upload(client, sample_sop_path, output_format="json")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    from pathlib import Path
    upload_job_dir = Path(app.config["UPLOAD_FOLDER"]) / job_id
    # The source file (and its now-empty job directory) must not remain.
    assert not upload_job_dir.exists() or not any(upload_job_dir.iterdir())


def test_is_safe_path_component_helper():
    assert app_module._is_safe_path_component("training_data.json")
    assert not app_module._is_safe_path_component("../../etc/passwd")
    assert not app_module._is_safe_path_component("..")
    assert not app_module._is_safe_path_component("a/b")
    assert not app_module._is_safe_path_component("a\\b")
    assert not app_module._is_safe_path_component("")


# ---------------------------------------------------------------------------
# Optional LLM enhancement wiring (docs/LLM.md) - never touches the network
# ---------------------------------------------------------------------------
def _upload_sample(client, sample_sop_path):
    import io as _io
    with open(sample_sop_path, 'rb') as f:
        data = {
            'num_questions': '6', 'passing_score': '80',
            'scorm_version': '1.2', 'output_format': 'scorm',
            'file': (_io.BytesIO(f.read()), 'sample_sop.txt'),
        }
        return client.post('/api/upload', data=data, content_type='multipart/form-data')


def test_llm_enhancement_is_off_by_default(client, sample_sop_path, monkeypatch):
    monkeypatch.delenv('TRAINING_CREATOR_LLM', raising=False)
    resp = _upload_sample(client, sample_sop_path)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload['metadata']['llm_enhancement'] == {
        'enabled': False, 'model': None, 'accepted': 0, 'rejected': 0}
    job_dir = Path(app_module.app.config['OUTPUT_FOLDER']) / payload['job_id']
    assert not (job_dir / 'enhancement_report.json').exists()


def test_llm_enhancement_runs_with_injected_provider_and_fails_closed(
        client, sample_sop_path, monkeypatch):
    """With the layer enabled and a provider that returns nothing usable, the
    pipeline completes with deterministic content, writes the per-item report,
    and reports zero accepted items - no crash, no silent enhancement."""
    from src.llm import FakeProvider

    created = []

    def fake_factory(config):
        provider = FakeProvider([])  # every call -> explicit 'exhausted' error
        created.append(provider)
        return provider

    monkeypatch.setenv('TRAINING_CREATOR_LLM', 'anthropic')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-never-used')
    monkeypatch.setattr(app_module, 'build_llm_provider', fake_factory)

    resp = _upload_sample(client, sample_sop_path)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    summary = payload['metadata']['llm_enhancement']
    assert summary['enabled'] is True and summary['accepted'] == 0
    assert len(created) == 1, 'provider must be built once and shared'
    assert created[0].calls, 'the enhancement layer must have been invoked'

    job_dir = Path(app_module.app.config['OUTPUT_FOLDER']) / payload['job_id']
    report = json.loads((job_dir / 'enhancement_report.json').read_text(encoding='utf-8'))
    assert report['enabled'] is True and report['accepted_count'] == 0
    assert 'test-key-never-used' not in json.dumps(report)

    job = json.loads((job_dir / 'job.json').read_text(encoding='utf-8'))
    assert job['llm_enhancement']['enabled'] is True
    # Deterministic content still present and the learner package still clean.
    assert job['training_module']['learning_objectives']
    zip_resp = client.get(payload['download_url'])
    assert zip_resp.status_code == 200
    import zipfile as _zipfile, io as _io2
    html = _zipfile.ZipFile(_io2.BytesIO(zip_resp.data)).read('assessment.html').decode('utf-8')
    assert 'correct_answer' not in html
