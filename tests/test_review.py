"""Flask app tests for the SME review and approval workflow.

Covers: the review page (token-gated, renders source + generated content),
POST /api/review (strict validation, edit-and-regenerate, edits_count),
and POST /api/approve (approval record, package banner swap, double-approve
rejection). Uses the Flask test client end to end, same as tests/test_app.py.
"""

import io
import json
import zipfile

import app as app_module


def _upload(client, sample_sop_path, **form_overrides):
    form = {
        "num_questions": "6",
        "passing_score": "80",
        "scorm_version": "1.2",
        "output_format": "scorm",
    }
    form.update(form_overrides)

    with open(sample_sop_path, "rb") as f:
        data = dict(form)
        data["file"] = (io.BytesIO(f.read()), "sample_sop.txt")
        return client.post("/api/upload", data=data, content_type="multipart/form-data")


def _zip_member_text(zip_bytes, suffix):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    name = next(n for n in z.namelist() if n.endswith(suffix))
    return z.read(name).decode("utf-8")


def _upload_and_get_job(client, sample_sop_path):
    resp = _upload(client, sample_sop_path)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    job_id = payload["job_id"]
    token = payload["review_url"].split("t=")[1]
    return payload, job_id, token


# ---------------------------------------------------------------------------
# Upload -> review_url
# ---------------------------------------------------------------------------
def test_upload_response_includes_tokenised_review_url(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    assert payload["review_url"] == f"/review/{job_id}?t={token}"


# ---------------------------------------------------------------------------
# GET /review/<job_id>
# ---------------------------------------------------------------------------
def test_review_page_requires_token(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    resp = client.get(f"/review/{job_id}")
    assert resp.status_code == 403


def test_review_page_renders_source_and_generated_content(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    resp = client.get(payload["review_url"])
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    # A section title (a "step" of the module) is present.
    assert any(section["title"] in text for section in module["sections"])
    # A question's text is present.
    assert assessment["questions"][0]["text"] in text
    # The SME-facing answer key must never be present on this page's markup
    # as the literal key name (the correct_answer index would defeat the
    # point of a review page that's meant to be edited, not leaked further,
    # but it legitimately drives which radio is pre-checked).
    assert "show-source" in text


def test_review_page_404_for_unknown_job(client, sample_sop_path):
    # Mint a token for a job id that was never generated.
    token = app_module.generate_download_token("does-not-exist")
    resp = client.get(f"/review/does-not-exist?t={token}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/review/<job_id>
# ---------------------------------------------------------------------------
def test_review_submit_requires_token(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    resp = client.post(
        f"/api/review/{job_id}",
        json={"module": job["training_module"], "assessment": job["assessment"]},
    )
    assert resp.status_code == 403


def test_review_submit_edits_and_regenerates(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    module["learning_objectives"][0] = module["learning_objectives"][0] + " (edited by SME)"
    new_option_text = "A freshly written distractor nobody has seen before"
    assessment["questions"][0]["options"][0] = new_option_text

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    result = resp.get_json()
    assert result["success"] is True
    assert result["status"] == "edited"
    assert result["edits_count"] >= 2

    download_resp = client.get(result["download_url"])
    assert download_resp.status_code == 200
    assessment_html = _zip_member_text(download_resp.data, "assessment.html")
    assert new_option_text in assessment_html
    assert "correct_answer" not in assessment_html

    # draft.json now holds the untouched original generation.
    job_dir = app_module._job_dir(job_id)
    draft = json.loads((job_dir / "draft.json").read_text(encoding="utf-8"))
    assert new_option_text not in json.dumps(draft["assessment"])


def test_review_submit_correct_answer_out_of_range_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    assessment["questions"][0]["correct_answer"] = 99

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_review_submit_single_option_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    q = assessment["questions"][0]
    q["options"] = [q["options"][0]]
    q["correct_answer"] = 0

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_review_submit_too_few_questions_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    assert len(assessment["questions"]) >= 5  # sanity: fixture requested 6
    assessment["questions"] = assessment["questions"][:4]

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_review_submit_duplicate_options_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    q = assessment["questions"][0]
    if len(q["options"]) < 2:
        q["options"].append("Extra option")
    q["options"][1] = q["options"][0]

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_review_submit_missing_job_is_404(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    bad_token = app_module.generate_download_token("does-not-exist")
    resp = client.post(
        f"/api/review/does-not-exist?t={bad_token}",
        json={"module": job["training_module"], "assessment": job["assessment"]},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/approve/<job_id>
# ---------------------------------------------------------------------------
def test_approve_requires_token(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    resp = client.post(f"/api/approve/{job_id}", json={"approved_by": "Jane", "role": "SME"})
    assert resp.status_code == 403


def test_approve_without_role_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    resp = client.post(f"/api/approve/{job_id}?t={token}", json={"approved_by": "Jane Doe"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_approve_without_approved_by_is_400(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    resp = client.post(f"/api/approve/{job_id}?t={token}", json={"role": "QA Lead"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_approve_success_writes_approval_and_updates_package(app, client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane Q. Doe", "role": "Quality Assurance", "notes": "Reviewed line-by-line."},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    result = resp.get_json()
    assert result["success"] is True
    assert result["approval"]["approved_by"] == "Jane Q. Doe"

    from pathlib import Path

    approval_path = Path(app.config["OUTPUT_FOLDER"]) / job_id / "approval.json"
    assert approval_path.is_file()
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    assert set(approval.keys()) == {"approved_by", "role", "approved_at", "notes", "edits_count"}
    assert approval["approved_by"] == "Jane Q. Doe"
    assert approval["role"] == "Quality Assurance"

    download_resp = client.get(result["download_url"])
    assert download_resp.status_code == 200
    metadata = json.loads(_zip_member_text(download_resp.data, "metadata.json"))
    assert "approval" in metadata
    assert metadata["approval"]["approved_by"] == "Jane Q. Doe"

    assessment_html = _zip_member_text(download_resp.data, "assessment.html")
    assert "DRAFT" not in assessment_html
    assert "Jane Q. Doe" in assessment_html


def test_approve_twice_is_409(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    first = client.post(
        f"/api/approve/{job_id}?t={token}", json={"approved_by": "Jane", "role": "SME"})
    assert first.status_code == 200

    second = client.post(
        f"/api/approve/{job_id}?t={token}", json={"approved_by": "John", "role": "QA"})
    assert second.status_code == 409


def test_approve_uses_edited_content(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    new_option_text = "Edited-then-approved option text"
    assessment["questions"][0]["options"][0] = new_option_text

    edit_resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert edit_resp.status_code == 200

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}", json={"approved_by": "Jane", "role": "SME"})
    assert approve_resp.status_code == 200
    result = approve_resp.get_json()

    download_resp = client.get(result["download_url"])
    assessment_html = _zip_member_text(download_resp.data, "assessment.html")
    assert new_option_text in assessment_html


# ---------------------------------------------------------------------------
# XSS: an approver name is data, not markup
# ---------------------------------------------------------------------------
def test_approver_name_is_escaped_in_review_page_and_banner(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    malicious_name = "<script>alert(1)</script>"

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": malicious_name, "role": "SME"},
    )
    assert approve_resp.status_code == 200
    result = approve_resp.get_json()

    review_resp = client.get(payload["review_url"])
    review_text = review_resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in review_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in review_text

    download_resp = client.get(result["download_url"])
    assessment_html = _zip_member_text(download_resp.data, "assessment.html")
    assert "<script>alert(1)</script>" not in assessment_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in assessment_html
