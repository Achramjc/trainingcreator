# Training Creator

A powerful tool to convert Standard Operating Procedures (SOPs) and work instructions into interactive training materials with assessments, ready for upload to any Learning Management System (LMS).

**Available as both a user-friendly web interface and command-line tool!**

## Features

- **🌐 Web Interface**: No coding required! Beautiful drag-and-drop interface
- **Multi-format Input Support**: Parse SOPs from PDF, DOCX, TXT, and Markdown files
- **Intelligent Content Analysis**: Automatically extracts procedures, steps, and key information
- **Training Content Generation**: Creates structured training modules with learning objectives
- **Assessment Generation**: Automatically generates verification questions and quizzes
- **LMS-Ready Export**: Outputs SCORM 1.2/2004 packages compatible with major LMS platforms
- **Customizable Templates**: Modify training presentation and assessment styles

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
- **xAPI (Tin Can)**: For next-generation learning platforms
- **HTML Package**: Standalone HTML files for any web platform
- **JSON**: Structured data for custom integrations

## LMS Compatibility

Tested and compatible with:
- Moodle
- Canvas
- Blackboard
- TalentLMS
- Cornerstone OnDemand
- SAP SuccessFactors
- And any SCORM-compliant LMS

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
