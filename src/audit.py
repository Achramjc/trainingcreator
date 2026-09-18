"""Tamper-evident audit trail for a training job (M3, GOAL.md criterion 7).

An auditor asking "who did what, when, to which exact content; did anything
change after approval; is the package that shipped the content that was
approved; and has this record itself been altered?" must be able to answer
from one file: ``<job_dir>/audit.jsonl``.

The file is an append-only JSON Lines log.  Each line is one entry, and each
entry carries the SHA-256 of the entry before it, so the entries form a hash
chain: altering, deleting, reordering or inserting an entry breaks the chain
at (or immediately after) the point of the change.  When an HMAC key is
configured, every entry additionally carries ``hmac_sha256(key, hash)``, so an
attacker who can rewrite the whole file still cannot produce a chain that
verifies without the key.

What this does **not** prove is documented in ``docs/AUDIT_TRAIL.md`` and
summarised here, because the honesty invariant in CLAUDE.md applies to code
comments too:

* Removing whole trailing entries leaves a chain that still verifies.  The
  only defence is anchoring :meth:`AuditLog.head_hash` somewhere outside the
  file (printed, stored in the package, sent to another system).  A truncation
  that cuts a line in half *is* detected.
* Timestamps come from the server clock.  This module trusts it.
* This module is **not** a claim of 21 CFR Part 11 compliance.  It implements
  the direction Part 11 points in - secure, computer-generated, time-stamped
  records that do not obscure previously recorded information - but Part 11
  also requires e-signature binding, authority checks and a validated system,
  none of which live here.

Standard library only (hashlib, hmac, json, fcntl, os, dataclasses,
datetime): the audit trail must not be able to fail because of a dependency.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: ``prev_hash`` of the first entry in a log.  A chain that starts anywhere
#: else did not start at the beginning.
GENESIS = "0" * 64

#: Every event the trail can record.  Unknown events are rejected on append
#: and reported by :func:`verify_job`, so a reader never has to guess what a
#: line means.
EVENTS = (
    "job.created",
    "content.generated",
    "review.opened",
    "content.edited",
    "content.approved",
    "package.exported",
    "download.served",
    "retention.deleted",
)

#: Events that describe a specific state of the training content.  Each one
#: must carry the ``content_hash`` of the content it is about, or the trail
#: cannot answer "is what shipped what was approved?".
CONTENT_EVENTS = (
    "content.generated",
    "content.edited",
    "content.approved",
    "package.exported",
)

#: Where the actor's claim of identity came from.  ``system`` is the app
#: itself (retention sweeps, automatic regeneration); it is not a person.
ACTOR_SOURCES = ("web", "cli", "system")

#: Environment variable holding the HMAC key (utf-8).  Unset or empty means
#: "no key": the chain still verifies, the MACs simply are not there.
ENV_KEY_VAR = "AUDIT_HMAC_KEY"

#: Per-job log filename, and the global log used for events that outlive a
#: job directory (``retention.deleted``).
LOG_FILENAME = "audit.jsonl"
GLOBAL_LOG_FILENAME = "audit-global.jsonl"
GLOBAL_JOB_ID = "_global"

#: The exact field set of an entry line.  Verification rejects a line with
#: any missing or extra field: a field the hash payload does not cover would
#: be a place to hide un-hashed content.
_ENTRY_FIELDS = (
    "seq", "ts", "job_id", "event", "actor", "content_hash",
    "details", "prev_hash", "hash", "mac",
)

#: The fields covered by ``hash`` - every field except the hash and its MAC.
_PAYLOAD_FIELDS = (
    "seq", "ts", "job_id", "event", "actor", "content_hash", "details", "prev_hash",
)

_ACTOR_FIELDS = ("name", "role", "source")

_HEX = set("0123456789abcdef")


# --- primitives -----------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """One JSON encoding, so one hash.

    Sorted keys, no insignificant whitespace, no ASCII escaping.  Every hash
    in this module is taken over the output of this function, and every line
    written to the log is produced by it, so a line re-hashes to itself
    byte-for-byte regardless of how the dict was built.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(training_module: Dict, assessment: Dict) -> str:
    """SHA-256 of the exact training content a job holds at one moment.

    ``training_module`` and ``assessment`` are the ``to_dict()`` forms the
    review workflow already persists in ``job.json`` (see
    ``src/serialization.py``), so the hash covers every field an SME can edit
    - including the answer key, which is what makes "edited after approval"
    detectable at all.
    """
    return _sha256_hex(canonical_json(
        {"training_module": training_module, "assessment": assessment}))


def file_hash(path) -> str:
    """SHA-256 of a file's bytes, streamed - packages are tens of MB."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_hash(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in _HEX for char in value))


def _utcnow_iso() -> str:
    """UTC, ISO-8601, microseconds, explicit ``+00:00``.

    Microseconds because two events in one request must not collide into the
    same timestamp; the explicit offset because a naive timestamp in an audit
    record is an unanswerable question three years later.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _mac(key: bytes, entry_hash: str) -> str:
    return hmac.new(key, entry_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def hmac_key_from_env(env: Optional[Mapping[str, str]] = None,
                      var: str = ENV_KEY_VAR) -> Optional[bytes]:
    """Read the HMAC key from the environment, or ``None`` when unset/empty.

    ``var`` is an additive convenience for the CLI's ``--key-env``; callers
    following the module contract just call ``hmac_key_from_env()``.
    """
    source = os.environ if env is None else env
    raw = (source.get(var) or "").strip()
    return raw.encode("utf-8") if raw else None


# --- records --------------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    """Who the application says performed the action.

    This module records the claim; it does not authenticate it.  Until M2
    brings accounts, ``name`` is whatever the reviewer typed into the
    approval form - the same identity the approval record already carries.
    """

    name: str
    role: str
    source: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("actor name must be a non-empty string")
        if not isinstance(self.role, str):
            raise ValueError("actor role must be a string")
        if self.source not in ACTOR_SOURCES:
            raise ValueError(
                f"actor source must be one of {ACTOR_SOURCES!r}, got {self.source!r}")

    def to_dict(self) -> Dict:
        return {"name": self.name, "role": self.role, "source": self.source}


def _coerce_actor(actor: Any) -> Actor:
    if isinstance(actor, Actor):
        # Re-validate: a frozen dataclass is not immutable against
        # object.__setattr__, and this is the last gate before the record.
        return Actor(actor.name, actor.role, actor.source)
    if isinstance(actor, Mapping):
        unknown = sorted(set(actor) - set(_ACTOR_FIELDS))
        if unknown:
            raise ValueError(f"unknown actor field(s): {unknown}")
        return Actor(actor.get("name", ""), actor.get("role", ""),
                     actor.get("source", ""))
    raise ValueError("actor must be an Actor (or a mapping with name/role/source)")


@dataclass(frozen=True)
class AuditEntry:
    """One recorded action.  ``hash`` covers every other field but ``mac``."""

    seq: int
    ts: str
    job_id: str
    event: str
    actor: Dict
    content_hash: Optional[str]
    details: Dict
    prev_hash: str
    hash: str
    mac: Optional[str]

    def to_dict(self) -> Dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "job_id": self.job_id,
            "event": self.event,
            "actor": dict(self.actor),
            "content_hash": self.content_hash,
            "details": dict(self.details),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "mac": self.mac,
        }

    def payload(self) -> Dict:
        """The hashed part of the entry: everything except ``hash``/``mac``."""
        full = self.to_dict()
        return {name: full[name] for name in _PAYLOAD_FIELDS}

    def expected_hash(self) -> str:
        return _sha256_hex(canonical_json(self.payload()))


@dataclass(frozen=True)
class VerificationResult:
    """What verification found.

    ``ok`` is the whole answer: true only when nothing at all was found.
    ``problems`` lists every finding in the order found, of two kinds an
    auditor reads differently:

    * *integrity* - the file has been altered (hash, chain, MAC, malformed
      line).  Someone edited the record.
    * *consistency* - the file is intact but describes a job that broke its
      own rules (content edited after approval, a package exported that is
      not the approved content, a trail that does not start at job.created).

    ``package_matches_approval`` is tri-state on purpose: ``True``/``False``
    when the trail holds an approval followed by an export, ``None`` when the
    question does not apply yet.  ``False`` and ``None`` must never be
    reported to an auditor as the same thing.
    """

    ok: bool
    entries: int
    first_bad_seq: Optional[int]
    reason: Optional[str]
    problems: List[str]
    head_hash: str
    package_matches_approval: Optional[bool]

    def to_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "entries": self.entries,
            "first_bad_seq": self.first_bad_seq,
            "reason": self.reason,
            "problems": list(self.problems),
            "head_hash": self.head_hash,
            "package_matches_approval": self.package_matches_approval,
        }


# --- line parsing ---------------------------------------------------------

def _entry_from_obj(obj: Any) -> Tuple[Optional[AuditEntry], Optional[str]]:
    """Build an :class:`AuditEntry` from a parsed line, or say why not.

    Rejects unknown and missing fields outright.  A field outside
    ``_ENTRY_FIELDS`` would not be covered by ``hash``, which would make it a
    place to smuggle un-hashed content into a record that otherwise verifies.
    """
    if not isinstance(obj, dict):
        return None, "entry is not a JSON object"
    missing = sorted(set(_ENTRY_FIELDS) - set(obj))
    unknown = sorted(set(obj) - set(_ENTRY_FIELDS))
    if missing:
        return None, f"entry is missing field(s) {missing}"
    if unknown:
        return None, f"entry carries unknown field(s) {unknown} (not covered by its hash)"
    if not isinstance(obj["seq"], int) or isinstance(obj["seq"], bool):
        return None, "seq is not an integer"
    for name in ("ts", "job_id", "event", "prev_hash", "hash"):
        if not isinstance(obj[name], str):
            return None, f"{name} is not a string"
    if not isinstance(obj["actor"], dict):
        return None, "actor is not an object"
    if not isinstance(obj["details"], dict):
        return None, "details is not an object"
    if obj["content_hash"] is not None and not isinstance(obj["content_hash"], str):
        return None, "content_hash is not a string or null"
    if obj["mac"] is not None and not isinstance(obj["mac"], str):
        return None, "mac is not a string or null"
    return AuditEntry(
        seq=obj["seq"],
        ts=obj["ts"],
        job_id=obj["job_id"],
        event=obj["event"],
        actor=obj["actor"],
        content_hash=obj["content_hash"],
        details=obj["details"],
        prev_hash=obj["prev_hash"],
        hash=obj["hash"],
        mac=obj["mac"],
    ), None


def _split_lines(text: str) -> Tuple[List[str], bool]:
    """Split log text into complete lines, flagging a trailing partial one.

    A line is complete only if it ends with a newline.  A crash or a
    truncation mid-write leaves the last line without one - that is the one
    truncation case the chain itself can see.
    """
    if not text:
        return [], False
    partial = not text.endswith("\n")
    # The final element of the split is "" for a complete file, or the
    # partial line; either way it is not a complete line.
    return text.split("\n")[:-1], partial


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a UTC ISO-8601 timestamp, or ``None`` if it is not one.

    Only UTC is accepted: a local time in an audit record is ambiguous
    forever, and this module only ever writes UTC.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


# --- the log --------------------------------------------------------------

class AuditLog:
    """Append-only hash-chained log for one job.

    Concurrency: :meth:`append` holds an exclusive ``flock`` across
    read-tail-then-write and writes exactly one line with ``O_APPEND``
    followed by ``fsync``, so several processes appending to the same file
    still produce one valid chain with contiguous sequence numbers.

    Nothing in this module ever rewrites, truncates or reorders the file.
    """

    def __init__(self, path, job_id: str, hmac_key: Optional[bytes] = None):
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if hmac_key is not None and not isinstance(hmac_key, (bytes, bytearray)):
            raise ValueError("hmac_key must be bytes or None")
        self.path = Path(path)
        self.job_id = job_id
        self.hmac_key = bytes(hmac_key) if hmac_key else None

    @classmethod
    def for_job(cls, job_dir, job_id: str,
                hmac_key: Optional[bytes] = None) -> "AuditLog":
        return cls(Path(job_dir) / LOG_FILENAME, job_id, hmac_key)

    def exists(self) -> bool:
        return self.path.is_file()

    # -- append ------------------------------------------------------------

    def append(self, event: str, actor: Actor, details: Optional[Dict] = None,
               content_hash: Optional[str] = None) -> AuditEntry:
        """Record one action and return the entry exactly as written.

        Everything is validated *before* the file is touched: a rejected call
        must not leave a half-formed record behind.
        """
        if event not in EVENTS:
            raise ValueError(
                f"unknown audit event {event!r}; expected one of {EVENTS!r}")
        actor_obj = _coerce_actor(actor)
        details = self._validated_details(details)
        if content_hash is not None and not _is_hash(content_hash):
            raise ValueError("content_hash must be a 64-character hex SHA-256, or None")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                prev_seq, prev_hash, needs_newline = self._tail(fd)
                entry = self._build(
                    seq=prev_seq + 1, event=event, actor=actor_obj.to_dict(),
                    content_hash=content_hash, details=details, prev_hash=prev_hash)
                line = canonical_json(entry.to_dict()) + "\n"
                if needs_newline:
                    # The file ends mid-line (an interrupted write). Start a
                    # new line rather than splicing onto it - and leave the
                    # damaged line in place: verification reports it, and
                    # nothing here erases what was already recorded.
                    line = "\n" + line
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        if not existed:
            self._fsync_dir()
        return entry

    @staticmethod
    def _validated_details(details: Optional[Dict]) -> Dict:
        if details is None:
            return {}
        if not isinstance(details, dict):
            raise ValueError("details must be a dict")
        try:
            round_tripped = json.loads(canonical_json(details))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"details must be JSON-serialisable: {exc}") from exc
        if not isinstance(round_tripped, dict):  # pragma: no cover - defensive
            raise ValueError("details must be a JSON object")
        if round_tripped != details:
            # Integer keys, tuples, NaN: things json silently rewrites. An
            # audit record must say what the caller said, not a coercion of it.
            raise ValueError(
                "details must round-trip through JSON unchanged "
                "(string keys, lists rather than tuples, no NaN/Infinity)")
        return round_tripped

    def _build(self, seq, event, actor, content_hash, details, prev_hash) -> AuditEntry:
        payload = {
            "seq": seq,
            "ts": _utcnow_iso(),
            "job_id": self.job_id,
            "event": event,
            "actor": actor,
            "content_hash": content_hash,
            "details": details,
            "prev_hash": prev_hash,
        }
        entry_hash = _sha256_hex(canonical_json(payload))
        return AuditEntry(
            hash=entry_hash,
            mac=_mac(self.hmac_key, entry_hash) if self.hmac_key else None,
            **payload,
        )

    def _tail(self, fd) -> Tuple[int, str, bool]:
        """Last recorded (seq, hash) and whether the file ends mid-line.

        Read with ``pread`` so the append offset is untouched, under the lock
        the caller already holds.  Scans backwards past damaged lines rather
        than refusing to append: a corrupted line must not stop the record
        from continuing, and verification still reports it.
        """
        size = os.fstat(fd).st_size
        raw = os.pread(fd, size, 0) if size else b""
        lines, partial = _split_lines(raw.decode("utf-8", errors="replace"))
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            entry, _ = _entry_from_obj(obj)
            if entry is not None and _is_hash(entry.hash):
                return entry.seq, entry.hash, partial
        return 0, GENESIS, partial

    def _fsync_dir(self) -> None:
        """Make the log file's *existence* durable, not just its bytes."""
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(dir_fd)

    # -- read --------------------------------------------------------------

    def _load(self) -> Tuple[List[AuditEntry], List[str]]:
        entries: List[AuditEntry] = []
        problems: List[str] = []
        if not self.path.is_file():
            return entries, problems
        text = self.path.read_bytes().decode("utf-8", errors="replace")
        lines, partial = _split_lines(text)
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                problems.append(f"line {number}: blank line in the log")
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                problems.append(f"line {number}: not valid JSON")
                continue
            entry, why = _entry_from_obj(obj)
            if entry is None:
                problems.append(f"line {number}: {why}")
                continue
            entries.append(entry)
        if partial:
            problems.append(
                f"line {len(lines) + 1}: incomplete final line "
                "(the log was truncated or a write was interrupted)")
        return entries, problems

    def entries(self) -> List[AuditEntry]:
        """Every well-formed entry, in file order.

        Tolerant by design: a trailing partial line or a corrupted line is
        skipped here and reported by :meth:`verify`, because a reader must
        still be able to see the records that *are* intact.
        """
        return self._load()[0]

    def head_hash(self) -> str:
        """Hash of the last entry - the value to anchor outside this file."""
        entries = self.entries()
        return entries[-1].hash if entries else GENESIS

    # -- verify ------------------------------------------------------------

    def verify(self) -> VerificationResult:
        """Chain and MAC checks only: is this file the record it claims to be?"""
        return self._verify(semantic=False)

    def _verify(self, semantic: bool) -> VerificationResult:
        entries, problems = self._load()
        problems = list(problems)
        bad: List[int] = []

        prev = GENESIS
        for entry in entries:
            if entry.hash != entry.expected_hash():
                problems.append(
                    f"entry {entry.seq}: contents do not match its recorded hash")
                bad.append(entry.seq)
            if entry.prev_hash != prev:
                problems.append(
                    f"entry {entry.seq}: prev_hash does not chain to the previous entry")
                bad.append(entry.seq)
            if self.hmac_key is not None:
                if not entry.mac:
                    problems.append(
                        f"entry {entry.seq}: no MAC, but an HMAC key is configured")
                    bad.append(entry.seq)
                elif not hmac.compare_digest(entry.mac, _mac(self.hmac_key, entry.hash)):
                    problems.append(f"entry {entry.seq}: MAC does not verify")
                    bad.append(entry.seq)
            prev = entry.hash

        package_matches = None
        if semantic:
            sem_problems, sem_bad, package_matches = self._semantic(entries)
            problems.extend(sem_problems)
            bad.extend(sem_bad)

        return VerificationResult(
            ok=not problems,
            entries=len(entries),
            first_bad_seq=min(bad) if bad else None,
            reason=problems[0] if problems else None,
            problems=problems,
            head_hash=entries[-1].hash if entries else GENESIS,
            package_matches_approval=package_matches,
        )

    def _semantic(self, entries: List[AuditEntry]):
        """Does the intact record describe a job that followed its own rules?"""
        problems: List[str] = []
        bad: List[int] = []

        if not entries:
            problems.append("the audit log is empty or missing")
            return problems, bad, None

        for index, entry in enumerate(entries, start=1):
            if entry.seq != index:
                problems.append(
                    f"entry at position {index}: seq is {entry.seq}, expected {index} "
                    "(sequence numbers must be contiguous from 1)")
                bad.append(min(entry.seq, index))
            if entry.job_id != self.job_id:
                problems.append(
                    f"entry {entry.seq}: job_id {entry.job_id!r} is not {self.job_id!r}")
                bad.append(entry.seq)
            if entry.event not in EVENTS:
                problems.append(f"entry {entry.seq}: unknown event {entry.event!r}")
                bad.append(entry.seq)
            if _parse_ts(entry.ts) is None:
                problems.append(
                    f"entry {entry.seq}: ts {entry.ts!r} is not a UTC ISO-8601 timestamp")
                bad.append(entry.seq)
            if entry.event in CONTENT_EVENTS and not _is_hash(entry.content_hash):
                problems.append(
                    f"entry {entry.seq}: {entry.event} carries no content_hash")
                bad.append(entry.seq)

        for earlier, later in zip(entries, entries[1:]):
            first, second = _parse_ts(earlier.ts), _parse_ts(later.ts)
            if first is not None and second is not None and second < first:
                problems.append(
                    f"entry {later.seq}: timestamp goes backwards "
                    f"({later.ts} precedes {earlier.ts})")
                bad.append(later.seq)

        if entries[0].event != "job.created":
            problems.append(
                f"the trail starts with {entries[0].event!r}, not 'job.created' "
                "(the entries before it are missing)")
            bad.append(entries[0].seq)

        approvals = [e for e in entries if e.event == "content.approved"]
        package_matches = None
        if approvals:
            approved = approvals[-1]
            for edit in entries:
                if edit.event == "content.edited" and edit.seq > approved.seq:
                    problems.append(
                        f"entry {edit.seq}: content edited after approval "
                        f"(entry {approved.seq}) with no later approval")
                    bad.append(edit.seq)

            exports_after = [e for e in entries
                             if e.event == "package.exported" and e.seq > approved.seq]
            if exports_after:
                export = exports_after[-1]
                package_matches = bool(
                    _is_hash(export.content_hash)
                    and _is_hash(approved.content_hash)
                    and export.content_hash == approved.content_hash)
                if not package_matches:
                    problems.append(
                        f"entry {export.seq}: the exported package is not the content "
                        f"that was approved (entry {approved.seq})")
                    bad.append(export.seq)

        return problems, bad, package_matches


def verify_job(job_dir, job_id: str,
               hmac_key: Optional[bytes] = None) -> VerificationResult:
    """Full verification of a job's trail: chain + MAC + workflow semantics."""
    return AuditLog.for_job(job_dir, job_id, hmac_key)._verify(semantic=True)


def global_log(output_folder, hmac_key: Optional[bytes] = None) -> AuditLog:
    """The log for events that outlive a job directory.

    ``retention.deleted`` is recorded here, with the deleted job's last head
    hash in ``details``, because the job's own log is deleted with it.
    """
    return AuditLog(Path(output_folder) / GLOBAL_LOG_FILENAME,
                    GLOBAL_JOB_ID, hmac_key)


# --- CLI ------------------------------------------------------------------

def _details_summary(details: Dict, limit: int = 60) -> str:
    parts = []
    for key in sorted(details):
        value = details[key]
        text = value if isinstance(value, str) else canonical_json(value)
        if len(text) > limit:
            text = text[:limit - 1] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _show(log: AuditLog) -> int:
    entries, problems = log._load()
    for entry in entries:
        actor = entry.actor
        who = (f"{actor.get('name', '?')} "
               f"({actor.get('role', '')}, {actor.get('source', '')})")
        digest = (entry.content_hash or "")[:12] or "-"
        line = (f"{entry.seq:>4}  {entry.ts}  {entry.event:<18}  {who:<34}  "
                f"{digest:<12}  {_details_summary(entry.details)}")
        print(line.rstrip())
    for problem in problems:
        print(f"  !!  {problem}", file=sys.stderr)
    print(f"head_hash={log.head_hash()}", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m src.audit",
        description="Verify or print a training job's audit trail.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
            ("verify", "verify the trail and print the result as JSON"),
            ("show", "print one line per entry")):
        part = sub.add_parser(name, help=help_text)
        part.add_argument("job_dir", help="the job directory holding audit.jsonl")
        part.add_argument(
            "--job-id", default=None,
            help="job id the entries must carry (default: the directory name)")
        part.add_argument(
            "--key-env", default=ENV_KEY_VAR,
            help=f"environment variable holding the HMAC key (default: {ENV_KEY_VAR})")

    args = parser.parse_args(argv)
    job_dir = Path(args.job_dir)
    job_id = args.job_id or job_dir.resolve().name
    key = hmac_key_from_env(var=args.key_env)

    if args.command == "show":
        return _show(AuditLog.for_job(job_dir, job_id, key))

    result = verify_job(job_dir, job_id, key)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
