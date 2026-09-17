# Training Creator — Product Goal

## North star

> **Turn any controlled SOP into an audit-ready, LMS-deployable training module in under five
> minutes — with every sentence of training content traceable to the source document, and an
> assessment a quality auditor would accept as objective evidence of competence.**

Success is not "it generates a SCORM file." Success is a QA manager in a regulated
manufacturer choosing this over building the course by hand in Articulate, *and* being able to
defend that choice in an FDA or notified-body audit.

## Who this is for

**Primary buyer/user:** Quality, Regulatory, and Training leads at 50–1000 person regulated
manufacturers — medical device (ISO 13485 / 21 CFR 820), pharma, food, and aerospace.

**The job they're hiring us for:** They have 200–2000 controlled SOPs. Every revision triggers
a retraining obligation with documented effectiveness checks. Today that means a training
coordinator spending 4–8 hours per SOP in an authoring tool, and a permanent backlog that shows
up as an audit finding.

**Why we can win:** Generic AI course generators don't understand controlled documents,
traceability, or revision-driven retraining. Authoring tools (Storyline, Rise, iSpring) are
powerful but manual. The wedge is *SOP-native*: revision-aware, traceable, and compliance-shaped
by default.

## What "world class" means here — the acceptance bar

The goal is met when all of the following are objectively true and measured:

| # | Criterion | Target |
|---|---|---|
| 1 | **Content fidelity** — training content preserves the source procedure | ≥98% of procedure steps captured with full body text; every generated sentence linked to a source line |
| 2 | **Assessment validity** — the quiz actually discriminates | 0 questions answerable without reading the SOP; answer position uniformly distributed; SME edit rate <30% |
| 3 | **Assessment integrity** — the quiz can't be gamed | Answer key never shipped to the learner's browser; scoring server- or LMS-verified |
| 4 | **LMS conformance** | Packages import and report completion/score cleanly in Moodle, Canvas, Cornerstone, SuccessFactors, TalentLMS — verified by automated ADL conformance tests, not by assertion |
| 5 | **Time to value** | Signup → first SCORM package in <5 min, no docs read; p95 generation <60s for a 30-page SOP |
| 6 | **Trust** | SME review workflow with change tracking; nothing reaches learners without a named human approval |
| 7 | **Audit readiness** | Immutable audit trail of who generated/approved/published what, from which SOP revision, with 21 CFR Part 11-capable e-signature |
| 8 | **Reliability** | 99.9% uptime; no data loss; tenant-isolated storage; documented retention/deletion |
| 9 | **Honesty** | Every capability claimed in marketing and README exists and is tested in CI |

## Honest current state (as of this commit)

A working MVP with a real pipeline (parse → generate → assess → SCORM) and genuinely good
instincts on compliance framing (draft watermarks, transparency report, "not a validated
system" disclosure). But it is **not** yet a product anyone should run a regulated training
program on. The blocking gaps, verified in the code:

**Correctness / integrity (must fix before any paying customer)**
- **The quiz can be passed without reading anything.** Seven of the generated question types
  hardcode `correct_answer=0` (`src/assessments.py:156,170,218,239,291,361,374`) and the SCORM
  renderer emits options in source order (`src/scorm_exporter.py:326`). The
  `randomize_options` flag exists but is never consumed (`src/assessments.py:48`). Always
  clicking the first option scores 100%. This invalidates every training record the tool
  produces — it is the single most serious defect in the repo.
- **The answer key ships to the learner.** The full question objects, including
  `correct_answer` and `explanation`, are serialized into client-side JS
  (`src/scorm_exporter.py:349`). View-source defeats the assessment.
- **Distractors are giveaways.** Wrong answers are self-evidently absurd ("To increase
  paperwork requirements", "Skip this step if time is limited") while the correct answer is
  verbatim SOP text.
- **Question selection is `random.sample`** — non-deterministic, with no guarantee that safety
  or critical-step content is covered.

**Content fidelity**
- **Procedure bodies are silently dropped.** The step regex terminates at a blank line
  (`src/parser.py:186`), so "Step 2: Activate Emergency Stop" is captured as the *heading only* —
  the actual instructions underneath are lost. On the bundled sample SOP, all 9 steps lose their
  body text.
- **Definitions parse incorrectly.** `Emergency Shutdown (E-Stop): ...` yields the term `"E"`
  (`src/parser.py:213`).
- **Responsibilities are never extracted.** The field exists and the generator renders it
  (`src/generator.py:168`), but the parser never populates it — dead code path.
- **No semantic understanding.** "Intelligent Content Analysis" is regex. Learning objectives
  are string-slices of source text (`"Successfully perform step 3: Notify Supervision..."`),
  not Bloom's-taxonomy objectives.

**Product / platform**
- No accounts, no tenancy, no database. Jobs run synchronously inside the request
  (`app.py:106`); outputs land on local disk and vanish on container restart.
- `SECRET_KEY` hardcoded (`app.py:25`); `debug=True` in the entrypoint (`app.py:257`).
- Download authorization is "know the UUID" — no ownership check (`app.py:190`).
- No rate limiting, no upload scanning, no retention/deletion policy.
- No CI, and `test_basic.py` / `test_medical_device.py` are print scripts with no assertions —
  they cannot fail.

**Truth in advertising**
- README advertises **xAPI (Tin Can)** output (`README.md:84`) and **customizable templates**
  (`README.md:15`). Neither exists anywhere in the codebase.
- README claimed compatibility with six named LMS platforms with no conformance test behind it
  (removed in M0; replaced in the pilot-hardening pass by an XSD + fake-LMS harness — still no real LMS import).

## Status — M0 complete (2026-09-17)

Verified on the merged tree, not by assertion:

- **Naive learners fail.** The worst any fixed strategy scores — always option 1/2/3/4, always
  last, always True, always the longest option, always the shortest — is **60%**, against pass
  marks of 70% (standard) and 80% (medical device). Measured independently of the test suite on
  both shipped fixtures, both generators, 5–12 questions. Before M0 the answer was 100%.
  `tests/test_assessments.py` asserts this property against each assessment's own passing score.
- **No answer key in the package.** Learner-facing files carry only a per-question salt and a
  salted SHA-256 of the normalised correct text; `correct_answer` and `explanation` never leave
  Python. Verified in a real headless Chromium run on both code paths the shipped JavaScript can
  take (`crypto.subtle` over http://localhost, pure-JS fallback over `file://`): always-first
  fails, all-correct passes. Honest limit, documented in `src/answer_key.py` and `metadata.json`:
  a learner with dev tools can hash the 2–4 displayed options against the public salt; server-side
  scoring is M2.
- **Content fidelity.** Every step now carries its full body, sub-steps, and `source_lines`;
  definitions, responsibilities, purpose and scope parse from their sections; numbered
  conventions (`4.1`, `4.2`, `4.10`) work. 458 pytest assertions replace two print scripts that
  could not fail; CI runs them on Python 3.10–3.12.
- **Platform hygiene.** Secret and debug flag from the environment, signed and expiring download
  links, path containment, JSON 413s, source documents deleted after processing, 24h output
  retention. README no longer claims xAPI, customizable templates, or tested LMS compatibility.
- **Minimum assessment length is now 5.** A three-question quiz with one double-weighted item
  cannot be made ungameable by layout; the generator, API and CLI refuse rather than pretend.

**M1 (2026-09-17) — the machinery is built; the exit criterion is open.** Every extracted field
carries a source span; objectives are Bloom's-aligned and cited; sections and questions cite their
source; the transparency report measures citation coverage (95% on the fixtures — only the two
regulatory compliance questions are uncited, by design). The web app has the SME loop: source
beside generated content, inline edits with strict validation, server-owned answer layout so an
edit cannot reintroduce a gameable quiz, and a named approval recorded in the package, its pages,
and the report. The optional Claude layer (off by default) rewrites objectives, summarises
sections and proposes distractors, and a lexical grounding check rejects invented numbers, added
actions and low-overlap text — a filter, not a proof; `docs/LLM.md` lists what it cannot catch.
What is *not* done: the exit criterion ("SMEs accept with <30% edits across 20 real SOPs from 3
pilot customers") requires real customers and real SOPs. The `edits_count` the approval records
is the instrument for measuring it.

**Pilot hardening (2026-09-17).** Instrumentation for the M1 exit criterion (`/pilot`, edit rate
defined as edits over editable items across approved jobs); a six-regime sample gallery (medical
device, pharma, food, aerospace, clinical lab, general manufacturing) that now runs through every
invariant test — worst naive strategy 62.5% across all eight documents; and a SCORM conformance
harness that validates both manifests against ADL's official XSDs and drives the package in a
fake LMS in Chromium on both API surfaces. That harness found the "SCORM 2004" output had been a
1.2 manifest with a 1.2 runtime and a version string — non-functional in any 2004 LMS — plus an
API-discovery loop that never terminated inside frames without an LMS, a re-launch that
overwrote a pass with "incomplete", and a missing `LMSFinish`. All fixed; each defect is pinned by
a negative test. Criterion 4 is now *partly* met: schema-valid and runtime-verified against a
fake LMS, not against a real one or ADL's test suite. Per-question evidence is now written as
`cmi.interactions` in both bindings, with `correct_responses` deliberately omitted so the answer
key never travels through the learner's browser (an auditor sees what was answered and whether it
was right, not the key); the remaining gaps are a real LMS import and server-verified scoring
(M2) — see `docs/SCORM_CONFORMANCE.md`.

Carried into M1/M2 from this pass: server-side scoring (the only real fix for the static-package
limit); the SME JSON export legitimately contains the key and sits on disk for the retention
window — it should move behind an ownership check; internal exception text still reaches API
error responses; a bare ALL-CAPS line inside a step body is mistaken for a section heading;
three-level numbering (`4.1.1`) falls through to the integer pattern. `source_lines` and
`source_ref` on every step and question are the groundwork for M1's citation coverage metric.

## Milestones

### M0 — Make it honest and correct *(blocking; nothing else matters first)*
Fix the integrity defects above: shuffle options with a server-side key, strip answers from
client payloads, generate plausible distractors, capture full step bodies, fix definitions and
responsibilities parsing. Replace print-scripts with real pytest assertions including regression
tests for each defect. Add CI. Remove or implement every unbacked README claim.
**Exit:** a deliberately naive learner clicking the first option every time fails the quiz, and
CI proves it.

### M1 — Content a subject-matter expert would sign
Introduce LLM-assisted generation for objectives, summaries, and distractors — with strict
grounding: every generated sentence carries a citation to the source line, and an automated
check rejects unsupported claims. Bloom's-aligned objectives. Question blueprints that guarantee
coverage of safety-critical and step-sequence content. Build the SME review UI: side-by-side
source vs. generated, inline edit, explicit approval.
**Exit:** SMEs accept generated content with <30% edits across 20 real SOPs from 3 pilot
customers.

### M2 — A real application
Accounts, organizations, role-based access (Author / SME Reviewer / Approver / Admin). Postgres
for metadata, object storage for artifacts, background workers for generation, signed and
authorized download URLs. Production config hygiene, rate limiting, upload scanning, retention
policy. Observability: structured logs, error tracking, per-job metrics.
**Exit:** 10 concurrent tenants generating without interference; zero data loss across deploys;
a security review with no high findings.

### M3 — Compliance-grade
Immutable audit trail (who, what, when, from which SOP revision). Part 11-capable e-signature
on approval and publish. Document-revision awareness: detect that SOP-042 moved v2.1 → v2.2,
diff it, and generate *delta* retraining plus an effectiveness check. Training-record
reconciliation back from the LMS. Ship a validation package (IQ/OQ/PQ scripts, traceability
matrix, release notes) so customers can validate the tool inside their own QMS.
**Exit:** a customer passes an external audit with our artifacts in evidence.

### M4 — Market
Self-serve onboarding with a sample SOP; templates per regulatory regime; LMS integrations
(direct publish to Cornerstone, SuccessFactors, Docebo) and document-system integrations
(SharePoint, MasterControl, Veeva, Greenlight Guru); usage-based pricing with a free tier;
SOC 2 Type II.
**Exit:** repeatable self-serve conversion, net revenue retention >110%.

## Explicit non-goals

- **Not an LMS.** We generate and publish; the customer's LMS owns delivery and records
  (21 CFR 820.25 record-keeping stays with them).
- **Not a document control system.** We read controlled SOPs; we don't become the source of truth.
- **Not a general-purpose course authoring tool.** Marketing, sales enablement, and soft-skills
  training are someone else's market. SOP-native is the whole advantage.
- **Not a validated system we certify.** We ship the evidence customers need to validate it
  within their own QMS — the current disclosure posture is correct and should be kept.

## Principal risks

1. **Trust is the product.** One customer audit finding traced to our output ends the company in
   this market. This is why M0 and the citation-grounding in M1 precede every growth feature.
2. **AI skepticism in QA/RA.** Mitigation: grounded generation with visible citations, mandatory
   human approval, no black boxes — sell it as *acceleration with a human in the loop*, never as
   automation of a regulated judgment.
3. **LMS integration surface is deep and unglamorous.** Conformance testing must be automated
   early or M4 collapses into permanent support work.
4. **Incumbent bundling.** eQMS vendors could ship a "good enough" version. Speed to M3 and
   depth of revision-aware retraining are the defense.

## Metrics to instrument now

**Quality:** SME edit rate per module · distractor plausibility (expert-rated) · assessment
discrimination index · citation coverage %
**Product:** time to first package · SOPs converted per org per month · generation success rate ·
p95 generation latency
**Business:** activation (first published package) · weekly active authors · NRR · audit findings
attributable to our output (target: zero)
