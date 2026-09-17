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
    tampered_url = download_url[:-1] + ("x" if download_url[-1] != "x" else "y")
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
