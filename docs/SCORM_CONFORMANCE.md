# SCORM conformance

The README says this tool produces SCORM 1.2 and SCORM 2004 packages. This
document is the evidence for that sentence, and the boundary of it.

The harness lives in `tests/conformance/` and runs in CI. It has two layers:

1. **Manifest** — `imsmanifest.xml` is validated against the official ADL/IMS
   XSDs (vendored offline, see `tests/conformance/schemas/SOURCES.md`) and then
   against the Content Aggregation Model rules a schema cannot express.
2. **Run-time** — the package's own pages are loaded in headless Chromium,
   inside a fake LMS that publishes `API` (SCORM 1.2) or `API_1484_11` (SCORM
   2004) on a window two frames above the content, and records every call,
   argument and return value. The SCO is never handed its API; it has to find
   it, which is the path a real LMS puts it on.

```bash
python3 -m pytest tests/conformance -q            # the whole harness
python3 -m pytest tests/conformance/test_manifest.py -q   # no browser needed
python3 -m pytest tests/conformance/test_runtime.py -q    # needs Chromium
```

The browser tests `pytest.skip` when no Chromium is installed, so the ordinary
`test` CI job stays green without one. The `conformance` job in
`.github/workflows/ci.yml` installs Chromium and runs them for real.

## What the audit found

Rows 1–11 were measured against the tree at `0e7e999`, not inferred. Row 12 is
the gap that audit left open and a later pass closed; it was measured the same
way, against the run-time harness.

| # | Area | What the exporter did before | Consequence | Now |
|---|---|---|---|---|
| 1 | 2004 CAM binding | `scorm_version="2004"` emitted a **SCORM 1.2 manifest**: default namespace `imscp_rootv1p1p2`, `adlcp_rootv1p2`, lowercase `adlcp:scormtype`. Only `<schemaversion>` changed, to the string `"2004"`. | The "2004" package fails the SCORM 2004 4th Edition schema at the root element ("No matching global declaration available for the validation root"). No 2004 LMS will accept it. | `imscp_v1p1` + `adlcp_v1p3` + `adlseq_v1p3` + `adlnav_v1p3` + `imsss`, `adlcp:scormType` with the capital T, and `<schemaversion>2004 4th Edition</schemaversion>`. Validates against ADL's own 4th Edition XSDs. |
| 2 | `<schemaversion>` token | `"2004"` | Not a token SCORM defines. The values are `"1.2"`, `"2004 3rd Edition"`, `"2004 4th Edition"`. | Exact token per version, asserted against the allowed set. |
| 3 | `<file>` declarations | Each resource declared only its own page. `styles.css` and `scorm_api.js` — which **every** page loads — and `metadata.json` were in the zip but declared by nothing. | An LMS that deploys only what the manifest declares serves a SCO with no stylesheet and **no SCORM API wrapper**: `initializeSCORM` is undefined, `window.onload` throws, and the course reports nothing at all. This is a silent, total loss of tracking. | Every resource declares its page plus `styles.css` and `scorm_api.js`; `metadata.json` is declared by an explicit asset resource. The harness asserts declared ⇔ present, both ways. |
| 4 | Run-time API | `scorm_api.js` looked only for `window.API` and spoke only `LMSInitialize`/`LMSSetValue`/`LMSCommit`/`LMSFinish` over `cmi.core.*`. | A SCORM 2004 LMS exposes `API_1484_11` and nothing called `API`. The 2004 package could never report anything. | One wrapper that finds either object and maps the call and the data model onto whichever it found. |
| 5 | **API discovery hangs** | `while (parent && parent != window) { …; parent = parent.parent; }`. At the top window `parent.parent === parent`, and `parent` is never `window`, so **the loop never terminates** when no ancestor exposes `API`. | Measured: the same package that finishes a passing attempt in under two seconds in a 1.2 host **never finishes loading** in a 2004 host — the tab locks up on `window.onload`. It also locks up in any frameset with no LMS. The old `file://` browser test could not see this, because there the SCO is the top window and the loop body never runs. | A bounded walk (7 ancestors, the specification's limit), `try`/`catch` around every cross-origin property access, then the opener and the opener's ancestors. The no-LMS case is now a test. |
| 6 | Re-entry | `initializeSCORM()` set `cmi.core.lesson_status = "incomplete"` unconditionally on every launch. | A learner who passed and reopens the module has their recorded pass overwritten with `incomplete`. In a regulated training programme that is a destroyed training record. | The status is only initialised when the LMS reports `""` / `not attempted` / `unknown`. Asserted by relaunching a passed SCO against a persisted data model. |
| 7 | Session end | The assessment page never called `LMSFinish`. `finishSCORM` was wired to `onbeforeunload` on content pages only. | Several LMSs discard an attempt that is never terminated. | The attempt commits and terminates when the result is shown, and again on unload if the learner leaves early. No `SetValue` is accepted after termination. |
| 8 | 2004 score | No `cmi.score.scaled` anywhere. | 2004 rolls up on the normalised measure. A 2004 SCO that reports only `raw` cannot satisfy an objective or drive sequencing. | `cmi.score.scaled` in [0, 1], written alongside `raw`/`min`/`max` and asserted consistent with `raw`. |
| 9 | 2004 status model | Only `cmi.core.lesson_status` existed. | 2004 splits completion from success; a single lesson status has no meaning in it. | `cmi.completion_status` = `completed` and `cmi.success_status` = `passed`/`failed`. |
| 10 | `xsi:schemaLocation` | The `xsi` prefix was declared and then never used. | Cosmetic, but it is the one hint an LMS or validator has about which bindings the manifest claims. | Present, mapping every declared namespace. |
| 11 | Pass mark in the manifest | Not expressed at all. | The LMS had no way to apply the same pass mark the page applies. | `<adlcp:masteryscore>` for 1.2; an `imsss` primary objective with `satisfiedByMeasure` and `minNormalizedMeasure` for 2004 — and the run-time test asserts the score reported actually clears it. |
| 12 | Per-question evidence | The package reported a score and a status and nothing else. `cmi.interactions`, `cmi.suspend_data`, session time and exit were never written. | An auditor asking "which emergency-stop question did this operator get wrong, and what did they answer?" got a percentage. For a regulated buyer that is the difference between a training record and a number. | One `cmi.interactions` entry per question, in the version-appropriate data model, written after every hash resolves and before the commit that ends the attempt — plus session time, exit and a compact `cmi.suspend_data`. Detailed below. |

Two things the audit **cleared**: the SCORM 1.2 manifest was already valid
against the official 1.2 XSD (its defects were rows 3 and 10, neither of which
a schema can catch), and `identifierref` integrity, `<organizations default>`
and the manifest's position at the archive root were all already correct.

## What the harness proves

The manifest layer runs over the full matrix — both SCORM versions × both
assessment generators (`AssessmentGenerator`,
`MedicalDeviceAssessmentGenerator`) × both shipped SOP fixtures, eight real
packages built per run. The run-time layer drives one package per version for
the API-discovery and status checks, because what those exercise is
`scorm_api.js` and the page's call sequence, which do not vary with the
generator or the fixture. The `cmi.interactions` checks do run the full matrix
— both versions × both generators × both fixtures × two answering strategies
(always-first, all-correct) — because what they assert *is* per question: the
ids, types, weightings and per-question results have to match whatever the
generator produced.

### Manifest, both versions

- Validates against the official ADL/IMS XSDs for its version, offline.
- Bound to that version's namespaces, and to **none** of the other version's.
- `<schemaversion>` is the exact token the version defines.
- `adlcp:scormtype` (1.2) / `adlcp:scormType` (2004) is present, correctly
  cased, and is `sco` or `asset`; the wrongly-cased spelling appears nowhere.
- `<organizations default>` names a real organization; every identifier in the
  manifest is unique; every `item/@identifierref` resolves to a resource that
  has an `href` and is a SCO; every item has a non-empty title.
- Every `<file href>` and every resource `href` exists in the built directory
  **and** in the zip, and every file in the package except `imsmanifest.xml` is
  declared by some resource.
- Every page's `styles.css` and `scorm_api.js` are declared by the resource
  that launches it.
- `xsi:schemaLocation` maps every declared namespace.
- 1.2: the pass mark is an `<adlcp:masteryscore>` equal to the assessment's own
  passing score. 2004: an `imsss` primary objective whose
  `minNormalizedMeasure` equals it, plus `controlMode choice/flow` on the
  organization.
- The checker itself is tested against manifests carrying each historical
  defect, so a green result means the checks can still fail.

### Run-time, SCORM 1.2 (`API`, `cmi.core.*`)

Driven in Chromium, served over `http://127.0.0.1`, SCO two frames below the
window holding the API:

- `LMSInitialize("")` is called exactly once, is the first call, returns
  `"true"`, and precedes every `LMSSetValue`.
- Every argument crossing the API boundary is a JavaScript string.
- Failing attempt: `cmi.core.lesson_status` ends `"failed"`,
  `cmi.core.score.raw` equals the score the Python model predicts for a
  first-option learner, `min` is `"0"` and `max` is `"100"`.
- Passing attempt: ends `"passed"` — `setComplete()` does not downgrade it —
  with `raw` `"100"`; `LMSCommit` then `LMSFinish`, exactly one `LMSFinish`,
  and **no `LMSSetValue` after it**.
- Only `cmi.core.*` elements are written; no 2004 element is touched.
- No call is ever rejected by the LMS (`"false"` would mean a broken sequence).
- Relaunching a passed SCO against a persisted data model does not write
  `incomplete`.
- A content page's "Mark as Complete" sets `cmi.core.lesson_status =
  "completed"` and commits.
- Only `cmi.core.*`, `cmi.suspend_data` and `cmi.interactions.n.*` are written
  — the three places 1.2 puts a SCO's data. `cmi.core.session_time` is a
  `CMITimespan` and `cmi.core.exit` is `""`.

### Run-time, SCORM 2004 (`API_1484_11`, `cmi.*`)

The same properties, on the 2004 surface: `Initialize("")` once and first;
`cmi.score.raw`, `min`, `max`; `cmi.score.scaled` in [0, 1] and equal to
`raw/100`; `cmi.success_status` `passed`/`failed`; `cmi.completion_status`
`completed`; `Commit` then exactly one `Terminate` with nothing set after it;
no `cmi.core.*` element written; `scaled` clears the manifest's
`minNormalizedMeasure`; "Mark as Complete" sets `cmi.completion_status`.
`cmi.session_time` is an ISO 8601 duration and `cmi.exit` is `normal`.

### Per-question evidence (`cmi.interactions`)

This is the block a quality auditor reads. On submission — after every answer
hash has resolved, before the commit that closes the attempt, and never after
`LMSFinish`/`Terminate` — the page writes one interaction per question, with
index *n* matching the order the questions were presented in. If no API was
found the whole thing is skipped silently and the package still renders and
still scores.

**SCORM 1.2** (`cmi.interactions.n.*` — every element in this collection is
*write-only* in 1.2, and nothing in the package reads one back):

| Element | Value |
|---|---|
| `id` | the question id from the Python model, e.g. `step_mc_2` |
| `type` | `choice` for `multiple_choice` and `sequence`, `true-false` for `true_false` |
| `student_response` | `CMIFeedback`. Choice: the selected option's identifier, `a`/`b`/`c`/`d` **by rendered position**. True-false: `t` or `f`. Omitted entirely when the learner did not answer — `""` is not a legal value for either format |
| `result` | `correct`, `wrong`, or `neutral` when unanswered |
| `weighting` | the question's points, as a string |
| `latency` | `CMITimespan`, `HHHH:MM:SS.SS` |
| `time` | `CMITime`, `HH:MM:SS`, local |

Plus `cmi.core.session_time` (`CMITimespan`), `cmi.core.exit` (`""` — the 1.2
vocabulary for an ordinary end of session, as opposed to `suspend`, `logout` or
`time-out`) and `cmi.suspend_data`.

**SCORM 2004 4th Edition** (`cmi.interactions.n.*`):

| Element | Value |
|---|---|
| `id` | the question id, as above |
| `type` | `choice` / `true-false` |
| `learner_response` | Choice: the option identifier (`a`, `b`, …) by rendered position. True-false: the literal `true` or `false`. Omitted when unanswered |
| `result` | `correct`, `incorrect`, or `neutral` when unanswered |
| `weighting` | the question's points, as a string |
| `latency` | `timeinterval(second,10,2)` — an ISO 8601 duration, `PT0H1M23.45S` |
| `timestamp` | `time(second,10,0)` — ISO 8601, UTC, e.g. `2026-09-17T09:41:02Z` |
| `description` | the question text, whitespace-collapsed and clipped to 250 characters (the SPM for `localized_string_type`) |
| `objectives.0.id` | the question's `source_ref["kind"]` (`step`, `safety`, `sequence`, `purpose`, `definition`, `md_required`, …), or a slugged `topic` when there is no kind |

Plus `cmi.session_time` (ISO 8601 duration), `cmi.exit` = `normal` and
`cmi.suspend_data`.

**`cmi.suspend_data`**, in both versions, is compact JSON:

```json
{"attempt": 2, "responses": {"step_mc_2": "b", "safety_tf_1": "f"}}
```

`attempt` is read back from the previous `cmi.suspend_data` and incremented, so
a relaunch counts rather than pretending to be the first attempt. `responses`
holds the same token that was written as the response for that version, keyed by
question id; unanswered questions are absent. Both bindings cap the element at
4096 characters, so the page enforces the cap itself — responses are dropped
from the end until the JSON fits, rather than letting an LMS reject or silently
truncate the write.

The **latency is one figure for the whole attempt** — page load to submit —
repeated on every interaction, not a per-question dwell time. The page does not
time individual questions, and pretending otherwise in a training record would
be worse than being coarse about it.

**What is deliberately not written: `correct_responses`.**
`cmi.interactions.n.correct_responses.0.pattern` is the element that states the
expected answer. Writing it would mean the package carries the answer key into
the learner's browser and hands it to the network tab — precisely the defect
CLAUDE.md invariant 2 and GOAL.md criterion 3 exist to prevent. It is therefore
never written, in either version. The trade-off, stated plainly because a buyer
will ask: **an auditor can see which question each learner was asked, what they
answered, and whether it was judged correct — but not what the correct answer
was.** The correct answers live in the SME review record and in the transparency
report (`src/transparency_report.py`, the "LMS record" block says exactly this),
which are controlled documents on the customer's side, not content served to
learners. When server-side scoring lands (M2) the grader holds the key and can
report `correct_responses` from a place the learner cannot read.

Two more honest limits on this block:

- **The fake LMS accepts writes it does not validate.** It stores whatever
  string it is handed and answers `"true"`. A green run therefore proves *the
  package wrote this element with this value in this order*, not *a real LMS
  would accept it*. The formats above are the ones the specifications require —
  SCORM 1.2's `CMIFeedback` for `student_response` (a choice identifier, `t`/`f`
  for true-false), `CMITimespan` `HHHH:MM:SS.SS` and `CMITime` `HH:MM:SS`; SCORM
  2004's `choice` and `true-false` `learner_response` formats, ISO 8601 for
  `latency` and `timestamp` — and the run-time tests assert them with regular
  expressions rather than trusting the service. Real LMSs *do* enforce them, and
  several are stricter than the specification.
- **Nothing reads the interactions back.** In 1.2 that is not allowed; in 2004
  it would be, but the package has no reason to. So there is no test that an LMS
  stored what it was told, only that it was told.

### Both, and neither

- **No LMS present.** With no `API` and no `API_1484_11` anywhere, both
  packages still score, still render pass and fail, and raise no uncaught
  JavaScript error. (This is the case that used to hang the browser.)
- **The wrong LMS.** The wrapper is discovery-driven, not build-driven: the
  same `scorm_api.js` ships in both packages, so a 1.2 package dropped into a
  2004 LMS reports against the 2004 data model rather than silently reporting
  nothing, and vice versa. Both directions are tested.

## What this does **not** prove

Be blunt about this when talking to a customer.

- **No real LMS has imported these packages.** Not Moodle, not Canvas, not
  Cornerstone, SuccessFactors, TalentLMS or SCORM Cloud. The fake LMS is a
  microphone, not an implementation: it answers every legal call with `"true"`
  and enforces almost nothing. Real LMSs differ in how they handle re-entry,
  suspend data, rollup, and time limits, and several are stricter than the
  specification. GOAL.md criterion 4 stays open until packages have been
  imported into named platforms and the result recorded.
- **This is not ADL certification.** ADL's SCORM 2004 4th Edition Test Suite
  and the SCORM 1.2 Conformance Test Suite are Java applications that must be
  run interactively against a package and a SCO; they are not part of this
  harness, and nothing here entitles the package to the "SCORM conformant"
  mark. We use ADL's *schemas*, which is a much narrower thing.
- **Sequencing is declared, not exercised.** The 2004 manifest carries
  `controlMode` and a primary objective. Nothing here runs the IMS Simple
  Sequencing pseudo-code, rollup rules, or navigation requests; `adlnav` is
  declared but no `<adlnav:presentation>` or `hideLMSUI` is emitted, and no
  sequencing rule is asserted to behave.
- **SCORM 2004 3rd Edition is not emitted.** The exporter targets 4th Edition;
  `"2004 3rd Edition"` is accepted by the checker but never produced.
- **The data model is exercised narrowly.** Only the elements the pages write:
  status, success, completion, score, `cmi.interactions`, `cmi.suspend_data`,
  session time and exit. `cmi.location`/`cmi.core.lesson_location`,
  `cmi.comments`, `cmi.objectives` at the SCO level, `cmi.progress_measure`,
  `cmi.max_time_allowed` and the whole of `adl.nav` are not written and not
  tested — the package has no bookmarking, no resume and no navigation
  requests.
- **The interaction record has a hole in it, by design.**
  `cmi.interactions.n.correct_responses` is never written, because the package
  would have to carry the answer key through the learner's browser to write it.
  An auditor gets what was answered and whether it was right, not what the
  right answer was; see "Per-question evidence" above for why, and where the
  answers do live.
- **Scoring is still client-side.** The package computes its own score and
  tells the LMS the answer. Server-verified scoring is M2; the limitation is
  documented in `src/answer_key.py` and disclosed in the package's
  `metadata.json`.
- **The XSD layer validates the manifest, not the package.** Schemas cannot see
  whether the HTML works, whether the zip is well-formed, or whether the LMS
  can serve the files; the structural and run-time layers cover those, in that
  order.

## Adding a check

Manifest rules go in `tests/conformance/manifest_checks.py`, as a message that
names the element and what was expected — a conformance failure reading
"validation error" costs an afternoon. Add a matching negative test in
`test_manifest.py`: a check that cannot fail is not a check.

Run-time behaviour goes in `test_runtime.py`. The fake LMS in `fake_lms.py`
records `{fn, args, argTypes, ret}` for every call; assert on the recording,
not on the page's text, because the page's text is what the learner sees and
the recording is what the training record is made of.
