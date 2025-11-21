"""
Web Application for Training Creator
Flask-based web interface for converting SOPs to training materials
"""

import os
import uuid
from pathlib import Path
from flask import Flask, request, render_template, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename
import json
from datetime import datetime

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator
from src.scorm_exporter import SCORMExporter

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['SECRET_KEY'] = 'dev-secret-key-change-in-production'

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'md', 'pdf', 'docx'}


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    """Serve the main application page"""
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handle file upload and initiate processing"""
    try:
        # Check if file is present
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': f'File type not supported. Allowed types: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

        # Get parameters
        num_questions = int(request.form.get('num_questions', 5))
        passing_score = int(request.form.get('passing_score', 70))
        scorm_version = request.form.get('scorm_version', '1.2')
        output_format = request.form.get('output_format', 'scorm')

        # Validate parameters
        if not 1 <= num_questions <= 20:
            return jsonify({'error': 'Number of questions must be between 1 and 20'}), 400
        if not 50 <= passing_score <= 100:
            return jsonify({'error': 'Passing score must be between 50 and 100'}), 400

        # Generate unique job ID
        job_id = str(uuid.uuid4())

        # Save uploaded file
        filename = secure_filename(file.filename)
        upload_path = Path(app.config['UPLOAD_FOLDER']) / job_id / filename
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        file.save(str(upload_path))

        # Process the file
        result = process_training(
            str(upload_path),
            job_id,
            num_questions,
            passing_score,
            scorm_version,
            output_format
        )

        return jsonify(result), 200

    except Exception as e:
        app.logger.error(f"Error processing upload: {str(e)}")
        return jsonify({'error': f'Processing failed: {str(e)}'}), 500


def process_training(file_path, job_id, num_questions, passing_score, scorm_version, output_format):
    """Process SOP and generate training package"""
    try:
        # Step 1: Parse SOP
        parser = SOPParser()
        sop_content = parser.parse(file_path)

        # Step 2: Generate training content
        generator = TrainingGenerator()
        training_module = generator.generate(sop_content)

        # Step 3: Generate assessment
        assessment_gen = AssessmentGenerator()
        assessment = assessment_gen.generate(
            sop_content,
            num_questions=num_questions,
            passing_score=passing_score
        )

        # Create output directory for this job
        output_dir = Path(app.config['OUTPUT_FOLDER']) / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Step 4: Export based on format
        if output_format == 'scorm':
            exporter = SCORMExporter(scorm_version=scorm_version)
            package_name = secure_filename(sop_content.title.replace(' ', '_')[:50])
            output_path = exporter.create_package(
                training_module,
                assessment,
                str(output_dir),
                package_name
            )
            download_filename = Path(output_path).name

        elif output_format == 'json':
            # Export as JSON
            json_data = {
                "sop_content": sop_content.to_dict(),
                "training_module": training_module.to_dict(),
                "assessment": assessment.to_dict(),
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "num_questions": num_questions,
                    "passing_score": passing_score
                }
            }
            download_filename = "training_data.json"
            output_path = output_dir / download_filename
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)

        elif output_format == 'html':
            # Export as standalone HTML
            download_filename = "training.html"
            output_path = output_dir / download_filename
            html_content = create_standalone_html(training_module, assessment)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

        # Return results
        return {
            'success': True,
            'job_id': job_id,
            'download_url': f'/api/download/{job_id}/{download_filename}',
            'metadata': {
                'title': sop_content.title,
                'version': sop_content.version,
                'num_procedures': len(sop_content.procedures),
                'num_safety_warnings': len(sop_content.safety_warnings),
                'num_questions': len(assessment.questions),
                'passing_score': passing_score,
                'estimated_duration': training_module.estimated_duration,
                'learning_objectives': len(training_module.learning_objectives)
            }
        }

    except Exception as e:
        app.logger.error(f"Error in process_training: {str(e)}")
        raise


@app.route('/api/download/<job_id>/<filename>')
def download_file(job_id, filename):
    """Download generated training package"""
    try:
        output_dir = Path(app.config['OUTPUT_FOLDER']) / job_id
        return send_from_directory(
            output_dir,
            filename,
            as_attachment=True
        )
    except Exception as e:
        return jsonify({'error': 'File not found'}), 404


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'version': '1.0.0'}), 200


def create_standalone_html(training_module, assessment):
    """Create a standalone HTML file with all content"""
    sections_html = ""
    for section in training_module.sections:
        sections_html += f"<div class='section'>{section.get('content', '')}</div>"

    questions_html = ""
    for idx, q in enumerate(assessment.questions, 1):
        questions_html += f"""
        <div class='question'>
            <p><strong>Question {idx}:</strong> {q.text}</p>
            <ul>
        """
        for option in q.options:
            questions_html += f"<li>{option}</li>"
        questions_html += "</ul></div>"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{training_module.title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        .section {{ margin: 30px 0; padding: 20px; background: #f8f9fa; border-radius: 5px; }}
        .question {{ margin: 20px 0; padding: 15px; background: #fff; border-left: 4px solid #3498db; }}
    </style>
</head>
<body>
    <h1>{training_module.title}</h1>
    {sections_html}
    <h2>Assessment</h2>
    {questions_html}
</body>
</html>"""
    return html


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
