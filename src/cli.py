"""
Command Line Interface for Training Creator
"""

import click
import json
from pathlib import Path

from .parser import SOPParser
from .generator import TrainingGenerator
from .assessments import AssessmentGenerator
from .scorm_exporter import SCORMExporter


@click.command()
@click.option('--input', '-i', required=True, type=click.Path(exists=True),
              help='Path to SOP/work instruction file (PDF, DOCX, TXT, MD)')
@click.option('--output', '-o', required=True, type=click.Path(),
              help='Output directory for training package')
@click.option('--format', '-f', default='scorm1.2',
              type=click.Choice(['scorm1.2', 'scorm2004', 'html', 'json'], case_sensitive=False),
              help='Output format (default: scorm1.2)')
@click.option('--questions', '-q', default=5, type=int,
              help='Number of assessment questions to generate (default: 5)')
@click.option('--passing-score', '-p', default=70, type=int,
              help='Minimum passing score percentage (default: 70)')
@click.option('--package-name', '-n', default=None,
              help='Custom package name (default: based on SOP title)')
@click.option('--verbose', '-v', is_flag=True,
              help='Verbose output')
def main(input, output, format, questions, passing_score, package_name, verbose):
    """
    Training Creator - Convert SOPs and work instructions into LMS-ready training materials

    Example usage:
        training-creator -i sample_sop.pdf -o ./training_output

        training-creator -i work_instruction.docx -o ./output -f scorm2004 -q 10
    """
    try:
        click.echo("=" * 60)
        click.echo("Training Creator - SOP to Training Converter")
        click.echo("=" * 60)
        click.echo()

        # Step 1: Parse SOP
        click.echo(f"📄 Parsing SOP from: {input}")
        parser = SOPParser()
        sop_content = parser.parse(input)

        if verbose:
            click.echo(f"   Title: {sop_content.title}")
            click.echo(f"   Version: {sop_content.version}")
            click.echo(f"   Procedures: {len(sop_content.procedures)} steps")
            click.echo(f"   Safety Warnings: {len(sop_content.safety_warnings)}")
            click.echo(f"   Definitions: {len(sop_content.definitions)}")
        click.echo("   ✓ Parsing complete")
        click.echo()

        # Step 2: Generate training content
        click.echo("📚 Generating training content...")
        generator = TrainingGenerator()
        training_module = generator.generate(sop_content)

        if verbose:
            click.echo(f"   Learning Objectives: {len(training_module.learning_objectives)}")
            click.echo(f"   Training Sections: {len(training_module.sections)}")
            click.echo(f"   Estimated Duration: {training_module.estimated_duration} minutes")
        click.echo("   ✓ Training content generated")
        click.echo()

        # Step 3: Generate assessment
        click.echo(f"✏️  Generating assessment ({questions} questions)...")
        assessment_gen = AssessmentGenerator()
        assessment = assessment_gen.generate(sop_content, num_questions=questions,
                                            passing_score=passing_score)

        if verbose:
            click.echo(f"   Assessment Title: {assessment.title}")
            click.echo(f"   Questions Generated: {len(assessment.questions)}")
            click.echo(f"   Passing Score: {assessment.passing_score}%")
        click.echo("   ✓ Assessment generated")
        click.echo()

        # Step 4: Export to selected format
        output_path = Path(output)
        output_path.mkdir(parents=True, exist_ok=True)

        click.echo(f"📦 Exporting to {format.upper()} format...")

        if format.startswith('scorm'):
            version = "1.2" if format == "scorm1.2" else "2004"
            exporter = SCORMExporter(scorm_version=version)
            result_path = exporter.create_package(training_module, assessment,
                                                  str(output_path), package_name)
            click.echo(f"   ✓ SCORM package created: {result_path}")

        elif format == 'json':
            # Export as JSON
            json_data = {
                "sop_content": sop_content.to_dict(),
                "training_module": training_module.to_dict(),
                "assessment": assessment.to_dict()
            }
            json_path = output_path / f"{package_name or 'training'}.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            click.echo(f"   ✓ JSON data saved: {json_path}")

        elif format == 'html':
            # Export as standalone HTML (simplified)
            html_path = output_path / f"{package_name or 'training'}.html"
            html_content = _create_standalone_html(training_module, assessment)
            html_path.write_text(html_content, encoding='utf-8')
            click.echo(f"   ✓ HTML file created: {html_path}")

        click.echo()
        click.echo("=" * 60)
        click.echo("✅ Training creation complete!")
        click.echo("=" * 60)
        click.echo()
        click.echo("📋 Summary:")
        click.echo(f"   Input: {input}")
        click.echo(f"   Output: {output}")
        click.echo(f"   Format: {format.upper()}")
        click.echo(f"   Training Duration: ~{training_module.estimated_duration} minutes")
        click.echo(f"   Assessment Questions: {len(assessment.questions)}")
        click.echo(f"   Passing Score: {passing_score}%")
        click.echo()
        click.echo("🚀 Your training package is ready to upload to your LMS!")

    except FileNotFoundError as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        raise click.Abort()


def _create_standalone_html(training_module, assessment) -> str:
    """Create a standalone HTML file with all content"""
    sections_html = ""
    for section in training_module.sections:
        sections_html += f"<div class='section'>{section.get('content', '')}</div>"

    questions_html = ""
    for q in assessment.questions:
        questions_html += f"""
        <div class='question'>
            <p><strong>Q{assessment.questions.index(q) + 1}:</strong> {q.text}</p>
            <ul>
        """
        for idx, option in enumerate(q.options):
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
    main()
