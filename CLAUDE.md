# CLAUDE.md — Training Creator

Converts controlled SOPs (txt/md/pdf/docx) into SCORM 1.2/2004 training packages with a
verification assessment, for regulated manufacturers (medical device, pharma). Read `GOAL.md`
first: it defines what "world class" means here, the milestones, and the honest current state.

## Commands

```bash
python3 -m pytest tests -q            # full suite (all assertions; no print-script tests)
python3 -m src.cli -i examples/sample_sop.txt -o /tmp/out -f scorm1.2 --questions 5
python3 app.py                        # Flask UI on 127.0.0.1:5000 (FLASK_DEBUG=1 for reload)
```

Dependencies are in `requirements.txt` (pinned `~=`). CI: `.github/workflows/ci.yml`, Python 3.10–3.12.

## Architecture

```
src/parser.py          SOPParser → SOPContent (heading-driven sections; procedures carry
                       title/body/content/substeps/source_lines); runs the injection scan and
                       exposes it as `injection_scan`
src/injection_scan.py  deterministic scan of untrusted document text: instruction-to-AI phrasing,
                       role markers, hidden/invisible characters, base64, markup; risk none/low/high;
                       false positives measured on the shipped corpus and on 107 real documents
src/generator.py       TrainingGenerator → TrainingModule (sections as escaped HTML, objectives)
src/assessments.py     AssessmentGenerator / MedicalDeviceAssessmentGenerator → Assessment
                       (deterministic, blueprint-selected, document-drawn distractors,
                       joint answer-position search; min 5 questions; register-matched
                       purpose/scope options; no text answers two questions — `leakage_count`)
src/answer_key.py      salted-hash learner keys + the client-side verifier JS (Python and JS
                       normalisers must stay identical — change both or neither)
src/scorm_exporter.py  two distinct CAM bindings (1.2 / 2004 4th Ed), one API wrapper that finds
                       API or API_1484_11 through frames, content pages, assessment.html, zip
src/transparency_report.py  SME/auditor report (HTML + JSON): citation coverage, approval record,
                       audit trail with verification
src/audit.py           append-only hash-chained audit.jsonl per job (optional HMAC via AUDIT_HMAC_KEY);
                       verify_job checks the chain AND that the exported package is the approved content
src/serialization.py   rebuild model objects from their to_dict() JSON (review round-trips)
src/llm/               optional grounded LLM layer (config, provider, prompts, grounding, enhance);
                       off by default, fake provider in tests, never raises into the pipeline; the
                       document travels in a delimited user-turn data block (never in `system`);
                       gated off for `high`-risk documents; every model output passes output filters
src/medical_device_config.py required compliance questions, integrity disclosure
app.py                 Flask API: upload → generate → job.json → signed download / review
                       (GET /review, POST /api/review, POST /api/approve); src/cli.py the CLI
```

## Non-negotiable invariants (each has a test — keep it that way)

- **A naive learner must fail.** Every fixed strategy (always option N, always last, always
  True, always longest/shortest) scores below the assessment's own passing score, for both
  fixtures, both generators, every question count ≥ 5. `tests/test_assessments.py`.
- **No answer key reaches the learner.** Learner-facing files carry only `salt` + `answer_hash`;
  never `correct_answer` or `explanation`. `tests/test_scorm.py` (includes a real-browser check).
- **Deterministic builds.** Same SOP title+version → byte-identical package. Use
  `random.Random(seed)` instances only; never the `random` module functions.
- **Document text is untrusted.** `html.escape` everything from the SOP before it enters HTML.
- **Parser dict contracts are append-only.** Add keys; never rename or remove.
- **No question gives another away.** Purpose/scope options all share the correct answer's
  register (statement vs instruction); no correct answer is a distractor elsewhere; a statement
  and its flip count as one piece of material. Residue is `Assessment.leakage_count` (0 on every
  shipped document, n=5…12). `tests/test_assessments.py`.
- **SME edits cannot bypass invariant 1.** `POST /api/review` re-runs the answer-position layout
  and rejects content a naive strategy would still pass. `tests/test_review.py`.
- **Every generated sentence cites source lines.** Objectives, sections and questions carry
  `source_ref`; the transparency report measures coverage. LLM output that fails grounding is
  dropped in favour of the deterministic original. `tests/test_transparency.py`, `tests/test_llm.py`.
- **A document the scanner rates `high` is never sent to a model**, and no shipped or corpus
  document rates `high` (zero false positives is a hard requirement — widening a pattern means
  re-measuring on the corpus). `tests/test_injection.py`; `tools/corpus_check.py`.
- **SCORM packages are schema-valid and run against a fake LMS on both API surfaces.**
  `tests/conformance/` (manifests against ADL's vendored XSDs; runtime in Chromium, SCO two frames
  below the API window). No real LMS has been tested — say so.
- **Every job action is recorded before it counts.** `audit.jsonl` is append-only and hash-chained;
  approval is appended before `approval.json` is written; the package `metadata.json` carries the
  approval head hash. Verification fails on any altered entry and on export ≠ approved content.
  `tests/test_audit.py`, `tests/test_audit_integration.py`.
- **Don't claim what isn't tested.** README/USAGE_GUIDE/DEPLOYMENT describe only what exists.

## Working conventions

- Tests are pytest assertions in `tests/`. A test that cannot fail is not a test.
- When work runs in parallel (worktrees), each stream owns a disjoint file set; needs in
  someone else's file go in the report, not the diff. Fable plans and checks; the checker
  re-measures independently (see `GOAL.md` status for what that caught).
- Commits: clear message; end with the attribution footer used in this repo's history.
- Parser conventions handled: `Step N:`, `N.`, `N.M` and `N.M.K` (absorbed as sub-steps of an
  enclosing `N.M`, else its own step); ALL-CAPS lines inside a step body are not section headings.

## Progress log

| Date | Milestone | State | Evidence |
|---|---|---|---|
| 2026-09-16 | Goal defined | done | `GOAL.md` |
| 2026-09-17 | **M0 — honest and correct** | **done** | 458 tests; naive worst 60% vs 70/80 pass; no key in package; both browser hash paths verified; CI on 3.10–3.12 |
| 2026-09-17 | **M1 — content an SME would sign (machinery)** | **built; pilot exit criterion open** | 615 tests; provenance on every field, 95% citation coverage on fixtures (only the 2 compliance questions uncited by design); Bloom's objectives; SME review/edit/approve with server-owned answer layout; approval in package + report; grounded LLM layer off by default, fails closed. The "<30% SME edits across 20 real SOPs from 3 pilots" exit needs real customers — not measurable here |
| 2026-09-17 | **Pilot hardening** (owner's call: pilot M1 before M2) | **done** | 1264 tests. `/pilot` metrics with the edit-rate definition; six-regime gallery in the invariant sweep (worst naive strategy 62.5% across 8 documents); SCORM harness found the "2004" package was a 1.2 manifest with a 1.2 runtime, an API-discovery loop that hung inside frames, re-launch overwriting a pass, and no `LMSFinish` — all fixed and pinned by tests. Per-question evidence written as `cmi.interactions` in both bindings (no `correct_responses`). Still open: no real-LMS import |
| — | M2 — real application (accounts, DB, workers, server-side scoring) | on hold | owner chose to pilot M1 first |
| 2026-09-17 | **M3 (partial) — audit trail** (owner's call: before M2) | **done** | 1378 tests collected (1373 pass, 5 opt-in/N.A. skips). Per-job hash-chained `audit.jsonl` (optional HMAC), a 9-entry lifecycle (7 distinct events) recorded from upload to download, approval recorded before it is written, package anchors the head hash, report/review page/pilot dashboard show verification. Independently verified: one altered character in an approver's name is caught at the right entry; export after an unapproved edit flags "package ≠ approved"; the on-disk report for an approved job says package-matches-approval (an independent code review caught that it previously could not, plus an approve-then-export-failure hole — both fixed and pinned). Limits: no external anchor (trailing-entry removal undetectable without one), server clock, unauthenticated actors pre-M2, not a Part 11 claim |
| 2026-09-18 | **Security pass — real documents + prompt injection** | **done** | 107 real web-sourced procedures: 107/107 parsed and exported, 0 gameable, scanner 101 none / 6 low / 0 high. Document moved out of the system prompt; scanner + LLM gate + output filters; five adversarial fixtures; a control character in a title no longer breaks export. The checker's first probe found the scanner missed five obvious attacks — widened, re-measured, and `docs/SECURITY.md` records that a phrasing it misses should be assumed to exist |
| 2026-09-18 | **Question quality — a human read of the output** | **done** | 1593 tests collected (1587 pass, 6 opt-in/N.A. skips). Three defects found by reading one gallery quiz: a purpose question whose answer was the only prose statement among step bodies; one altered warning driving two questions; step answers reused as other questions' distractors. Fixed by a register classifier, an answer-material ledger, and distractor repair that costs a question at most one option. Checker re-measured independently across 8 documents × 2 generators × n=5…12: 0 leaks, 0 shared material, 0 register tells; worst naive strategy unchanged at 62.5% vs 80. Cost: four-option items 74% of MC (was ~94%) on thin fixtures |
| — | M3 — rest (Part 11 e-signature, revision-delta retraining, validation pack) | not started | |
| — | M4 — market | not started | |

Update this table when a milestone changes state; put the measured evidence, not the intent.
