# Training Creator - Complete Usage Guide

## Table of Contents
1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Input Formats](#input-formats)
4. [Output Formats](#output-formats)
5. [SME Review and Approval (Web App)](#sme-review-and-approval-web-app)
6. [Audit Trail](#audit-trail)
7. [Running a Pilot](#running-a-pilot)
8. [Advanced Usage](#advanced-usage)
9. [LMS Upload Instructions](#lms-upload-instructions)
10. [Customization](#customization)
11. [Troubleshooting](#troubleshooting)

## Installation

### Prerequisites
- Python 3.8 or higher
- pip (Python package manager)

### Installation Steps

```bash
# Clone or download the repository
cd trainingcreator

# Install dependencies
pip install -r requirements.txt

# Optional: Install in development mode
pip install -e .
```

## Quick Start

### Basic Command Line Usage

```bash
# Convert a text SOP to SCORM package
python -m src.cli --input examples/sample_sop.txt --output ./output

# Convert a PDF work instruction
python -m src.cli --input work_instruction.pdf --output ./training_packages

# Generate with custom number of questions
python -m src.cli -i sop.docx -o ./output -q 10 --passing-score 80
```

### Python API Usage

```python
from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator
from src.scorm_exporter import SCORMExporter

# Parse your SOP
parser = SOPParser()
sop = parser.parse('my_sop.pdf')

# Generate training content
generator = TrainingGenerator()
training = generator.generate(sop)

# Create assessment
assessment_gen = AssessmentGenerator()
assessment = assessment_gen.generate(sop, num_questions=8)

# Export to SCORM
exporter = SCORMExporter(scorm_version="1.2")
package_path = exporter.create_package(training, assessment, './output')

print(f"Training package created: {package_path}")
```

## Input Formats

### Supported File Types

1. **Plain Text (.txt)**
   - Simple text files with sections
   - Best for basic SOPs
   - Example: `sample_sop.txt`

2. **Markdown (.md)**
   - Structured documents with markdown formatting
   - Supports headers, lists, and emphasis
   - Good for well-formatted SOPs

3. **PDF (.pdf)**
   - Extracts text from PDF documents
   - Works best with text-based PDFs (not scanned images)
   - Requires `pdfplumber` library

4. **Word Documents (.docx)**
   - Microsoft Word documents
   - Preserves basic structure
   - Requires `python-docx` library

### SOP Structure Best Practices

For optimal results, structure your SOPs with:

```
Title: [Procedure Name]
Version: [Version Number]
Effective Date: [Date]

PURPOSE:
[Description of purpose]

SCOPE:
[When this applies]

DEFINITIONS:
Term: Definition
Another Term: Another Definition

SAFETY WARNINGS:
WARNING: [Safety information]
CAUTION: [Caution information]

PROCEDURE:
Step 1: [First step description]
Step 2: [Second step description]
  a. [Sub-step]
  b. [Sub-step]
Step 3: [Third step description]
```

## Output Formats

### SCORM 1.2 (Default)
```bash
python -m src.cli -i sop.txt -o ./output -f scorm1.2
```
- Broadest LMS compatibility of the two SCORM versions
- Creates ZIP file ready to upload to any SCORM 1.2-conformant LMS
- Tracks completion and scores

### SCORM 2004 (4th Edition)

Both package types are validated against ADL's official XSDs and driven in a fake LMS in CI on their own API surfaces (`API` for 1.2, `API_1484_11` for 2004); see `docs/SCORM_CONFORMANCE.md` for exactly what is and is not verified. No named LMS has been tested.
```bash
python -m src.cli -i sop.txt -o ./output -f scorm2004
```
- Modern SCORM standard
- Advanced sequencing features
- Better reporting capabilities
- May not work with older LMS platforms

### HTML Package
```bash
python -m src.cli -i sop.txt -o ./output -f html
```
- Standalone HTML file
- No SCORM wrapper
- Can be hosted on any web server
- No LMS tracking

### JSON Data
```bash
python -m src.cli -i sop.txt -o ./output -f json
```
- Structured data export
- For custom integrations
- Contains parsed SOP, training content, and assessments
- Easy to process programmatically

## SME Review and Approval (Web App)

### Try it with a sample

Before uploading your own SOP, the web app's gallery lets you try the whole flow - generation
through SME review - against one of six bundled sample SOPs, one per regulatory regime (medical
device, pharmaceutical, food safety, aerospace, clinical laboratory, and general manufacturing).
`GET /api/samples` returns the catalog; `POST /api/samples/<id>/generate` accepts the same form
fields as `/api/upload` (`num_questions`, `passing_score`, `scorm_version`, `output_format`) and
returns the same response shape, including a `review_url` you can open right away.

`app.py` (`python3 app.py`) generates a package immediately, exactly as the CLI
does - but nothing generated this way is ready for learners until a named
human reviews and approves it. Every `/api/upload` response now includes a
`review_url` alongside `download_url`:

```json
{
  "success": true,
  "job_id": "…",
  "download_url": "/api/download/<job_id>/<package>.zip?t=<token>",
  "review_url": "/review/<job_id>?t=<token>",
  "...": "..."
}
```

The `download_url` in that first response is still a **DRAFT** package,
watermarked as such on every page - useful for a quick look, but not for
publishing to an LMS.

### 1. Review

Open `review_url` in a browser. The left column shows the source SOP with
line numbers; the right column shows the generated module (title, learning
objectives, section content) and assessment (passing score, one block per
question with its options, correct-answer radio, and explanation). Where a
question was generated from a specific place in the SOP, a **Show source**
link scrolls the left column to and highlights those lines.

Everything on the right is editable in place:
- Add/remove learning objectives.
- Edit any section's HTML content directly.
- Edit question text, options (2-4 per question, no duplicates), which
  option is correct, and the explanation.

Click **Save edits** to submit `POST /api/review/<job_id>?t=<token>` with the
edited module and assessment. The server validates strictly (non-empty text,
2-4 options per question, a valid correct-answer index, no duplicate options,
at least 5 questions) and returns `400` with a precise message on failure. On
success it rebuilds the real content objects, regenerates the package and
transparency report, and reports `edits_count` - how many fields differ from
the *original, untouched* generation (not from the last save), so it stays a
meaningful measure of how much the model got wrong even across several
rounds of edits.

*Which* option is correct is SME content, and free to change; *where* that
option sits in the option list is layout the server always re-derives itself
(the same deterministic placement the generator uses - see CLAUDE.md
invariant #1), so an edit can never reopen "a naive learner can pass by
always clicking option 1" through the review UI. If the edited option
*text* itself would let a naive learner pass anyway - e.g. making the
correct answer dramatically longer than every distractor - saving is
refused with `400` naming the exploitable strategy, its score, and which
questions to fix; nothing is saved until it's addressed. The saved (and
returned) assessment reflects this server-chosen layout, which is why the
option order on screen can shift after a save even when you didn't touch
that question.

### 2. Approve

Once the content is right, an SME (or QA, or Training Manager - whoever your
process requires) fills in their name, role, and optional notes, and clicks
**Approve**. This calls `POST /api/approve/<job_id>?t=<token>` with
`{"approved_by", "role", "notes"}`; `approved_by` and `role` are required.

Approving:
- Writes an immutable `approval.json` record next to the job's other files:
  `{"approved_by", "role", "approved_at", "notes", "edits_count"}`.
- Regenerates the package with the DRAFT watermark on every page replaced by
  an "Approved by `<name>` (`<role>`) on `<date>`" banner, and `approval`
  embedded in `metadata.json` for the audit trail.
- Uses whatever content was last saved (edited or original) - approving after
  an edit approves the *edited* version.

A job can only be approved once; approving an already-approved job returns
`409`. Editing an approved job's content clears its approval status, since
the thing being reviewed has changed - it needs a fresh sign-off.

The review and approval endpoints reuse the same signed, expiring token as
downloads (`DOWNLOAD_TTL_SECONDS`), so a review link works for exactly as
long as a download link does.

## Audit Trail

Every job's actions - generation, edits, approval, export, download - can be recorded in a
tamper-evident, append-only log at `<output_folder>/<job_id>/audit.jsonl`, one JSON line per
action, each hash-chained to the one before it. The full design, exactly what it proves and what
it does not, lives in `docs/AUDIT_TRAIL.md`; this section is how to read it day to day.

### Where it lives

- **Per job:** `<output_folder>/<job_id>/audit.jsonl` - deleted with the job directory.
- **Global:** `<output_folder>/audit-global.jsonl` records `retention.deleted` events (a job's
  last head hash, kept after the job itself is swept), since that entry can't live in a file that
  is itself being deleted.
- Content produced before this feature existed, or by a code path that hasn't started writing to
  it yet, simply has no `audit.jsonl`. Every consumer of the trail (the transparency report, the
  review page, the pilot dashboard) treats that as "no audit trail" rather than an error.

### Reading the timeline

**In the review page** (`/review/<job_id>?t=<token>`): the "Audit trail" panel fetches
`GET /api/audit/<job_id>?t=<token>` and shows the verification status (intact / failed), the
verification mode, package-matches-approval, the head hash (with a copy button), and a table of
every recorded event - sequence number, timestamp, event, actor (name / role / source), a content
hash, and its details. A `404` there means no trail exists yet for that job.

**In the transparency report** (`transparency_report.html` / `.json`, under
`report["audit_trail"]` in the JSON): the same information, plus a one-line, per-event-type
summary of each entry's details (e.g. `package.exported` shows the format, SCORM version and
package hash prefix; `content.approved` shows who approved it and their role). A **failed
verification is shown as a red banner** at the top of the section - it is not something you have
to notice by reading a status field.

**In the pilot dashboard** (`/pilot`): jobs whose trail fails verification are counted
(`jobs_with_failed_audit`) and listed in a red alert box at the top of the page, and each job's
row in the jobs table carries a "verified" / "failed" / "no trail" badge.

### Verifying independently

You don't need the app running to check a trail - it's a plain file:

```bash
python3 -m src.audit verify outputs/<job_id>            # prints a VerificationResult as JSON; exit 1 if not ok
python3 -m src.audit verify outputs/<job_id> --key-env AUDIT_HMAC_KEY   # if an HMAC key is configured
python3 -m src.audit show   outputs/<job_id>             # one line per entry, head_hash on stderr
```

`ok: true` means the chain, MACs (if a key is configured) and the workflow rules (approval before
export, no edits after the last approval, etc.) all check out. Anything else is listed in
`problems`, each one naming exactly what's wrong.

### The head hash in `metadata.json`

`AuditLog.head_hash()` is the hash of the trail's last entry - one 64-character value that commits
to everything recorded up to that point. Because a chain that has had its *last* entries deleted
still verifies as intact (there's nothing left in the file to say they're missing -
`docs/AUDIT_TRAIL.md` explains why), an approved job's shipped package (SCORM's `metadata.json`,
the JSON export's `audit_head_hash` key, or the standalone HTML export's
`<meta name="audit-head-hash">` tag) carries a head hash anchor - but **at approval**, not at
export: it is the hash of the trail's `content.approved` entry itself, computed before that
approval's own export extends the chain further. The transparency report's own head hash
(`audit_trail.head_hash`) is a *different* point in the same trail - as of the export that
produced the report, one or more entries later - so it will not equal `metadata.json`'s
`audit_head_hash` even on a perfectly healthy job; don't compare those two to each other.

To confirm the package is the content that was approved: run `python3 -m src.audit show
<job_dir>` and check that the `content.approved` entry's own `hash` equals the package's
`audit_head_hash`. To confirm nothing was quietly dropped from the end of the trail since the
package shipped, compare either anchored value against `head_hash` from
`python3 -m src.audit verify` (or `show`) run on the job's *current* `audit.jsonl` - a value that
used to be a valid entry hash and no longer appears in `show`'s output means something in the
trail changed since that anchor was taken.

## Running a Pilot

GOAL.md's M1 exit criterion is **SMEs accept generated content with <30% edits across 20 real
SOPs from 3 pilot customers** - not something you can measure without real customers reviewing
real SOPs. This section is that pilot's instrumentation and protocol.

### What's measured

Every job already carries what a pilot needs, written into `job.json` as review happens:
`edits_count` (the flat total, unchanged), `edits_by_category` (how much of the edit landed in
`title`, `objectives`, `sections`, `questions`, `question_options`, or `question_answers`),
`edit_rounds` (how many times **Save edits** was clicked), and the timestamps
`review_opened_at`, `first_edit_at`, and `approved_at`. `src/pilot_metrics.py` turns a directory
of these into aggregates - see its module docstring for the exact **edit rate** definition (the
number to compare against the <30% target): the weighted ratio of edits made to the size of the
reviewable content, computed only over *approved* jobs.

### 1. Turn it on

Set `PILOT_METRICS_TOKEN` in the environment before starting `app.py`. This is off by default -
with no token configured, `/pilot` and `/pilot/metrics` both return `404` (not `403`), so a
default deployment never exposes SME edit data:

```bash
export PILOT_METRICS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 app.py
```

Keep this token as secret as a password - it reads every job's content and every reviewer's
approval notes across the whole output folder, not just one job.

### 2. Run the pilot

The recommended protocol, echoing GOAL.md's target ("20 real SOPs from 3 pilot customers"):

1. Pick 3-5 real, controlled SOPs from the customer(s) piloting the tool - the more
   representative of their actual document set, the more the result means.
2. Send the **same** SOPs to each of your 2-3 SMEs, so the per-source-document breakdown (see
   below) is actually comparing like with like.
3. Ask each SME to open their `review_url`, decide whether each objective, section, and question
   is right as generated, edit anything that isn't, and **Approve**.
4. Tell them not to discuss their review with the other SMEs before finishing - the point is to
   measure how the *generated* content lands with an independent expert, not the result of a
   group edit.
5. Do not coach anyone toward "fewer edits." An SME who would edit something should edit it;
   suppressing that would make the number meaningless.

### 3. Read the results

Visit `/pilot?token=<PILOT_METRICS_TOKEN>` for a dashboard (aggregates, edits-by-category,
a per-source-document table, and a per-job table linking to each job's review page), or
`GET /pilot/metrics?token=<PILOT_METRICS_TOKEN>` for the same data as JSON. From the command
line against the same `OUTPUT_FOLDER` the app is using:

```bash
python3 -m src.pilot_metrics ./outputs
python3 -m src.pilot_metrics ./outputs --json   # for scripting / a spreadsheet import
```

**What the numbers mean:**
- **Edit rate** (the headline number): the fraction of the reviewable content (objectives,
  sections, and questions' text/options/answers) that differed between what the model generated
  and what an SME actually approved, averaged across every approved job with the approved jobs
  weighted by how much content they had. **GOAL.md's M1 exit criterion is met when this is
  below 30%.**
- **Approval rate**: how many jobs opened for review ended up approved at all (a job an SME
  never approves is a stronger signal than a high edit rate on one they did).
- **Edits by category**: where the edits land - a pilot dominated by `sections` edits points at
  the generator's content fidelity; one dominated by `question_answers`/`question_options`
  points at distractor or blueprint quality; `title`/`objectives` edits are typically the
  cheapest to fix.
- **Per-source-document breakdown**: since the same SOP is meant to go to multiple SMEs, a wide
  spread in edit rate *for the same document* is itself informative - it may mean the SOP is
  ambiguous, or that SME judgment genuinely varies, more than it means the generator is wrong.
- **Median time to approve**: from job creation to approval - a rough proxy for how much of the
  "4-8 hours per SOP" GOAL.md cites this actually saves, once a real pilot has enough approved
  jobs to make a median meaningful.

Twenty SOPs and three SMEs is a small sample - treat single-digit-job numbers as directional,
not final, and prefer the per-category and per-source-document breakdowns over the single
headline percentage when deciding what to fix next.

## Advanced Usage

### Command Line Options

```bash
python -m src.cli \
  --input sop.pdf \              # Input file path
  --output ./packages \          # Output directory
  --format scorm1.2 \            # Output format
  --questions 10 \               # Number of assessment questions
  --passing-score 80 \           # Minimum passing percentage
  --package-name "Safety_Training" \  # Custom package name
  --verbose                      # Show detailed output
```

### Batch Processing

Create a script to process multiple SOPs:

```python
import os
from pathlib import Path
from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator
from src.scorm_exporter import SCORMExporter

# Directory containing SOPs
sop_dir = Path('./sops')
output_dir = Path('./training_packages')

parser = SOPParser()
generator = TrainingGenerator()
assessment_gen = AssessmentGenerator()
exporter = SCORMExporter()

# Process all SOPs
for sop_file in sop_dir.glob('*.txt'):
    print(f"Processing {sop_file.name}...")

    sop = parser.parse(str(sop_file))
    training = generator.generate(sop)
    assessment = assessment_gen.generate(sop)

    package_path = exporter.create_package(
        training, assessment, str(output_dir)
    )

    print(f"Created: {package_path}")
```

## LMS Upload Instructions

The steps below are each platform's standard SCORM package import flow, from their own
documentation — Training Creator has not been verified against these specific LMS platforms
(see the "LMS Compatibility" note in README.md and GOAL.md for current status). Always test an
imported package in your own LMS before rolling it out.

### Moodle

1. Log in to Moodle as instructor/admin
2. Navigate to your course
3. Turn editing on
4. Click "Add an activity or resource"
5. Select "SCORM package"
6. Upload the ZIP file
7. Configure settings and save

### Canvas

1. Go to your Canvas course
2. Click "Settings" → "Import Course Content"
3. Select "SCORM Package" as import type
4. Upload the ZIP file
5. Click "Import"

### Blackboard

1. Open your Blackboard course
2. Go to "Content"
3. Click "Build Content" → "SCORM Package"
4. Upload the ZIP file
5. Set availability and tracking options
6. Submit

### TalentLMS

1. Log in to TalentLMS admin
2. Go to "Courses" → Select your course
3. Click "Add Content" → "SCORM/TinCan"
4. Upload the ZIP file
5. Configure settings and save

## Customization

### Adjusting Assessment Difficulty

```python
# More questions = harder
assessment = assessment_gen.generate(sop, num_questions=15)

# Higher passing score
assessment.passing_score = 85

# Add time limit (minutes)
assessment.time_limit = 30
```

### Custom Styling

Edit the CSS in `src/scorm_exporter.py` to customize appearance:
- Colors and fonts
- Layout and spacing
- Button styles
- Question formatting

### Adding Custom Question Types

Extend the `AssessmentGenerator` class:

```python
class CustomAssessmentGenerator(AssessmentGenerator):
    def _generate_scenario_questions(self, sop_content):
        # Your custom question logic
        pass
```

## Troubleshooting

### Common Issues

**Issue: "Module not found" error**
```bash
# Solution: Install missing dependencies
pip install -r requirements.txt
```

**Issue: PDF parsing fails**
```bash
# Solution: Install PDF tools
pip install pdfplumber PyPDF2
```

**Issue: DOCX parsing fails**
```bash
# Solution: Install Word document support
pip install python-docx
```

**Issue: No questions generated**
- Check that your SOP has clear procedure steps
- Ensure sections are labeled (PURPOSE, SCOPE, etc.)
- Increase number of questions with `-q` option

**Issue: SCORM package doesn't work in LMS**
- Verify you're using SCORM 1.2 for maximum compatibility
- Check that the ZIP file isn't corrupted
- Try re-uploading the package
- Check LMS documentation for specific requirements

### Getting Help

1. Check this usage guide
2. Review example SOPs in `examples/` directory
3. Check README.md for additional information
4. Run with `--verbose` flag for detailed error messages

## Best Practices

1. **SOP Structure**: Use clear section headers (PURPOSE, SCOPE, PROCEDURE, etc.)
2. **Step Numbering**: Number procedure steps sequentially (1, 2, 3...)
3. **Safety Warnings**: Clearly mark with WARNING or CAUTION
4. **Testing**: Always test SCORM packages in your LMS before deploying
5. **Version Control**: Keep track of SOP versions in your training packages
6. **Regular Updates**: Update training when SOPs are revised

## Next Steps

- Try the example SOP: `python -m src.cli -i examples/sample_sop.txt -o ./test_output`
- Create training from your own SOPs
- Customize the templates and styling
- Integrate with your LMS workflow
- Automate batch processing for multiple SOPs
