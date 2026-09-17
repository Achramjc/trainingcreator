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

## Output Formats

- **SCORM 1.2**: Maximum compatibility with older LMS platforms
- **SCORM 2004**: Modern SCORM standard with sequencing support
- **HTML Package**: Standalone HTML file, not LMS-tracked
- **JSON**: Structured data for custom integrations

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
