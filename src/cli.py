"""
Command Line Interface for Training Creator
"""

import html
import json
import os
from pathlib import Path

import click

from .audit import Actor, AuditLog, content_hash, file_hash, hmac_key_from_env
from .injection_scan import llm_skip_note, result_from_dict, sanitize_for_terminal
from .parser import SOPParser
from .generator import TrainingGenerator
from .assessments import MIN_ASSESSMENT_QUESTIONS, AssessmentGenerator
from .scorm_exporter import SCORMExporter
from .llm import LLMConfig, enhance_assessment, enhance_module, merge_reports
from .llm import build_provider as build_llm_provider

#: Written next to the package whenever the LLM layer runs.  It lists every
#: generated item, whether it was kept, and why - the artifact an SME reviews
#: and an auditor asks for.
ENHANCEMENT_REPORT_FILENAME = "enhancement_report.json"


@click.command()
@click.option('--input', '-i', required=True, type=click.Path(exists=True),
              help='Path to SOP/work instruction file (PDF, DOCX, TXT, MD)')
@click.option('--output', '-o', required=True, type=click.Path(),
              help='Output directory for training package')
@click.option('--format', '-f', default='scorm1.2',
              type=click.Choice(['scorm1.2', 'scorm2004', 'html', 'json'], case_sensitive=False),
              help='Output format (default: scorm1.2)')
@click.option('--questions', '-q', default=5,
              type=click.IntRange(min=MIN_ASSESSMENT_QUESTIONS),
              help='Number of assessment questions to generate '
                   f'(default: 5, minimum: {MIN_ASSESSMENT_QUESTIONS}). Shorter '
                   'assessments cannot spread their answers well enough to stop '
                   'a learner passing by clicking the same option every time.')
@click.option('--passing-score', '-p', default=70, type=int,
              help='Minimum passing score percentage (default: 70)')
@click.option('--package-name', '-n', default=None,
              help='Custom package name (default: based on SOP title)')
@click.option('--llm/--no-llm', 'llm', default=None,
              help='Run the optional grounded LLM enhancement layer between '
                   'generation and export. Default comes from the '
                   'TRAINING_CREATOR_LLM environment variable (off unless it '
                   'is set to "anthropic"). Every generated sentence is checked '
                   'against a cited source line; anything unsupported is '
                   'discarded and the deterministic text kept. Writes '
                   'enhancement_report.json next to the output. This is '
                   'acceleration, not approval: an SME must still sign off.')
@click.option('--verbose', '-v', is_flag=True,
              help='Verbose output')
def main(input, output, format, questions, passing_score, package_name, llm,
         verbose):
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

        # The output directory doubles as this job's audit-trail directory
        # (docs/AUDIT_TRAIL.md): `<output>/audit.jsonl`, job id = the
        # directory's own name, so `python3 -m src.audit verify <output>`
        # needs no --job-id to check a CLI-produced directory.
        output_path = Path(output)
        job_id = output_path.resolve().name
        audit_log = AuditLog.for_job(output_path, job_id, hmac_key_from_env())
        audit_log.append(
            'job.created', Actor(os.environ.get('USER', 'cli'), 'author', 'cli'),
            {'source_filename': Path(input).name, 'source_sha256': file_hash(input),
             'num_questions': questions, 'passing_score': passing_score,
             'format': format})

        # Step 1: Parse SOP
        click.echo(f"📄 Parsing SOP from: {input}")
        parser = SOPParser()
        sop_content = parser.parse(input)

        # Everything document-derived is sanitized before it is printed. A
        # document title can carry ANSI escape sequences, and a terminal will
        # obey them: "\x1b[2K\r" erases the line an operator is reading as
        # evidence and prints something else over it. See src/injection_scan.py
        # and docs/SECURITY.md.
        if verbose:
            click.echo(f"   Title: {sanitize_for_terminal(sop_content.title)}")
            click.echo(f"   Version: {sanitize_for_terminal(sop_content.version)}")
            click.echo(f"   Procedures: {len(sop_content.procedures)} steps")
            click.echo(f"   Safety Warnings: {len(sop_content.safety_warnings)}")
            click.echo(f"   Definitions: {len(sop_content.definitions)}")
        click.echo("   ✓ Parsing complete")

        # The input scan. Printed whenever it found anything, because the person
        # running the CLI is the only human in this path.
        scan_result = result_from_dict(getattr(sop_content, 'injection_scan', None))
        if not scan_result.clean:
            click.echo(f"   ⚠️  Input scan: {scan_result.risk.upper()} risk - "
                       f"{sanitize_for_terminal(scan_result.summary())}")
            for finding in scan_result.findings[:10]:
                click.echo(f"      line {finding.line}: {finding.kind}: "
                           f"{sanitize_for_terminal(finding.excerpt)}")
            if len(scan_result.findings) > 10:
                click.echo(f"      ... and {len(scan_result.findings) - 10} more")
            if scan_result.blocks_llm:
                click.echo("      This document contains text aimed at an "
                           "automated system. The LLM layer will not run on it; "
                           "deterministic generation continues. Read the flagged "
                           "lines before you release this training.")
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
            click.echo(f"   Assessment Title: {sanitize_for_terminal(assessment.title)}")
            click.echo(f"   Questions Generated: {len(assessment.questions)}")
            click.echo(f"   Passing Score: {assessment.passing_score}%")
        click.echo("   ✓ Assessment generated")
        click.echo()

        output_path.mkdir(parents=True, exist_ok=True)

        # Step 3b: Optional grounded LLM enhancement.
        #
        # Deliberately between generation and export, never inside either: the
        # deterministic pipeline must be able to run, and be tested, with this
        # step absent.  A failure here is reported, not raised.
        llm_config = LLMConfig.from_env()
        if llm is not None:
            llm_config = llm_config.with_enabled(llm)

        # The injection gate: a `high`-risk document is never sent to a model,
        # even with --llm, and there is no override in this build
        # (docs/SECURITY.md). The report still gets written, carrying the note
        # that says why nothing was enhanced.
        if llm_config.enabled and scan_result.blocks_llm:
            report = merge_reports("package", [], llm_config)
            report.note(llm_skip_note(scan_result))
            report_path = output_path / ENHANCEMENT_REPORT_FILENAME
            with open(report_path, 'w', encoding='utf-8') as handle:
                json.dump(report.to_dict(), handle, indent=2, ensure_ascii=False)
            click.echo("🤖 Grounded LLM enhancement SKIPPED - the input scan "
                       "flagged this document "
                       f"({', '.join(scan_result.high_kinds)}).")
            click.echo(f"   ✓ Enhancement report written: {report_path}")
            click.echo()
        elif llm_config.enabled:
            click.echo("🤖 Running grounded LLM enhancement "
                       f"(model {llm_config.model})...")
            provider = build_llm_provider(llm_config)
            training_module, module_report = enhance_module(
                training_module, sop_content, provider, llm_config)
            assessment, assessment_report = enhance_assessment(
                assessment, sop_content, provider, llm_config)
            report = merge_reports("package", [module_report, assessment_report],
                                   llm_config)
            report_path = output_path / ENHANCEMENT_REPORT_FILENAME
            with open(report_path, 'w', encoding='utf-8') as handle:
                json.dump(report.to_dict(), handle, indent=2, ensure_ascii=False)
            for line in report.summary_lines():
                click.echo(f"   {sanitize_for_terminal(line)}")
            click.echo(f"   ✓ Enhancement report written: {report_path}")
            click.echo("   ⚠️  Machine-assisted draft - a named SME must review "
                       "and approve before release.")
            click.echo()

        # `content_hash` covers exactly the module/assessment about to be
        # exported below (post-LLM-enhancement, if that ran) - the same rule
        # app.py's process_training follows.
        module_dict = training_module.to_dict()
        assessment_dict = assessment.to_dict()
        generated_hash = content_hash(module_dict, assessment_dict)
        audit_log.append(
            'content.generated', Actor(os.environ.get('USER', 'cli'), 'author', 'cli'),
            {'questions': len(assessment.questions)}, generated_hash)

        # Step 4: Export to selected format
        click.echo(f"📦 Exporting to {format.upper()} format...")

        if format.startswith('scorm'):
            version = "1.2" if format == "scorm1.2" else "2004"
            exporter = SCORMExporter(scorm_version=version)
            result_path = exporter.create_package(training_module, assessment,
                                                  str(output_path), package_name)
            click.echo(f"   ✓ SCORM package created: {result_path}")
            exported_path = Path(result_path)

        elif format == 'json':
            # Export as JSON
            json_data = {
                "sop_content": sop_content.to_dict(),
                "training_module": module_dict,
                "assessment": assessment_dict
            }
            json_path = output_path / f"{package_name or 'training'}.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            click.echo(f"   ✓ JSON data saved: {json_path}")
            exported_path = json_path

        elif format == 'html':
            # Export as standalone HTML (simplified)
            html_path = output_path / f"{package_name or 'training'}.html"
            html_content = _create_standalone_html(training_module, assessment)
            html_path.write_text(html_content, encoding='utf-8')
            click.echo(f"   ✓ HTML file created: {html_path}")
            exported_path = html_path

        audit_log.append(
            'package.exported', Actor('training-creator', 'application', 'system'),
            {'package_sha256': file_hash(exported_path), 'format': format,
             'filename': exported_path.name},
            generated_hash)

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
        click.echo(f"   Audit trail: {output_path / 'audit.jsonl'}")
        click.echo(f"   Audit head hash: {audit_log.head_hash()}")
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
    """Create a standalone HTML preview of the training content.

    READ-ONLY: this lists the questions and their options for review. It does no
    scoring and carries no answer key - use the SCORM package for anything a
    learner is meant to complete.

    Question text and options come from the source document, so everything
    interpolated here is HTML-escaped; section content is generated markup and is
    inserted as-is.
    """
    sections_html = ""
    for section in training_module.sections:
        sections_html += f"<div class='section'>{section.get('content', '')}</div>"

    questions_html = ""
    for number, q in enumerate(assessment.questions, 1):
        questions_html += f"""
        <div class='question'>
            <p><strong>Q{number}:</strong> {html.escape(str(q.text))}</p>
            <ul>
        """
        for option in q.options:
            questions_html += f"<li>{html.escape(str(option))}</li>"
        questions_html += "</ul></div>"

    page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{html.escape(str(training_module.title))}</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        .section {{ margin: 30px 0; padding: 20px; background: #f8f9fa; border-radius: 5px; }}
        .question {{ margin: 20px 0; padding: 15px; background: #fff; border-left: 4px solid #3498db; }}
    </style>
</head>
<body>
    <h1>{html.escape(str(training_module.title))}</h1>
    {sections_html}
    <h2>Assessment</h2>
    <p><em>Preview only - this page does not score answers.</em></p>
    {questions_html}
</body>
</html>"""
    return page


if __name__ == '__main__':
    main()
