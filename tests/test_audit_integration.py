"""End-to-end audit trail integration tests (M3 partial: app/CLI wiring).

Covers: the full upload -> review -> edit -> approve -> download lifecycle
through the Flask test client, `GET /api/audit`, HMAC mode, editing after
approval, the retention sweep's global log, the sample-gallery route, the
approve endpoint's audit-write atomicity, and the CLI's own `audit.jsonl`.

`src/audit.py` itself (the hash chain, MACs, semantic checks) is covered by
tests/test_audit.py; this file only checks that app.py/src/cli.py/
src/samples_routes.py/src/scorm_exporter.py call it the way
docs/AUDIT_TRAIL.md and CLAUDE.md require.
"""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import app as app_module
from src.audit import AuditLog, content_hash, file_hash, global_log, hmac_key_from_env, verify_job
from src.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _upload_and_get_job(client, sample_sop_path, **form_overrides):
    resp = _upload(client, sample_sop_path, **form_overrides)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    return payload, payload["job_id"], payload["review_url"].split("t=")[1]


def _zip_member_bytes(zip_bytes, suffix):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    name = next(n for n in z.namelist() if n.endswith(suffix))
    return z.read(name)


def _events(job_id):
    return [e.event for e in app_module._audit_log(job_id).entries()]


def _actors(job_id):
    return [(e.event, e.actor) for e in app_module._audit_log(job_id).entries()]


# ---------------------------------------------------------------------------
# Full lifecycle: upload -> review -> edit -> approve -> download
# ---------------------------------------------------------------------------
def test_full_lifecycle_produces_a_verifying_trail_in_the_expected_order(
        app, client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    # Upload alone already produced job.created, content.generated,
    # package.exported.
    assert _events(job_id) == ["job.created", "content.generated", "package.exported"]

    # GET /review -> review.opened
    review_resp = client.get(payload["review_url"])
    assert review_resp.status_code == 200
    assert _events(job_id)[-1] == "review.opened"

    # A second GET must not add a second review.opened.
    client.get(payload["review_url"])
    assert _events(job_id).count("review.opened") == 1

    # Edit -> content.edited, package.exported
    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    new_text = "A freshly written distractor for the audit trail test"
    assessment["questions"][0]["options"][0] = new_text
    edit_resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert edit_resp.status_code == 200, edit_resp.get_data(as_text=True)
    assert _events(job_id)[-2:] == ["content.edited", "package.exported"]

    # Approve -> content.approved, package.exported
    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Dana Reyes", "role": "Quality Engineer"},
    )
    assert approve_resp.status_code == 200, approve_resp.get_data(as_text=True)
    result = approve_resp.get_json()
    assert _events(job_id)[-2:] == ["content.approved", "package.exported"]

    expected_order = [
        "job.created", "content.generated", "package.exported",
        "review.opened",
        "content.edited", "package.exported",
        "content.approved", "package.exported",
    ]
    assert _events(job_id) == expected_order

    # Actors: anonymous/web before approval, the named approver at approval,
    # the system actor for every package.exported.
    actors = dict()
    for event, actor in _actors(job_id):
        actors.setdefault(event, []).append(actor)
    assert actors["job.created"][0] == {"name": "anonymous", "role": "author", "source": "web"}
    assert actors["review.opened"][0] == {"name": "anonymous", "role": "author", "source": "web"}
    assert actors["content.edited"][0] == {"name": "anonymous", "role": "author", "source": "web"}
    assert actors["content.approved"][0] == {
        "name": "Dana Reyes", "role": "Quality Engineer", "source": "web"}
    for actor in actors["package.exported"]:
        assert actor == {"name": "training-creator", "role": "application", "source": "system"}

    # job.created carries the uploaded file's real sha256.
    with open(sample_sop_path, "rb") as f:
        expected_source_sha256 = hashlib.sha256(f.read()).hexdigest()
    entries = app_module._audit_log(job_id).entries()
    created = entries[0]
    assert created.content_hash is None
    assert created.details["source_sha256"] == expected_source_sha256

    # Every content event carries a content_hash.
    for entry in entries:
        if entry.event in ("content.generated", "content.edited",
                            "content.approved", "package.exported"):
            assert entry.content_hash and len(entry.content_hash) == 64

    # content.approved's hash equals content_hash of job.json's saved content
    # at approval time (unchanged by approval itself).
    saved = app_module._read_job_json(job_id)
    approved_entry = next(e for e in entries if e.event == "content.approved")
    assert approved_entry.content_hash == content_hash(
        saved["training_module"], saved["assessment"])

    # The exported zip's metadata.json carries the head hash as of approval.
    download_resp = client.get(result["download_url"])
    assert download_resp.status_code == 200
    metadata = json.loads(_zip_member_bytes(download_resp.data, "metadata.json"))
    assert metadata["audit_head_hash"] == approved_entry.hash

    # download.served was recorded for that download too.
    assert _events(job_id)[-1] == "download.served"

    # Full verification: ok, and package_matches_approval is True (an export
    # after the approval carries the approved content_hash).
    verification = verify_job(app_module._job_dir(job_id), job_id,
                               app.config.get("AUDIT_HMAC_KEY"))
    assert verification.ok is True, verification.problems
    assert verification.package_matches_approval is True

    # GET /api/audit mirrors this.
    audit_resp = client.get(f"/api/audit/{job_id}?t={token}")
    assert audit_resp.status_code == 200
    audit_payload = audit_resp.get_json()
    assert [e["event"] for e in audit_payload["entries"]] == _events(job_id)
    assert audit_payload["verification"]["ok"] is True
    assert audit_payload["verification"]["package_matches_approval"] is True
    assert audit_payload["hmac_mode"] == "chain-only"


# ---------------------------------------------------------------------------
# F3: the on-disk transparency report reflects the export that produced it
# ---------------------------------------------------------------------------
def test_transparency_report_reflects_the_export_that_produced_it(
        app, client, sample_sop_path):
    """The transparency report written by an export must be built AFTER that
    same export's own `package.exported` entry (app.py `_export_outputs`),
    not one event behind it - and must not be silently rewritten by a later,
    unrelated event such as `download.served`."""
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    assessment["questions"][0]["options"][0] = "A freshly written distractor"
    edit_resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert edit_resp.status_code == 200, edit_resp.get_data(as_text=True)

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Dana Reyes", "role": "Quality Engineer"},
    )
    assert approve_resp.status_code == 200, approve_resp.get_data(as_text=True)

    # Snapshot the trail as it stands right after approve+export, BEFORE the
    # download below appends `download.served` - the report on disk must
    # match this moment, not any later one.
    job_dir = app_module._job_dir(job_id)
    verification_before_download = verify_job(
        job_dir, job_id, app.config.get("AUDIT_HMAC_KEY"))
    assert _events(job_id)[-2:] == ["content.approved", "package.exported"]

    report = json.loads((job_dir / "transparency_report.json").read_text(encoding="utf-8"))
    audit_trail = report["audit_trail"]

    assert audit_trail["status"] == "intact"
    assert audit_trail["package_matches_approval"] is True
    assert audit_trail["head_hash"] == verification_before_download.head_hash

    timeline_events = [item["event"] for item in audit_trail["timeline"]]
    approved_index = timeline_events.index("content.approved")
    assert "package.exported" in timeline_events[approved_index + 1:]

    html_text = (job_dir / "transparency_report.html").read_text(encoding="utf-8")
    assert "intact" in html_text
    assert "AUDIT TRAIL VERIFICATION FAILED" not in html_text

    # Downloading afterwards records `download.served` on the trail but must
    # not rewrite the report already on disk (the report is "as of export",
    # not "as of now").
    download_resp = client.get(approve_resp.get_json()["download_url"])
    assert download_resp.status_code == 200
    assert _events(job_id)[-1] == "download.served"
    report_after_download = json.loads(
        (job_dir / "transparency_report.json").read_text(encoding="utf-8"))
    assert report_after_download == report


# ---------------------------------------------------------------------------
# F2: approve is atomic across the export - a failed export must not leave an
# approved-looking job on disk
# ---------------------------------------------------------------------------
def test_approve_export_failure_leaves_job_unapproved_and_retry_succeeds(
        client, sample_sop_path, monkeypatch):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    from src.scorm_exporter import SCORMExporter

    def boom(self, *args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SCORMExporter, "create_package", boom)

    resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert resp.status_code == 500

    # Nothing beyond the (already-permanent) content.approved entry was
    # written: no approval.json, job.json still not 'approved', and no
    # package.exported after that entry.
    assert not app_module._approval_json_path(job_id).exists()
    job = app_module._read_job_json(job_id)
    assert job["status"] != "approved"
    assert job["approval"] is None

    events = _events(job_id)
    assert events.count("content.approved") == 1
    approved_index = events.index("content.approved")
    assert "package.exported" not in events[approved_index + 1:]

    # Restore the real exporter and retry: since job.json's status is still
    # not 'approved', the endpoint allows a second attempt.
    monkeypatch.undo()

    retry_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert retry_resp.status_code == 200, retry_resp.get_data(as_text=True)

    verification = verify_job(app_module._job_dir(job_id), job_id,
                               app_module.app.config.get("AUDIT_HMAC_KEY"))
    assert verification.ok is True, verification.problems
    assert verification.package_matches_approval is True


# ---------------------------------------------------------------------------
# F5: the audit head hash is anchored into JSON/HTML exports too, only once
# a job is approved
# ---------------------------------------------------------------------------
def test_json_export_carries_audit_head_hash_only_after_approval(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path, output_format="json")
    job_dir = app_module._job_dir(job_id)

    draft_json = json.loads((job_dir / "training_data.json").read_text(encoding="utf-8"))
    assert "audit_head_hash" not in draft_json
    assert "approval" not in draft_json

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert approve_resp.status_code == 200, approve_resp.get_data(as_text=True)

    approved_json = json.loads((job_dir / "training_data.json").read_text(encoding="utf-8"))
    approved_entry = next(e for e in app_module._audit_log(job_id).entries()
                           if e.event == "content.approved")
    assert approved_json["audit_head_hash"] == approved_entry.hash
    assert approved_json["approval"]["approved_by"] == "Jane"


def test_html_export_carries_audit_head_hash_meta_only_after_approval(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path, output_format="html")
    job_dir = app_module._job_dir(job_id)

    draft_html = (job_dir / "training.html").read_text(encoding="utf-8")
    assert 'name="audit-head-hash"' not in draft_html

    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert approve_resp.status_code == 200, approve_resp.get_data(as_text=True)

    approved_html = (job_dir / "training.html").read_text(encoding="utf-8")
    approved_entry = next(e for e in app_module._audit_log(job_id).entries()
                           if e.event == "content.approved")
    assert f'name="audit-head-hash" content="{approved_entry.hash}"' in approved_html


def test_api_audit_requires_token_and_404s_for_unknown_job(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    no_token = client.get(f"/api/audit/{job_id}")
    assert no_token.status_code == 403

    unknown = client.get(
        f"/api/audit/does-not-exist?t={app_module.generate_download_token('does-not-exist')}")
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# HMAC mode
# ---------------------------------------------------------------------------
def test_hmac_key_set_produces_macs_and_a_different_key_fails_verification(
        app, client, sample_sop_path):
    app.config["AUDIT_HMAC_KEY"] = b"pilot-site-key-one"
    try:
        payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
        entries = AuditLog.for_job(
            app_module._job_dir(job_id), job_id, b"pilot-site-key-one").entries()
        assert entries and all(e.mac for e in entries)

        ok_with_right_key = verify_job(app_module._job_dir(job_id), job_id, b"pilot-site-key-one")
        assert ok_with_right_key.ok is True

        wrong_key_result = verify_job(app_module._job_dir(job_id), job_id, b"a-different-key")
        assert wrong_key_result.ok is False

        audit_resp = client.get(f"/api/audit/{job_id}?t={token}")
        assert audit_resp.get_json()["hmac_mode"] == "hmac"
    finally:
        app.config["AUDIT_HMAC_KEY"] = None


# ---------------------------------------------------------------------------
# Edited after approval
# ---------------------------------------------------------------------------
def test_edit_after_approval_fails_verification_until_reapproved(client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    approve_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert approve_resp.status_code == 200

    before = verify_job(app_module._job_dir(job_id), job_id,
                         app_module.app.config.get("AUDIT_HMAC_KEY"))
    assert before.ok is True

    job = app_module._read_job_json(job_id)
    module = job["training_module"]
    assessment = job["assessment"]
    assessment["questions"][0]["options"][0] = "Edited again after approval"
    edit_resp = client.post(
        f"/api/review/{job_id}?t={token}",
        json={"module": module, "assessment": assessment},
    )
    assert edit_resp.status_code == 200, edit_resp.get_data(as_text=True)

    after_edit = verify_job(app_module._job_dir(job_id), job_id,
                             app_module.app.config.get("AUDIT_HMAC_KEY"))
    assert after_edit.ok is False
    assert any("edited after approval" in p for p in after_edit.problems)

    reapprove_resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert reapprove_resp.status_code == 200, reapprove_resp.get_data(as_text=True)

    after_reapproval = verify_job(app_module._job_dir(job_id), job_id,
                                   app_module.app.config.get("AUDIT_HMAC_KEY"))
    assert after_reapproval.ok is True, after_reapproval.problems


# ---------------------------------------------------------------------------
# Retention sweep -> global log
# ---------------------------------------------------------------------------
def test_retention_sweep_records_head_hash_on_the_global_log(app, client, sample_sop_path):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)
    job_dir = app_module._job_dir(job_id)
    expected_head = app_module._audit_log(job_id).head_hash()

    # Force this job's directory to look old enough to sweep.
    import os
    import time as time_module
    old = time_module.time() - 1_000_000
    os.utime(job_dir, (old, old))

    app_module._sweep_expired_outputs(app.config["OUTPUT_FOLDER"], 1)

    assert not job_dir.exists()

    glog = global_log(app.config["OUTPUT_FOLDER"], app.config.get("AUDIT_HMAC_KEY"))
    entries = glog.entries()
    deletion = next(e for e in entries if e.event == "retention.deleted"
                     and e.details.get("job_id") == job_id)
    assert deletion.details["head_hash"] == expected_head
    assert deletion.actor == {"name": "retention-sweep", "role": "system", "source": "system"}

    verification = glog.verify()
    assert verification.ok is True, verification.problems


# ---------------------------------------------------------------------------
# Approve endpoint: audit-write atomicity
# ---------------------------------------------------------------------------
def test_approve_trail_write_failure_leaves_no_approval_on_disk(
        client, sample_sop_path, monkeypatch):
    payload, job_id, token = _upload_and_get_job(client, sample_sop_path)

    def boom(self, event, actor, details=None, content_hash=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(AuditLog, "append", boom)

    resp = client.post(
        f"/api/approve/{job_id}?t={token}",
        json={"approved_by": "Jane", "role": "SME"},
    )
    assert resp.status_code == 500

    approval_path = app_module._approval_json_path(job_id)
    assert not approval_path.exists()

    job = app_module._read_job_json(job_id)
    assert job["status"] != "approved"
    assert job["approval"] is None

    # The trail itself must not carry a content.approved entry either.
    assert "content.approved" not in _events(job_id)


# ---------------------------------------------------------------------------
# Sample gallery
# ---------------------------------------------------------------------------
def test_sample_gallery_generation_records_sample_id(app, client):
    if "samples" not in app.blueprints:
        from src.samples_routes import samples_bp
        app.register_blueprint(samples_bp)

    from src.samples import list_samples
    sample_id = list_samples()[0]["id"]

    resp = client.post(
        f"/api/samples/{sample_id}/generate",
        data={
            "num_questions": "5",
            "passing_score": "80",
            "scorm_version": "1.2",
            "output_format": "scorm",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job_id = resp.get_json()["job_id"]

    created = app_module._audit_log(job_id).entries()[0]
    assert created.event == "job.created"
    assert created.details.get("sample_id") == sample_id
    assert created.actor == {"name": "anonymous", "role": "author", "source": "web"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_output_directory_has_a_verifying_audit_trail(tmp_path, sample_sop_path):
    runner = CliRunner()
    output_dir = tmp_path / "cli_audit_out"

    result = runner.invoke(cli_main, [
        "--input", sample_sop_path,
        "--output", str(output_dir),
        "--format", "scorm1.2",
        "--questions", "5",
    ])
    assert result.exit_code == 0, result.output

    audit_path = output_dir / "audit.jsonl"
    assert audit_path.is_file()
    assert "Audit head hash:" in result.output

    job_id = output_dir.resolve().name
    verification = verify_job(output_dir, job_id, hmac_key_from_env())
    assert verification.ok is True, verification.problems

    log = AuditLog.for_job(output_dir, job_id, hmac_key_from_env())
    events = [e.event for e in log.entries()]
    assert events == ["job.created", "content.generated", "package.exported"]

    printed_head = result.output.split("Audit head hash:")[1].strip().splitlines()[0]
    assert printed_head == log.head_hash()

    with open(sample_sop_path, "rb") as f:
        expected_source_sha256 = hashlib.sha256(f.read()).hexdigest()
    assert log.entries()[0].details["source_sha256"] == expected_source_sha256
