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
                       title/body/content/substeps/source_lines)
src/generator.py       TrainingGenerator → TrainingModule (sections as escaped HTML, objectives)
src/assessments.py     AssessmentGenerator / MedicalDeviceAssessmentGenerator → Assessment
                       (deterministic, blueprint-selected, document-drawn distractors,
                       joint answer-position search; min 5 questions)
src/answer_key.py      salted-hash learner keys + the client-side verifier JS (Python and JS
                       normalisers must stay identical — change both or neither)
src/scorm_exporter.py  two distinct CAM bindings (1.2 / 2004 4th Ed), one API wrapper that finds
                       API or API_1484_11 through frames, content pages, assessment.html, zip
src/transparency_report.py  SME/auditor report (HTML + JSON): citation coverage, approval record
src/serialization.py   rebuild model objects from their to_dict() JSON (review round-trips)
src/llm/               optional grounded LLM layer (config, provider, prompts, grounding, enhance);
                       off by default, fake provider in tests, never raises into the pipeline
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
- **SME edits cannot bypass invariant 1.** `POST /api/review` re-runs the answer-position layout
  and rejects content a naive strategy would still pass. `tests/test_review.py`.
- **Every generated sentence cites source lines.** Objectives, sections and questions carry
  `source_ref`; the transparency report measures coverage. LLM output that fails grounding is
  dropped in favour of the deterministic original. `tests/test_transparency.py`, `tests/test_llm.py`.
- **SCORM packages are schema-valid and run against a fake LMS on both API surfaces.**
  `tests/conformance/` (manifests against ADL's vendored XSDs; runtime in Chromium, SCO two frames
  below the API window). No real LMS has been tested — say so.
- **Don't claim what isn't tested.** README/USAGE_GUIDE/DEPLOYMENT describe only what exists.

## Working conventions

- Tests are pytest assertions in `tests/`. A test that cannot fail is not a test.
- When work runs in parallel (worktrees), each stream owns a disjoint file set; needs in
  someone else's file go in the report, not the diff. Fable plans and checks; the checker
  re-measures independently (see `GOAL.md` status for what that caught).
- Commits: clear message; end with the attribution footer used in this repo's history.
- Known parser edges (not yet fixed): a bare ALL-CAPS line inside a step body reads as a section
  heading; `4.1.1`-style three-level numbering falls through to the integer pattern.

## Progress log

| Date | Milestone | State | Evidence |
|---|---|---|---|
| 2026-09-16 | Goal defined | done | `GOAL.md` |
| 2026-09-17 | **M0 — honest and correct** | **done** | 458 tests; naive worst 60% vs 70/80 pass; no key in package; both browser hash paths verified; CI on 3.10–3.12 |
| 2026-09-17 | **M1 — content an SME would sign (machinery)** | **built; pilot exit criterion open** | 615 tests; provenance on every field, 95% citation coverage on fixtures (only the 2 compliance questions uncited by design); Bloom's objectives; SME review/edit/approve with server-owned answer layout; approval in package + report; grounded LLM layer off by default, fails closed. The "<30% SME edits across 20 real SOPs from 3 pilots" exit needs real customers — not measurable here |
| 2026-09-17 | **Pilot hardening** (owner's call: pilot M1 before M2) | **done** | 1203 tests. `/pilot` metrics with the edit-rate definition; six-regime gallery in the invariant sweep (worst naive strategy 62.5% across 8 documents); SCORM harness found the "2004" package was a 1.2 manifest with a 1.2 runtime, an API-discovery loop that hung inside frames, re-launch overwriting a pass, and no `LMSFinish` — all fixed and pinned by tests. Still open: no `cmi.interactions` (per-question evidence for auditors), no real-LMS import |
| — | M2 — real application (accounts, DB, workers, server-side scoring) | on hold | owner chose to pilot M1 first |
| — | M3 — compliance-grade (audit trail, Part 11, revision-delta retraining, validation pack) | not started | |
| — | M4 — market | not started | |

Update this table when a milestone changes state; put the measured evidence, not the intent.
