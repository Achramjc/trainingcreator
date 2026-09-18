"""Tests for src/audit.py - the tamper-evident audit trail (M3).

Every tamper case below mutates a *real* log file on disk and then asks the
module whether it notices. The hashes the helpers compute are written out
here from the specification (sha256 over sorted-key compact JSON of the entry
without `hash`/`mac`), not borrowed from the module's own builder, so a change
to the module's hashing that still "agrees with itself" fails these tests.

The one case that is deliberately *not* detected - removal of whole trailing
entries - is tested too, as a pinned statement of the limit and of the
external anchor (`head_hash`) that closes it.
"""

import hashlib
import hmac
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.audit import (
    ACTOR_SOURCES,
    EVENTS,
    GENESIS,
    GLOBAL_JOB_ID,
    Actor,
    AuditEntry,
    AuditLog,
    VerificationResult,
    canonical_json,
    content_hash,
    file_hash,
    global_log,
    hmac_key_from_env,
    verify_job,
)
from src import audit as audit_module

REPO_ROOT = Path(__file__).resolve().parent.parent

JOB_ID = "job-0123456789ab"
KEY = b"an-audit-hmac-key"
OTHER_KEY = b"a-different-audit-hmac-key"

SME = Actor(name="Dana Reyes", role="Quality Engineer", source="web")
SYSTEM = Actor(name="training-creator", role="application", source="system")

MODULE_A = {"title": "Cleanroom Gowning", "sections": [{"title": "Purpose"}]}
MODULE_B = {"title": "Cleanroom Gowning", "sections": [{"title": "Purpose (edited)"}]}
ASSESSMENT_A = {"questions": [{"id": "q1", "correct_answer": 2}], "passing_score": 80}
ASSESSMENT_B = {"questions": [{"id": "q1", "correct_answer": 0}], "passing_score": 80}

HASH_A = content_hash(MODULE_A, ASSESSMENT_A)
HASH_B = content_hash(MODULE_B, ASSESSMENT_B)


# --- helpers ---------------------------------------------------------------
# These reimplement the on-disk format from the spec rather than calling the
# module, so they can forge a *convincing* log - which is the only way to test
# that verification does more than trust the file.

def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_of(obj):
    payload = {key: obj[key] for key in
               ("seq", "ts", "job_id", "event", "actor", "content_hash",
                "details", "prev_hash")}
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def _log_path(tmp_path, job_id=JOB_ID):
    return tmp_path / job_id / "audit.jsonl"


def _read_objs(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_objs(path, objs):
    """Write lines back verbatim - no re-hashing. A crude tamper."""
    path.write_text("".join(_canon(obj) + "\n" for obj in objs), encoding="utf-8")


def _rechain(path, objs, key=None):
    """Rewrite the whole log as a *fresh, internally consistent* chain.

    This is the forger who understands the format: every hash and MAC is
    recomputed, so nothing in the chain itself is wrong. Only the semantics
    (sequence, order, events, workflow) or a missing key can give it away.
    """
    prev = GENESIS
    lines = []
    for obj in objs:
        entry = dict(obj)
        entry["prev_hash"] = prev
        entry["hash"] = _hash_of(entry)
        entry["mac"] = (hmac.new(key, entry["hash"].encode("utf-8"),
                                 hashlib.sha256).hexdigest() if key else None)
        lines.append(_canon(entry))
        prev = entry["hash"]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _seed(tmp_path, key=None, job_id=JOB_ID, approve=True, export=True,
          export_hash=None):
    """A realistic trail: created -> generated -> reviewed -> edited -> approved -> exported."""
    log = AuditLog.for_job(tmp_path / job_id, job_id, key)
    log.append("job.created", SYSTEM, {"source_filename": "SOP-042.txt"})
    log.append("content.generated", SYSTEM, {"questions": 5}, HASH_A)
    log.append("review.opened", SME, {"token": "signed"})
    log.append("content.edited", SME, {"edits_count": 3}, HASH_B)
    if approve:
        log.append("content.approved", SME,
                   {"approved_by": SME.name, "role": SME.role, "edits_count": 3}, HASH_B)
    if export:
        log.append("package.exported", SYSTEM,
                   {"package_sha256": "ab" * 32, "format": "scorm",
                    "scorm_version": "1.2"},
                   export_hash or HASH_B)
    return log


# --- primitives ------------------------------------------------------------

def test_canonical_json_is_key_order_independent_and_compact():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": "é"}) == '{"a":"é"}'  # no ASCII escaping


def test_content_hash_is_stable_and_sensitive():
    assert content_hash(MODULE_A, ASSESSMENT_A) == HASH_A
    assert len(HASH_A) == 64
    assert HASH_A != HASH_B
    # An answer-key change alone must move the hash: that is what makes
    # "edited after approval" detectable at all.
    assert content_hash(MODULE_A, ASSESSMENT_A) != content_hash(MODULE_A, ASSESSMENT_B)


def test_content_hash_is_independent_of_dict_insertion_order():
    reordered = {"passing_score": 80, "questions": ASSESSMENT_A["questions"]}
    assert content_hash(MODULE_A, reordered) == HASH_A


def test_file_hash_matches_hashlib(tmp_path):
    target = tmp_path / "package.zip"
    target.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
    assert file_hash(target) == hashlib.sha256(target.read_bytes()).hexdigest()


def test_hmac_key_from_env():
    assert hmac_key_from_env({"AUDIT_HMAC_KEY": "secret"}) == b"secret"
    assert hmac_key_from_env({"AUDIT_HMAC_KEY": ""}) is None
    assert hmac_key_from_env({"AUDIT_HMAC_KEY": "   "}) is None
    assert hmac_key_from_env({}) is None


def test_genesis_and_event_vocabulary():
    assert GENESIS == "0" * 64
    assert "content.approved" in EVENTS and "retention.deleted" in EVENTS
    assert len(set(EVENTS)) == len(EVENTS)


# --- input validation ------------------------------------------------------

@pytest.mark.parametrize("name", ["", "   ", None, 7])
def test_actor_rejects_empty_or_non_string_name(name):
    with pytest.raises(ValueError):
        Actor(name=name, role="QA", source="web")


@pytest.mark.parametrize("source", ["", "api", "System", None])
def test_actor_rejects_source_outside_the_allowed_set(source):
    with pytest.raises(ValueError):
        Actor(name="Dana", role="QA", source=source)


def test_actor_accepts_every_allowed_source():
    for source in ACTOR_SOURCES:
        assert Actor("Dana", "QA", source).source == source


def test_append_rejects_unknown_event(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    with pytest.raises(ValueError):
        log.append("content.deleted", SME)
    assert not log.exists()  # nothing written by a rejected call


@pytest.mark.parametrize("details", [
    {"when": object()},           # not serialisable at all
    {"tags": {"a", "b"}},         # a set
    {1: "integer key"},           # json would silently stringify the key
    {"steps": ("a", "b")},        # json would silently turn it into a list
    ["not", "a", "dict"],
])
def test_append_rejects_details_that_are_not_faithful_json(tmp_path, details):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    with pytest.raises(ValueError):
        log.append("job.created", SYSTEM, details)


def test_append_rejects_a_content_hash_that_is_not_a_sha256(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    with pytest.raises(ValueError):
        log.append("content.generated", SYSTEM, {}, "not-a-hash")


def test_append_rejects_an_actor_that_is_not_an_actor(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    with pytest.raises(ValueError):
        log.append("job.created", "Dana Reyes")
    with pytest.raises(ValueError):
        log.append("job.created", {"name": "Dana", "role": "QA", "source": "ftp"})


def test_audit_log_rejects_an_empty_job_id(tmp_path):
    with pytest.raises(ValueError):
        AuditLog(tmp_path / "audit.jsonl", "")


# --- entry shape -----------------------------------------------------------

def test_first_entry_shape(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    entry = log.append("job.created", SYSTEM, {"source_filename": "SOP-042.txt"})
    assert entry.seq == 1
    assert entry.prev_hash == GENESIS
    assert entry.job_id == JOB_ID
    assert entry.actor == {"name": "training-creator", "role": "application",
                           "source": "system"}
    assert entry.mac is None
    assert entry.content_hash is None
    assert entry.ts.endswith("+00:00")
    # microsecond precision: 2026-09-17T12:34:56.123456+00:00
    assert len(entry.ts) == len("2026-09-17T12:34:56.123456+00:00")
    assert entry.hash == _hash_of(entry.to_dict())
    assert log.exists()


def test_entry_to_dict_is_exactly_the_line_on_disk(tmp_path):
    log = _seed(tmp_path)
    written = _read_objs(log.path)
    assert [e.to_dict() for e in log.entries()] == written


def test_chain_links_each_entry_to_the_previous(tmp_path):
    log = _seed(tmp_path)
    entries = log.entries()
    assert [e.seq for e in entries] == [1, 2, 3, 4, 5, 6]
    assert entries[0].prev_hash == GENESIS
    for earlier, later in zip(entries, entries[1:]):
        assert later.prev_hash == earlier.hash
    assert log.head_hash() == entries[-1].hash


def test_timestamps_are_non_decreasing(tmp_path):
    log = _seed(tmp_path)
    stamps = [e.ts for e in log.entries()]
    assert stamps == sorted(stamps)


def test_mac_is_written_only_when_a_key_is_configured(tmp_path):
    keyed = _seed(tmp_path, key=KEY, job_id="keyed")
    unkeyed = _seed(tmp_path, job_id="unkeyed")
    for entry in keyed.entries():
        assert entry.mac == hmac.new(KEY, entry.hash.encode("utf-8"),
                                     hashlib.sha256).hexdigest()
    assert all(entry.mac is None for entry in unkeyed.entries())


def test_head_hash_of_an_untouched_log_is_genesis(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    assert not log.exists()
    assert log.head_hash() == GENESIS
    assert log.entries() == []
    assert log.verify().ok is True
    assert log.verify().entries == 0


def test_for_job_and_global_log_paths(tmp_path):
    assert AuditLog.for_job(tmp_path / JOB_ID, JOB_ID).path == tmp_path / JOB_ID / "audit.jsonl"
    glog = global_log(tmp_path)
    assert glog.path == tmp_path / "audit-global.jsonl"
    assert glog.job_id == GLOBAL_JOB_ID


def test_global_log_records_retention_deletions(tmp_path):
    job = _seed(tmp_path)
    head = job.head_hash()
    glog = global_log(tmp_path)
    glog.append("retention.deleted", SYSTEM,
                {"job_id": JOB_ID, "head_hash": head, "retention_seconds": 86400})
    entry = glog.entries()[-1]
    assert entry.job_id == GLOBAL_JOB_ID
    assert entry.details["head_hash"] == head
    assert glog.verify().ok is True


# --- the positive cases ----------------------------------------------------

def test_a_valid_trail_verifies_without_a_key(tmp_path):
    _seed(tmp_path)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is True
    assert result.entries == 6
    assert result.first_bad_seq is None
    assert result.reason is None
    assert result.problems == []
    assert result.package_matches_approval is True


def test_a_valid_trail_verifies_with_a_key(tmp_path):
    log = _seed(tmp_path, key=KEY)
    assert log.verify().ok is True
    result = verify_job(tmp_path / JOB_ID, JOB_ID, KEY)
    assert result.ok is True
    assert result.head_hash == log.head_hash()


def test_verification_result_round_trips_to_json(tmp_path):
    _seed(tmp_path)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    as_dict = result.to_dict()
    assert json.loads(json.dumps(as_dict)) == as_dict
    assert set(as_dict) == {"ok", "entries", "first_bad_seq", "reason", "problems",
                            "head_hash", "package_matches_approval"}


def test_package_matches_approval_is_none_without_an_approval(tmp_path):
    _seed(tmp_path, approve=False)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is True
    assert result.package_matches_approval is None


def test_package_matches_approval_is_none_when_no_export_followed(tmp_path):
    _seed(tmp_path, export=False)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is True
    assert result.package_matches_approval is None


def test_an_export_before_the_approval_does_not_count_as_a_match(tmp_path):
    """An export that predates the approval says nothing about what shipped."""
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID)
    log.append("job.created", SYSTEM, {})
    log.append("content.generated", SYSTEM, {}, HASH_A)
    log.append("package.exported", SYSTEM, {"package_sha256": "cd" * 32}, HASH_A)
    log.append("content.approved", SME, {"approved_by": SME.name}, HASH_A)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is True
    assert result.package_matches_approval is None


def test_re_approval_after_an_edit_is_clean(tmp_path):
    log = _seed(tmp_path)
    log.append("content.edited", SME, {"edits_count": 4}, HASH_A)
    log.append("content.approved", SME, {"approved_by": SME.name}, HASH_A)
    log.append("package.exported", SYSTEM, {"package_sha256": "ef" * 32}, HASH_A)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is True, result.problems
    assert result.package_matches_approval is True


# --- tamper: the chain -----------------------------------------------------

def test_detects_a_modified_field_in_a_middle_entry(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[3]["details"]["edits_count"] = 0      # hide how much the SME changed
    _write_objs(log.path, objs)
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 4
    assert "do not match its recorded hash" in result.reason


def test_detects_a_modified_actor(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[4]["actor"]["name"] = "Someone Else"   # reassign an approval
    _write_objs(log.path, objs)
    assert log.verify().first_bad_seq == 5


def test_detects_a_modified_content_hash(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[5]["content_hash"] = HASH_A            # claim a different export
    _write_objs(log.path, objs)
    assert log.verify().ok is False


def test_detects_a_deleted_middle_entry(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    removed = objs.pop(3)                       # the inconvenient edit
    _write_objs(log.path, objs)
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == removed["seq"] + 1
    assert "prev_hash" in result.reason
    # and verify_job says so twice: the chain broke and the sequence gapped
    full = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert any("contiguous" in problem for problem in full.problems)


def test_detects_reordered_entries(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[3], objs[4] = objs[4], objs[3]         # approval before the edit
    _write_objs(log.path, objs)
    assert log.verify().ok is False
    assert verify_job(tmp_path / JOB_ID, JOB_ID).ok is False


def test_detects_a_forged_append_with_a_wrong_prev_hash(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    forged = dict(objs[-1])
    forged["seq"] = len(objs) + 1
    forged["event"] = "content.approved"
    forged["actor"] = {"name": "Ghost", "role": "Approver", "source": "web"}
    forged["prev_hash"] = "f" * 64
    forged["hash"] = _hash_of(forged)           # internally consistent, wrongly linked
    forged["mac"] = None
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(_canon(forged) + "\n")
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == forged["seq"]
    assert "prev_hash" in result.reason


def test_detects_an_entry_carrying_an_extra_unhashed_field(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[2]["signed_off_by"] = "Ghost"          # a field no hash covers
    _write_objs(log.path, objs)
    result = log.verify()
    assert result.ok is False
    assert "unknown field" in result.reason


def test_detects_a_corrupted_line(tmp_path):
    log = _seed(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2][:40] + "}"
    log.path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    result = log.verify()
    assert result.ok is False
    assert "not valid JSON" in result.reason
    assert log.entries()  # the intact records are still readable


# --- tamper: MAC -----------------------------------------------------------

def test_detects_a_forged_append_with_a_correct_prev_hash_but_no_mac(tmp_path):
    """The chain is perfect; only the key the forger lacks gives them away."""
    log = _seed(tmp_path, key=KEY)
    objs = _read_objs(log.path)
    forged = {
        "seq": len(objs) + 1,
        "ts": objs[-1]["ts"],
        "job_id": JOB_ID,
        "event": "content.approved",
        "actor": {"name": "Ghost", "role": "Approver", "source": "web"},
        "content_hash": HASH_A,
        "details": {"approved_by": "Ghost"},
        "prev_hash": objs[-1]["hash"],
        "mac": None,
    }
    forged["hash"] = _hash_of(forged)
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(_canon(forged) + "\n")

    assert AuditLog(log.path, JOB_ID, None).verify().ok is True   # keyless: blind
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == forged["seq"]
    assert "MAC" in result.reason


def test_detects_a_forged_append_with_an_invalid_mac(tmp_path):
    log = _seed(tmp_path, key=KEY)
    objs = _read_objs(log.path)
    forged = dict(objs[-1])
    forged["seq"] = len(objs) + 1
    forged["prev_hash"] = objs[-1]["hash"]
    forged["details"] = {"package_sha256": "00" * 32}
    forged["hash"] = _hash_of(forged)
    forged["mac"] = hmac.new(OTHER_KEY, forged["hash"].encode("utf-8"),
                             hashlib.sha256).hexdigest()
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(_canon(forged) + "\n")
    result = log.verify()
    assert result.ok is False
    assert "MAC does not verify" in result.reason


def test_detects_a_log_regenerated_with_a_different_key(tmp_path):
    """A rewrite that is flawless as a chain, but signed with the wrong key."""
    log = _seed(tmp_path, key=KEY)
    objs = _read_objs(log.path)
    objs[4]["actor"]["name"] = "Ghost"
    _rechain(log.path, objs, key=OTHER_KEY)

    assert AuditLog(log.path, JOB_ID, None).verify().ok is True   # keyless: blind
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1
    assert all("MAC does not verify" in problem for problem in result.problems)


def test_detects_a_log_regenerated_with_no_key_at_all(tmp_path):
    log = _seed(tmp_path, key=KEY)
    objs = _read_objs(log.path)
    objs.pop(3)
    for index, obj in enumerate(objs, start=1):
        obj["seq"] = index
    _rechain(log.path, objs, key=None)
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_seq == 1
    assert "no MAC" in result.reason


# --- tamper: truncation ----------------------------------------------------

def test_a_truncated_tail_is_not_detected_by_the_chain_alone(tmp_path):
    """The documented limit. Dropping whole trailing lines leaves a chain that
    verifies - which is exactly why head_hash must be anchored elsewhere."""
    log = _seed(tmp_path, key=KEY)
    anchored_head = log.head_hash()
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("".join(line + "\n" for line in lines[:-2]), encoding="utf-8")

    result = log.verify()
    assert result.ok is True          # the chain cannot see what is gone
    assert result.entries == 4
    # ...but the externally anchored head does:
    assert result.head_hash != anchored_head
    assert log.head_hash() != anchored_head


def test_a_truncation_that_leaves_a_partial_line_is_detected(tmp_path):
    log = _seed(tmp_path, key=KEY)
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:-60])   # cut mid-line
    result = log.verify()
    assert result.ok is False
    assert any("incomplete final line" in problem for problem in result.problems)
    assert log.entries()              # tolerated, not raised


def test_appending_after_a_partial_line_neither_splices_nor_erases(tmp_path):
    log = _seed(tmp_path, key=KEY)
    before = log.path.read_bytes()[:-60]
    log.path.write_bytes(before)
    entry = log.append("download.served", SYSTEM, {"filename": "package.zip"})

    after = log.path.read_bytes()
    assert after.startswith(before)   # nothing already recorded was rewritten
    # The damaged line was entry 6's; the last intact entry is 5, so the new
    # record takes 6 - and the damaged line stays on disk, still reported.
    assert entry.seq == 6
    problems = log.verify().problems
    assert any("not valid JSON" in problem for problem in problems)
    assert log.entries()[-1].seq == 6


# --- tamper: semantics (a flawless chain that still lies) ------------------

def test_detects_a_sequence_gap(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    del objs[3]                        # drop the edit and renumber around it
    for index, obj in enumerate(objs, start=1):
        obj["seq"] = index if index < 4 else index + 1
    _rechain(log.path, objs)
    assert log.verify().ok is True     # chain-perfect
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("contiguous" in problem for problem in result.problems)


def test_detects_a_timestamp_going_backwards(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[4]["ts"] = "2020-01-01T00:00:00.000000+00:00"
    _rechain(log.path, objs)
    assert log.verify().ok is True
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("backwards" in problem for problem in result.problems)


def test_detects_a_non_utc_timestamp(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[2]["ts"] = "2026-09-17T12:00:00.000000"      # no offset at all
    _rechain(log.path, objs)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("ISO-8601" in problem for problem in result.problems)


def test_detects_an_unknown_event(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[3]["event"] = "content.quietly.replaced"
    _rechain(log.path, objs)
    assert log.verify().ok is True
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("unknown event" in problem for problem in result.problems)


def test_detects_a_trail_that_does_not_start_at_job_created(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)[1:]    # the beginning of the story is missing
    for index, obj in enumerate(objs, start=1):
        obj["seq"] = index
    _rechain(log.path, objs)
    assert log.verify().ok is True
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("job.created" in problem for problem in result.problems)


def test_detects_entries_carrying_another_jobs_id(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[2]["job_id"] = "some-other-job"
    _rechain(log.path, objs)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("job_id" in problem for problem in result.problems)


def test_detects_a_content_event_with_no_content_hash(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[4]["content_hash"] = None
    _rechain(log.path, objs)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("carries no content_hash" in problem for problem in result.problems)


def test_detects_content_edited_after_approval(tmp_path):
    log = _seed(tmp_path)
    log.append("content.edited", SME, {"edits_count": 9}, HASH_A)
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert any("edited after approval" in problem for problem in result.problems)
    assert result.first_bad_seq == 7


def test_detects_an_export_that_is_not_the_approved_content(tmp_path):
    _seed(tmp_path, export_hash=HASH_A)   # approved HASH_B, shipped HASH_A
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert result.package_matches_approval is False
    assert any("not the content that was approved" in problem
               for problem in result.problems)


def test_an_empty_or_missing_trail_is_a_problem_for_a_job(tmp_path):
    result = verify_job(tmp_path / JOB_ID, JOB_ID)
    assert result.ok is False
    assert result.entries == 0
    assert result.head_hash == GENESIS
    assert "empty or missing" in result.reason


# --- append never rewrites -------------------------------------------------

def test_every_append_only_ever_grows_the_file(tmp_path):
    log = AuditLog.for_job(tmp_path / JOB_ID, JOB_ID, KEY)
    snapshots = []
    for event, digest in (("job.created", None), ("content.generated", HASH_A),
                          ("review.opened", None), ("content.edited", HASH_B),
                          ("content.approved", HASH_B)):
        log.append(event, SME, {"n": len(snapshots)}, digest)
        snapshots.append(log.path.read_bytes())
    for earlier, later in zip(snapshots, snapshots[1:]):
        assert later.startswith(earlier)
        assert len(later) > len(earlier)


def test_entries_are_written_one_per_line(tmp_path):
    log = _seed(tmp_path)
    text = log.path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 6


# --- concurrency -----------------------------------------------------------

PROCESSES = 8
PER_PROCESS = 25


def _concurrent_appender(path, job_id, key, count):
    """Module level so it survives being sent to another process."""
    log = AuditLog(path, job_id, key)
    for index in range(count):
        log.append("download.served", Actor(f"worker-{os.getpid()}", "load", "system"),
                   {"index": index, "pid": os.getpid()})


def test_concurrent_appends_from_many_processes_produce_one_valid_chain(tmp_path):
    path = tmp_path / JOB_ID / "audit.jsonl"
    path.parent.mkdir(parents=True)
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_concurrent_appender,
                               args=(path, JOB_ID, KEY, PER_PROCESS))
               for _ in range(PROCESSES)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert all(worker.exitcode == 0 for worker in workers)

    log = AuditLog(path, JOB_ID, KEY)
    result = log.verify()
    assert result.ok is True, result.problems
    assert result.entries == PROCESSES * PER_PROCESS
    entries = log.entries()
    assert [entry.seq for entry in entries] == list(
        range(1, PROCESSES * PER_PROCESS + 1))
    assert len({entry.hash for entry in entries}) == len(entries)
    # every process got its 25 lines in
    pids = [entry.details["pid"] for entry in entries]
    assert len(set(pids)) == PROCESSES
    assert all(pids.count(pid) == PER_PROCESS for pid in set(pids))


# --- CLI -------------------------------------------------------------------

def _run_cli(args, env=None):
    environment = dict(os.environ)
    environment.pop("AUDIT_HMAC_KEY", None)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "src.audit", *args],
                          cwd=str(REPO_ROOT), env=environment,
                          capture_output=True, text=True)


def test_cli_verify_exits_zero_on_a_valid_trail(tmp_path):
    _seed(tmp_path)
    done = _run_cli(["verify", str(tmp_path / JOB_ID)])
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["ok"] is True
    assert payload["entries"] == 6
    assert payload["package_matches_approval"] is True


def test_cli_verify_defaults_the_job_id_to_the_directory_name(tmp_path):
    _seed(tmp_path, job_id="job-xyz")
    assert _run_cli(["verify", str(tmp_path / "job-xyz")]).returncode == 0
    mismatched = _run_cli(["verify", str(tmp_path / "job-xyz"), "--job-id", "other"])
    assert mismatched.returncode == 1
    assert "job_id" in json.loads(mismatched.stdout)["reason"]


def test_cli_verify_exits_one_on_a_tampered_trail(tmp_path):
    log = _seed(tmp_path)
    objs = _read_objs(log.path)
    objs[1]["details"]["questions"] = 99
    _write_objs(log.path, objs)
    done = _run_cli(["verify", str(tmp_path / JOB_ID)])
    assert done.returncode == 1
    payload = json.loads(done.stdout)
    assert payload["ok"] is False
    assert payload["first_bad_seq"] == 2


def test_cli_verify_uses_the_key_from_the_environment(tmp_path):
    log = _seed(tmp_path, key=KEY)
    objs = _read_objs(log.path)
    _rechain(log.path, objs, key=OTHER_KEY)
    assert _run_cli(["verify", str(tmp_path / JOB_ID)]).returncode == 0
    keyed = _run_cli(["verify", str(tmp_path / JOB_ID)],
                     env={"AUDIT_HMAC_KEY": KEY.decode()})
    assert keyed.returncode == 1
    assert "MAC" in json.loads(keyed.stdout)["reason"]
    named = _run_cli(["verify", str(tmp_path / JOB_ID), "--key-env", "MY_KEY"],
                     env={"MY_KEY": KEY.decode()})
    assert named.returncode == 1


def test_cli_show_prints_one_line_per_entry(tmp_path):
    log = _seed(tmp_path)
    done = _run_cli(["show", str(tmp_path / JOB_ID)])
    assert done.returncode == 0, done.stderr
    lines = done.stdout.strip().splitlines()
    assert len(lines) == 6
    assert "job.created" in lines[0]
    assert "Dana Reyes" in lines[4] and "content.approved" in lines[4]
    assert HASH_B[:12] in lines[4]
    assert "edits_count=3" in lines[3]
    assert log.head_hash() in done.stderr


def test_cli_show_reports_damage_without_hiding_intact_entries(tmp_path):
    log = _seed(tmp_path)
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:-60])
    done = _run_cli(["show", str(tmp_path / JOB_ID)])
    assert done.returncode == 0
    assert len(done.stdout.strip().splitlines()) == 5
    assert "incomplete final line" in done.stderr


# --- dataclass contracts ---------------------------------------------------

def test_entry_and_result_are_frozen(tmp_path):
    log = _seed(tmp_path)
    entry = log.entries()[0]
    with pytest.raises(Exception):
        entry.seq = 99
    result = log.verify()
    with pytest.raises(Exception):
        result.ok = False
    assert isinstance(entry, AuditEntry)
    assert isinstance(result, VerificationResult)


def test_module_uses_only_the_standard_library():
    source = (REPO_ROOT / "src" / "audit.py").read_text(encoding="utf-8")
    imported = set()
    for line in source.splitlines():
        line = line.strip()
        if line.startswith("import "):
            imported.add(line.split()[1].split(".")[0])
        elif line.startswith("from ") and " import " in line:
            imported.add(line.split()[1].split(".")[0])
    assert imported <= {
        "argparse", "fcntl", "hashlib", "hmac", "json", "os", "sys",
        "dataclasses", "datetime", "pathlib", "typing", "__future__",
    }, imported
    assert audit_module.__name__ == "src.audit"
