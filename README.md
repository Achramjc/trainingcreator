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
- **SCORM Export**: Outputs SCORM 1.2/2004 packages

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

## Optional grounded LLM layer (off by default)

`TRAINING_CREATOR_LLM=anthropic` (plus `ANTHROPIC_API_KEY` and `pip install -r
requirements-llm.txt`) enables Claude-assisted objectives, section summaries and distractors.
Every generated sentence must cite source lines and pass a grounding check (invented numbers,
added actions and low-overlap text are rejected and the deterministic content is kept); every
accepted and rejected item is written to `enhancement_report.json`. The check is a filter, not a
proof — SME approval stays mandatory. Details and limits: `docs/LLM.md`.

## Output Formats

- **SCORM 1.2**: Maximum compatibility with older LMS platforms
- **SCORM 2004**: Modern SCORM standard with sequencing support
- **HTML Package**: A single-file, **read-only preview** of the training content
  and the assessment questions. It lists each question with its options but does
  **not** score answers, does not record completion and is not LMS-tracked — it
  is for review, not for delivering training. Use a SCORM package for anything a
  learner is meant to complete.
- **JSON**: Structured data for custom integrations. This export is for authors
  and subject-matter experts and **does contain the answer key** — do not hand it
  to learners.

## LMS Compatibility

Produces SCORM 1.2 and SCORM 2004 packages. Import into any SCORM-conformant LMS.
Automated conformance testing against the ADL test suite is on the roadmap (see
[GOAL.md](GOAL.md)) — no specific LMS platform has been verified against these packages yet.

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
