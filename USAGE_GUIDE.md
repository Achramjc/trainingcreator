# Training Creator - Complete Usage Guide

## Table of Contents
1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Input Formats](#input-formats)
4. [Output Formats](#output-formats)
5. [Advanced Usage](#advanced-usage)
6. [LMS Upload Instructions](#lms-upload-instructions)
7. [Customization](#customization)
8. [Troubleshooting](#troubleshooting)

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
- Maximum LMS compatibility
- Works with: Moodle, Blackboard, Canvas, TalentLMS
- Creates ZIP file ready to upload
- Tracks completion and scores

### SCORM 2004
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
