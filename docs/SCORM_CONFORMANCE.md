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

Every row below was measured against the tree at `0e7e999`, not inferred.

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

Two things the audit **cleared**: the SCORM 1.2 manifest was already valid
against the official 1.2 XSD (its defects were rows 3 and 10, neither of which
a schema can catch), and `identifierref` integrity, `<organizations default>`
and the manifest's position at the archive root were all already correct.

## What the harness proves

The manifest layer runs over the full matrix — both SCORM versions × both
assessment generators (`AssessmentGenerator`,
`MedicalDeviceAssessmentGenerator`) × both shipped SOP fixtures, eight real
packages built per run. The run-time layer drives one package per version,
because what it exercises is `scorm_api.js` and the page's call sequence, which
do not vary with the generator or the fixture.

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

### Run-time, SCORM 2004 (`API_1484_11`, `cmi.*`)

The same properties, on the 2004 surface: `Initialize("")` once and first;
`cmi.score.raw`, `min`, `max`; `cmi.score.scaled` in [0, 1] and equal to
`raw/100`; `cmi.success_status` `passed`/`failed`; `cmi.completion_status`
`completed`; `Commit` then exactly one `Terminate` with nothing set after it;
no `cmi.core.*` element written; `scaled` clears the manifest's
`minNormalizedMeasure`; "Mark as Complete" sets `cmi.completion_status`.

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
  status, success, completion, score. `cmi.suspend_data`,
  `cmi.location`/`cmi.core.lesson_location`, `cmi.interactions`,
  `cmi.session_time` and `cmi.exit` are not written and not tested. A learner's
  answers are therefore not reported to the LMS as interactions — an auditor
  who wants per-question evidence will not find it in the LMS record.
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
