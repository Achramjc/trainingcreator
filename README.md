# Training Creator

A tool to convert Standard Operating Procedures (SOPs) and work instructions into training materials with assessments, exportable as SCORM packages for a Learning Management System (LMS).

**Available as both a web interface and a command-line tool.**

## Status

**Pre-release.** This is a working pipeline (parse -> generate -> assess -> export), not yet
a validated product. Generated content and assessment questions require Subject Matter Expert
(SME) and Quality review before use in a regulated training program — nothing here is a
substitute for that review. See [GOAL.md](GOAL.md) for the current known gaps, what's fixed,
and the roadmap to a compliance-grade product.

## Features

- **Web Interface**: Drag-and-drop upload, no coding required
- **Multi-format Input Support**: Parse SOPs from PDF, DOCX, TXT, and Markdown files
- **Automated Content Extraction**: Extracts procedures, steps, and key SOP sections via pattern matching
- **Training Content Generation**: Creates structured training modules with learning objectives
- **Assessment Generation**: Automatically generates verification questions and quizzes
- **SCORM Export**: Outputs SCORM 1.2 and SCORM 2004 4th Edition packages, each
  validated against the official ADL/IMS schemas and driven against a fake LMS in
  a real browser in CI ([docs/SCORM_CONFORMANCE.md](docs/SCORM_CONFORMANCE.md))

## Quick Start

### Web Interface (Recommended for Non-Technical Users)

```bash
# Install dependencies
pip install -r requirements.txt

# Start the web application
python app.py

# Open your browser to:
# http://localhost:5000
```

Simply drag and drop your SOP file, configure your preferences, and download your training package!

#### Try it with a sample

Don't have an SOP handy? The web interface includes a gallery of six sample SOPs spanning
different regulatory regimes (medical device, pharmaceutical, food safety, aerospace, clinical
laboratory, and general manufacturing) so you can generate a package and try the SME review
flow without uploading anything. Programmatically, `GET /api/samples` lists the catalog and
`POST /api/samples/<id>/generate` runs the same pipeline as `/api/upload` against a chosen
sample.

### Command Line Interface

#### Installation

```bash
pip install -r requirements.txt
```

### Basic Usage

```bash
# Convert a single SOP to training material
python -m src.cli --input examples/sample_sop.txt --output training_output

# Specify output format
python -m src.cli --input my_sop.pdf --output training_output --format scorm2004

# Generate with custom number of assessment questions
python -m src.cli --input work_instruction.docx --output training --questions 10
```

### Python API Usage

```python
from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator
from src.scorm_exporter import SCORMExporter

# Parse SOP
parser = SOPParser()
sop_content = parser.parse('path/to/sop.pdf')

# Generate training content
generator = TrainingGenerator()
training_module = generator.generate(sop_content)

# Create assessments
assessment_gen = AssessmentGenerator()
assessments = assessment_gen.generate(sop_content, num_questions=5)

# Export to SCORM
exporter = SCORMExporter()
exporter.create_package(training_module, assessments, 'output_folder')
```

## SME review and approval

Nothing should reach learners unreviewed. After generation the web app returns a signed
`review_url`: a page showing the source document (with line numbers) beside the generated
objectives, sections and questions, all editable. Saving an edit re-validates it and re-runs the
answer-position layout so an edited assessment keeps the same "a naive learner fails" guarantee as
a generated one. Approving records who approved, in what role, when, and how many edits were
made; the package's `metadata.json`, its pages (the DRAFT watermark becomes an approval banner)
and the transparency report all carry that record. See USAGE_GUIDE.md.

Security posture, threat model and what is *not* defended: `docs/SECURITY.md`.

## Optional grounded LLM layer (off by default)

`TRAINING_CREATOR_LLM=anthropic` (plus `ANTHROPIC_API_KEY` and `pip install -r
requirements-llm.txt`) enables Claude-assisted objectives, section summaries and distractors.
Every generated sentence must cite source lines and pass a grounding check (invented numbers,
added actions and low-overlap text are rejected and the deterministic content is kept); every
accepted and rejected item is written to `enhancement_report.json`. The check is a filter, not a
proof — SME approval stays mandatory. Details and limits: `docs/LLM.md`.

## Output Formats

- **SCORM 1.2**: the `API` run-time, `cmi.core.lesson_status` and
  `cmi.core.score.*`, pass mark as `<adlcp:masteryscore>`. The widest LMS support.
- **SCORM 2004 4th Edition**: the `API_1484_11` run-time, separate
  `cmi.completion_status` and `cmi.success_status`, `cmi.score.scaled`, and a pass
  mark expressed as an IMS Simple Sequencing primary objective. The manifest
  declares `controlMode choice/flow`; no sequencing *rules* are generated.
- **HTML Package**: A single-file, **read-only preview** of the training content
  and the assessment questions. It lists each question with its options but does
  **not** score answers, does not record completion and is not LMS-tracked — it
  is for review, not for delivering training. Use a SCORM package for anything a
  learner is meant to complete.
- **JSON**: Structured data for custom integrations. This export is for authors
  and subject-matter experts and **does contain the answer key** — do not hand it
  to learners.

## LMS Compatibility

Produces SCORM 1.2 and SCORM 2004 4th Edition packages. Each is a real package in
its own binding — different manifest namespaces, different `scormtype` spelling,
different `<schemaversion>` — and a single run-time wrapper that discovers whichever
API the LMS exposes and speaks that version's data model.

**What is tested, in CI, on every commit** (`tests/conformance/`):

- `imsmanifest.xml` validates against the **official ADL/IMS XSDs** for its version
  (vendored in the repo, validated offline), for both SCORM versions, both
  assessment generators and both sample SOPs.
- The Content Aggregation Model rules a schema cannot express: every
  `identifierref` resolves to a launchable SCO, every file the manifest declares
  is in the zip, every file in the zip is declared, and every page's `styles.css`
  and `scorm_api.js` are declared by the resource that launches it.
- Run-time behaviour in **headless Chromium against a recording fake LMS**, with
  the content loaded two frames below the window holding the API so the SCO has to
  find it the way it would in a real LMS. SCORM 1.2: `LMSInitialize` once and
  first, `cmi.core.lesson_status` `passed`/`failed` matching the score,
  `cmi.core.score.raw`/`min`/`max`, `LMSCommit` then `LMSFinish`, nothing set after
  the session ends. SCORM 2004: `Initialize`, `cmi.score.scaled` consistent with
  `cmi.score.raw`, `cmi.success_status`, `cmi.completion_status`, `Commit`,
  `Terminate`. Plus: a relaunch does not erase a recorded pass, and a package
  opened with no LMS present still scores and renders.

**What is not tested, and so is not claimed:** no package has been imported into
any real LMS — not Moodle, Canvas, Cornerstone, SuccessFactors, TalentLMS or SCORM
Cloud — and this is not ADL certification; ADL's own conformance test suites are
not part of the harness. Sequencing is declared in the 2004 manifest but its
behaviour is not exercised, and the packages report status and score only, not
`cmi.interactions`. Details and the full boundary: [docs/SCORM_CONFORMANCE.md](docs/SCORM_CONFORMANCE.md).

## Project Structure

```
trainingcreator/
├── src/
│   ├── parser.py          # Document parsing logic
│   ├── generator.py       # Training content generation
│   ├── assessments.py     # Assessment/quiz creation
│   ├── scorm_exporter.py  # SCORM package builder
│   └── cli.py            # Command-line interface
├── templates/             # HTML and XML templates
├── examples/             # Sample SOPs and outputs
└── tests/               # Unit tests
```

## Requirements

- Python 3.8+
- See requirements.txt for dependencies

## License

MIT License

## Contributing

Contributions welcome! Please open an issue or submit a pull request.
