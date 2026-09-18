"""Flask app tests for the SME review and approval workflow.

Covers: the review page (token-gated, renders source + generated content),
POST /api/review (strict validation, edit-and-regenerate, edits_count),
and POST /api/approve (approval record, package banner swap, double-approve
rejection). Uses the Flask test client end to end, same as tests/test_app.py.
"""

import io
import json
import zipfile
from pathlib import Path

import app as app_module
from src.assessments import naive_strategies, strategy_pick
from src.serialization import assessment_from_dict

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Chosen (see tests below) because forcing every correct_answer to 0 on this
#: fixture, at this question count, does not *also* coincidentally trip a
#: length-based naive strategy purely from which distractor text happens to
#: already sit at index 0 before the edit - keeping the position-relayout
#: tests focused on the one thing they're testing.
NUMBERED_SOP_PATH = str(REPO_ROOT / "examples" / "sample_sop_numbered.txt")


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


def test_review_submit_validation_error_returns_precise_message(client, sample_sop_path):
    """RequestValidationError (raised by this app's own _validate_module_dict
    / _validate_assessment_dict) is a message written for the SME, and must
    reach them verbatim rather than being swallowed by the generic-error
    handling that covers unexpected internal failures."""
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    module["title"] = ""  # invalid: _validate_module_dict requires non-empty
    assessment = job["assessment"]

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "module.title must be a non-empty string"


def test_review_submit_internal_error_hides_exception_but_keeps_job_id(
        client, sample_sop_path, monkeypatch):
    """An unexpected failure while rebuilding/exporting an edited job (not a
    validation error) must not leak its exception text - only a generic
    message and the job id."""
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    def boom(data):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app_module, "sop_from_dict", boom)

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "secret internal detail" not in body
    assert job_id in body


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


# ---------------------------------------------------------------------------
# Answer position is server-owned layout, not SME content (CLAUDE.md
# invariant #1: "a naive learner must fail"). An SME edit that sets the
# correct_answer index in a way a naive learner could exploit must be
# silently corrected (position), or rejected outright when the exploit is in
# the option *text* itself (position can't fix that).
# ---------------------------------------------------------------------------
def test_review_submit_relayouts_answers_gamed_by_position(client):
    """Setting every correct_answer to 0 must not survive into the saved
    assessment as-is: the server re-lays-out positions so "always option 1"
    (and every other fixed-position strategy) still fails against this
    assessment's own passing score."""
    payload, job_id, token = _upload_and_get_job(client, NUMBERED_SOP_PATH)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    for q in assessment["questions"]:
        if q["type"] != "true_false":
            q["correct_answer"] = 0

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    result = resp.get_json()

    mc_positions = [
        q["correct_answer"] for q in result["assessment"]["questions"]
        if q["type"] != "true_false"
    ]
    assert len(mc_positions) >= 2
    # Not every correct answer landed back on position 0 - the whole point of
    # the exploit the SME just (naively or otherwise) tried.
    assert any(p != 0 for p in mc_positions)

    # Independently re-derive the invariant #1 check (not the app's own
    # helper) against the *saved* job: no fixed-position strategy reaches the
    # passing score.
    saved = app_module._read_job_json(job_id)
    rebuilt = assessment_from_dict(saved["assessment"])
    total_points = sum(q.points for q in rebuilt.questions)
    max_options = max(len(q.options) for q in rebuilt.questions)
    for strategy in naive_strategies(max_options):
        earned = sum(
            q.points for q in rebuilt.questions
            if strategy_pick(strategy, len(q.options)) == q.correct_answer
        )
        pct = 100.0 * earned / total_points
        assert pct < rebuilt.passing_score, (
            f"strategy {strategy} scores {pct:.1f}% after the SME's edit was saved"
        )


def test_review_submit_edits_count_not_inflated_by_relayout(client):
    """edits_count is measured against what the SME actually submitted, not
    against the server's post-relayout result - so redistributing answer
    positions must never show up as extra edits."""
    payload, job_id, token = _upload_and_get_job(client, NUMBERED_SOP_PATH)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    # A single, unrelated content edit - no question touched at all, so any
    # position changes the server makes are entirely its own doing.
    module["learning_objectives"][0] = module["learning_objectives"][0] + " (minor tweak)"

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["edits_count"] == 1


# ---------------------------------------------------------------------------
# Richer edit accounting: edits_by_category, edit_rounds, and the review/edit/
# approve timestamps (Stream G pilot instrumentation).
# ---------------------------------------------------------------------------
def test_review_submit_edits_by_category_scripted(client):
    """A scripted edit - one objective rewritten, one distractor's wording
    rewritten on one question, and which option is marked correct moved (no
    text rewritten) on a different question - lands in exactly the
    categories that content belongs to, and edits_count keeps meaning "the
    total"."""
    payload, job_id, token = _upload_and_get_job(client, NUMBERED_SOP_PATH)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    module["learning_objectives"][0] = module["learning_objectives"][0] + " (edited)"

    mc_questions = [q for q in assessment["questions"] if q["type"] != "true_false"]
    assert len(mc_questions) >= 2

    # One option's wording rewritten on the first MC question (not the
    # correct one, so it can't also register as a question_answers change).
    q1 = mc_questions[0]
    wrong_idx = next(i for i in range(len(q1["options"])) if i != q1["correct_answer"])
    q1["options"][wrong_idx] = q1["options"][wrong_idx] + " (reworded)"

    # Which option is correct moved to another already-existing option's
    # text on a *different* question - no option text rewritten at all.
    q2 = mc_questions[1]
    other_idx = next(i for i in range(len(q2["options"])) if i != q2["correct_answer"])
    q2["correct_answer"] = other_idx

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    result = resp.get_json()

    categories = result["edits_by_category"]
    assert categories["objectives"] == 1
    assert categories["question_options"] == 1
    assert categories["question_answers"] == 1
    assert categories["title"] == 0
    assert categories["sections"] == 0
    assert categories["questions"] == 0
    assert result["edits_count"] >= 3  # still the flat total, unaffected in meaning
    assert result["edit_rounds"] == 1

    saved = app_module._read_job_json(job_id)
    assert saved["edits_by_category"] == categories
    assert saved["edit_rounds"] == 1
    assert saved["first_edit_at"] is not None
    assert saved["review_opened_at"] is None  # this test never GETs /review

    first_edit_at = saved["first_edit_at"]

    # A second save increments edit_rounds without disturbing first_edit_at.
    resp2 = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp2.status_code == 200, resp2.get_data(as_text=True)
    assert resp2.get_json()["edit_rounds"] == 2

    saved2 = app_module._read_job_json(job_id)
    assert saved2["edit_rounds"] == 2
    assert saved2["first_edit_at"] == first_edit_at


def test_review_page_records_first_open_only(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    assert job["review_opened_at"] is None

    resp = client.get(payload["review_url"])
    assert resp.status_code == 200
    opened = app_module._read_job_json(job_id)
    assert opened["review_opened_at"] is not None

    first_open = opened["review_opened_at"]
    client.get(payload["review_url"])
    opened_again = app_module._read_job_json(job_id)
    assert opened_again["review_opened_at"] == first_open


def test_approve_sets_job_level_approved_at(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert resp.status_code == 200
    saved = app_module._read_job_json(job_id)
    assert saved["approved_at"] == saved["approval"]["approved_at"]
    assert saved["approved_at"] is not None


def test_review_submit_rejects_answers_gamed_by_length(client, sample_sop_path):
    """Position can't fix a text-level exploit: if the SME makes the correct
    option dramatically longer than its distractors in every multiple-choice
    question, "always click the longest option" would pass the quiz, and the
    edit must be rejected outright rather than silently saved."""
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]

    padding = " extra padding text to make this option far longer than the others" * 3
    for q in assessment["questions"]:
        if q["type"] == "true_false":
            continue
        idx = q["correct_answer"]
        q["options"][idx] = q["options"][idx] + padding

    resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "longest" in error
    assert "%" in error

    # Nothing was saved: the job is still in its pre-edit state.
    saved = app_module._read_job_json(job_id)
    assert saved["status"] == "draft"


def test_approve_after_position_gamed_edit_still_hides_answer_key(client):
    """The naive-learner fix must not reopen Defect 2: even after an SME
    edit that had to be corrected server-side, the approved package's
    learner-facing HTML still carries no answer key."""
    payload, job_id, token = _upload_and_get_job(client, NUMBERED_SOP_PATH)
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    for q in assessment["questions"]:
        if q["type"] != "true_false":
            q["correct_answer"] = 0

    edit_resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert edit_resp.status_code == 200

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane Doe", "role": "SME"},
    )
    assert approve_resp.status_code == 200
    result = approve_resp.get_json()

    download_resp = client.get(result["download_url"])
    assessment_html = _zip_member_text(download_resp.data, "assessment.html")
    assert "correct_answer" not in assessment_html
    assert "DRAFT" not in assessment_html


def test_transparency_report_carries_approval_record(client, sample_sop_path):
    """The auditor-facing report must show the unreviewed-draft state before
    approval and the exact approval record after it (GOAL criterion 7)."""
    import json as _json
    from pathlib import Path as _Path
    from app import app as _app

    _payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    report_path = _Path(_app.config['OUTPUT_FOLDER']) / job_id / 'transparency_report.json'

    before = _json.loads(report_path.read_text(encoding='utf-8'))
    assert before['approval'] == {'status': 'unreviewed_draft'}

    approve = client.post(
        f'/api/approve/{job_id}?t={token}',
        json={'approved_by': 'Dr. Quality', 'role': 'QA Manager', 'notes': 'ok'},
    )
    assert approve.status_code == 200, approve.get_json()

    after = _json.loads(report_path.read_text(encoding='utf-8'))
    assert set(after['approval']) == {'approved_by', 'role', 'approved_at', 'notes', 'edits_count'}
    assert after['approval']['approved_by'] == 'Dr. Quality'
    assert after['approval']['role'] == 'QA Manager'
    html_report = (report_path.parent / 'transparency_report.html').read_text(encoding='utf-8')
    assert 'Dr. Quality' in html_report and 'UNREVIEWED DRAFT' not in html_report
