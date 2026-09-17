"""Tests for pilot instrumentation: src/pilot_metrics.collect() aggregation
and the /pilot and /pilot/metrics Flask routes.

See tests/test_review.py for the per-job accounting this builds on
(edits_by_category, edit_rounds, review_opened_at/first_edit_at/approved_at).
"""

import json
from pathlib import Path

import app as app_module
from src.audit import Actor, AuditLog
from src.pilot_metrics import EDIT_CATEGORY_KEYS, collect, summary_table

# A minimal but structurally real training_module / assessment pair, sized
# so editable_items is easy to hand-verify:
#   objectives: 2, sections: 1, questions: 2 * (1 text + 3 options) = 8
#   -> editable_items = 2 + 1 + 8 = 11
_MODULE = {
    "title": "Sample Module",
    "learning_objectives": ["Do the thing", "Do the other thing"],
    "objectives": [],
    "sections": [{"id": "s1", "title": "Section 1", "content": "<p>content</p>"}],
    "estimated_duration": 10,
    "prerequisites": [],
    "summary": "",
}
_ASSESSMENT = {
    "title": "Quiz",
    "description": "",
    "questions": [
        {
            "id": "q1", "type": "multiple_choice", "text": "Question one?",
            "options": ["A", "B", "C"], "correct_answer": 0, "explanation": "",
            "points": 1, "salt": "s1", "answer_hash": "h1", "source_ref": None, "topic": None,
        },
        {
            "id": "q2", "type": "multiple_choice", "text": "Question two?",
            "options": ["D", "E", "F"], "correct_answer": 1, "explanation": "",
            "points": 1, "salt": "s2", "answer_hash": "h2", "source_ref": None, "topic": None,
        },
    ],
    "passing_score": 80,
    "time_limit": None,
    "randomize_questions": False,
    "randomize_options": False,
    "requested_questions": 2,
    "notes": [],
}


def _zero_categories():
    return {key: 0 for key in EDIT_CATEGORY_KEYS}


def _write_job(output_folder, job_id, **overrides):
    job_dir = Path(output_folder) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "job_id": job_id,
        "status": "draft",
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "source_filename": "sop.txt",
        "package_name": "sample",
        "request": {"num_questions": 2, "passing_score": 80,
                    "scorm_version": "1.2", "output_format": "scorm"},
        "sop_content": {},
        "training_module": _MODULE,
        "assessment": _ASSESSMENT,
        "edits_count": 0,
        "edits_by_category": _zero_categories(),
        "edit_rounds": 0,
        "review_opened_at": None,
        "first_edit_at": None,
        "approved_at": None,
        "approval": None,
        "llm_enhancement": {"enabled": False, "model": None, "accepted": 0, "rejected": 0},
    }
    record.update(overrides)
    (job_dir / "job.json").write_text(json.dumps(record), encoding="utf-8")
    return job_dir


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------
def test_collect_on_empty_folder(tmp_path):
    metrics = collect(tmp_path / "does-not-exist")
    assert metrics["total_jobs"] == 0
    assert metrics["unreadable_jobs"] == 0
    assert metrics["jobs_by_status"] == {}
    assert metrics["approval_rate"] is None
    assert metrics["edit_rate"] is None
    assert metrics["jobs"] == []


def test_collect_aggregates_three_jobs_and_a_corrupt_one(tmp_path):
    # Job A: untouched draft, never edited or approved.
    _write_job(tmp_path, "job-a", status="draft")

    # Job B: edited then approved. edits_count = 3, spread over 3 categories.
    categories_b = _zero_categories()
    categories_b["objectives"] = 1
    categories_b["question_options"] = 1
    categories_b["question_answers"] = 1
    _write_job(
        tmp_path, "job-b",
        status="approved",
        edits_count=3,
        edits_by_category=categories_b,
        edit_rounds=1,
        review_opened_at="2026-09-01T00:05:00+00:00",
        first_edit_at="2026-09-01T00:10:00+00:00",
        approved_at="2026-09-01T01:00:00+00:00",
        approval={"approved_by": "SME One", "role": "QA", "approved_at": "2026-09-01T01:00:00+00:00",
                  "notes": "", "edits_count": 3},
    )

    # Job C: approved with no edits at all (the pilot's headline good case).
    _write_job(
        tmp_path, "job-c",
        status="approved",
        edits_count=0,
        created_at="2026-09-02T00:00:00+00:00",
        approved_at="2026-09-02T00:30:00+00:00",
        approval={"approved_by": "SME Two", "role": "QA", "approved_at": "2026-09-02T00:30:00+00:00",
                  "notes": "", "edits_count": 0},
    )

    # A corrupt job.json: present, but not valid JSON. Must be tolerated and
    # counted as unreadable, never raised.
    corrupt_dir = tmp_path / "job-corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "job.json").write_text("{not valid json", encoding="utf-8")

    # A directory with no job.json at all (e.g. mid-generation) - skipped,
    # not counted as unreadable.
    (tmp_path / "job-in-progress").mkdir(parents=True)

    metrics = collect(tmp_path)

    assert metrics["total_jobs"] == 3
    assert metrics["unreadable_jobs"] == 1
    assert metrics["jobs_by_status"] == {"draft": 1, "approved": 2}
    assert metrics["approval_rate"] == 2 / 3

    assert metrics["mean_edits_per_approved_job"] == 1.5
    assert metrics["median_edits_per_approved_job"] == 1.5

    # editable_items for both approved jobs is 11 (see _MODULE/_ASSESSMENT
    # docstring above): edit_rate = (3 + 0) / (11 + 11).
    assert abs(metrics["edit_rate"] - (3 / 22)) < 1e-9
    # Equal weights here, so the unweighted mean matches the weighted rate.
    assert abs(metrics["mean_edit_rate"] - (3 / 22)) < 1e-9

    expected_categories = _zero_categories()
    expected_categories.update(categories_b)
    assert metrics["edits_by_category"] == expected_categories

    # time_to_approve: job-b is 3600s, job-c is 1800s -> median 2700.
    assert metrics["median_time_to_approve_s"] == 2700.0

    assert set(metrics["by_source_document"].keys()) == {"sop.txt"}
    bucket = metrics["by_source_document"]["sop.txt"]
    assert bucket["jobs"] == 3
    assert bucket["approved"] == 2
    assert bucket["approval_rate"] == 2 / 3

    job_ids = {job["job_id"] for job in metrics["jobs"]}
    assert job_ids == {"job-a", "job-b", "job-c"}

    # collect() must never raise, no matter what garbage is on disk.
    summary_table(metrics)  # renders without error


def test_collect_tolerates_missing_and_partial_fields(tmp_path):
    """A job.json missing keys this instrumentation didn't write (an older
    job, or a hand-edited one) must be summarised, not dropped as
    unreadable."""
    job_dir = tmp_path / "job-old"
    job_dir.mkdir(parents=True)
    minimal = {"job_id": "job-old", "status": "approved"}
    (job_dir / "job.json").write_text(json.dumps(minimal), encoding="utf-8")

    metrics = collect(tmp_path)
    assert metrics["total_jobs"] == 1
    assert metrics["unreadable_jobs"] == 0
    job = metrics["jobs"][0]
    assert job["edits_count"] == 0
    assert job["edits_by_category"] == _zero_categories()
    assert job["editable_items"] == 0
    assert job["edit_rate"] is None
    assert job["time_to_approve_s"] is None


def test_collect_job_json_not_a_dict_is_unreadable(tmp_path):
    job_dir = tmp_path / "job-list"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    metrics = collect(tmp_path)
    assert metrics["total_jobs"] == 0
    assert metrics["unreadable_jobs"] == 1


# ---------------------------------------------------------------------------
# GET /pilot/metrics and GET /pilot
# ---------------------------------------------------------------------------
def test_pilot_routes_404_when_disabled(client, monkeypatch):
    monkeypatch.delenv("PILOT_METRICS_TOKEN", raising=False)
    assert client.get("/pilot/metrics").status_code == 404
    assert client.get("/pilot").status_code == 404


def test_pilot_metrics_requires_correct_token(client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "s3cr3t-pilot-token")

    assert client.get("/pilot/metrics").status_code == 403
    assert client.get("/pilot/metrics?token=wrong").status_code == 403
    assert client.get("/pilot").status_code == 403

    ok = client.get("/pilot/metrics?token=s3cr3t-pilot-token")
    assert ok.status_code == 200
    data = ok.get_json()
    assert set(data.keys()) >= {
        "total_jobs", "unreadable_jobs", "jobs_by_status", "approval_rate",
        "mean_edits_per_approved_job", "median_edits_per_approved_job",
        "edit_rate", "mean_edit_rate", "edits_by_category",
        "median_time_to_approve_s", "by_source_document", "jobs",
    }

    ok_page = client.get("/pilot?token=s3cr3t-pilot-token")
    assert ok_page.status_code == 200
    assert b"Pilot metrics" in ok_page.data


def test_pilot_metrics_accepts_bearer_token(client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "bearer-token-value")
    resp = client.get(
        "/pilot/metrics", headers={"Authorization": "Bearer bearer-token-value"})
    assert resp.status_code == 200


def test_pilot_dashboard_escapes_source_filename(app, client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "s3cr3t-pilot-token")
    malicious_name = "<script>alert(1)</script>.txt"
    _write_job(app.config["OUTPUT_FOLDER"], "job-xss", source_filename=malicious_name)

    resp = client.get("/pilot?token=s3cr3t-pilot-token")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_pilot_dashboard_links_to_review_page(app, client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "s3cr3t-pilot-token")
    _write_job(app.config["OUTPUT_FOLDER"], "job-link", status="draft")

    resp = client.get("/pilot?token=s3cr3t-pilot-token")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "/review/job-link?t=" in text

    # The minted review link actually works.
    start = text.index("/review/job-link?t=")
    end = text.index('"', start)
    review_url = text[start:end]
    review_resp = client.get(review_url)
    assert review_resp.status_code == 200


# ---------------------------------------------------------------------------
# Audit trail surfaced in collect() / summary_table() / the /pilot dashboard
# ---------------------------------------------------------------------------

def _write_audit_trail(job_dir, job_id, tamper=False):
    """Write a minimal, real audit.jsonl for `job_id` in `job_dir`. With
    `tamper=True`, corrupt the one recorded entry's contents afterwards so
    verification fails."""
    log = AuditLog.for_job(job_dir, job_id)
    log.append("job.created", Actor("training-creator", "application", "system"),
               {"source_filename": "sop.txt"})
    if tamper:
        path = job_dir / "audit.jsonl"
        obj = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        obj["details"] = {"source_filename": "tampered.txt"}
        path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    return log


def test_collect_sets_audit_verified_for_good_tampered_and_missing_trails(tmp_path):
    good_dir = _write_job(tmp_path, "job-good", status="approved")
    _write_audit_trail(good_dir, "job-good")

    bad_dir = _write_job(tmp_path, "job-bad", status="approved")
    _write_audit_trail(bad_dir, "job-bad", tamper=True)

    # No audit.jsonl at all.
    _write_job(tmp_path, "job-none", status="approved")

    metrics = collect(tmp_path)
    by_id = {job["job_id"]: job for job in metrics["jobs"]}

    assert by_id["job-good"]["audit_verified"] is True
    assert by_id["job-good"]["audit_problems"] == []

    assert by_id["job-bad"]["audit_verified"] is False
    assert by_id["job-bad"]["audit_problems"]

    assert by_id["job-none"]["audit_verified"] is None
    assert by_id["job-none"]["audit_problems"] == []


def test_collect_counts_jobs_with_failed_audit(tmp_path):
    good_dir = _write_job(tmp_path, "job-good", status="approved")
    _write_audit_trail(good_dir, "job-good")

    bad_dir_1 = _write_job(tmp_path, "job-bad-1", status="approved")
    _write_audit_trail(bad_dir_1, "job-bad-1", tamper=True)

    bad_dir_2 = _write_job(tmp_path, "job-bad-2", status="draft")
    _write_audit_trail(bad_dir_2, "job-bad-2", tamper=True)

    metrics = collect(tmp_path)
    assert metrics["jobs_with_failed_audit"] == 2
    failed_ids = {failed["job_id"] for failed in metrics["failed_audit_jobs"]}
    assert failed_ids == {"job-bad-1", "job-bad-2"}
    for failed in metrics["failed_audit_jobs"]:
        assert failed["problems"]

    # Never raises, and a bad trail doesn't stop the job from being counted.
    summary_table(metrics)


def test_collect_never_raises_on_a_bad_trail(tmp_path):
    job_dir = _write_job(tmp_path, "job-garbage", status="approved")
    (job_dir / "audit.jsonl").write_text("not even json\n", encoding="utf-8")

    metrics = collect(tmp_path)
    job = metrics["jobs"][0]
    assert job["audit_verified"] is False
    assert metrics["jobs_with_failed_audit"] == 1


def test_pilot_dashboard_lists_failed_audit_job(app, client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "s3cr3t-pilot-token")
    bad_dir = _write_job(app.config["OUTPUT_FOLDER"], "job-bad-dashboard", status="approved")
    _write_audit_trail(bad_dir, "job-bad-dashboard", tamper=True)

    resp = client.get("/pilot?token=s3cr3t-pilot-token")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "job-bad-dashboard" in text
    assert "failed audit-trail verification" in text.lower()


def test_pilot_dashboard_shows_no_audit_alert_when_all_verified(app, client, monkeypatch):
    monkeypatch.setenv("PILOT_METRICS_TOKEN", "s3cr3t-pilot-token")
    good_dir = _write_job(app.config["OUTPUT_FOLDER"], "job-clean", status="approved")
    _write_audit_trail(good_dir, "job-clean")

    resp = client.get("/pilot?token=s3cr3t-pilot-token")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "failed audit-trail verification" not in text.lower()
