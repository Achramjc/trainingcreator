"""CLI tests using click.testing.CliRunner."""

from pathlib import Path

from click.testing import CliRunner

from src.cli import main


def test_cli_happy_path_scorm(tmp_path, sample_sop_path):
    runner = CliRunner()
    output_dir = tmp_path / "out"

    result = runner.invoke(main, [
        "--input", sample_sop_path,
        "--output", str(output_dir),
        "--format", "scorm1.2",
        "--questions", "5",
    ])

    assert result.exit_code == 0, result.output
    zips = list(output_dir.rglob("*.zip"))
    assert len(zips) == 1


def test_cli_happy_path_json(tmp_path, sample_sop_path):
    runner = CliRunner()
    output_dir = tmp_path / "out_json"

    result = runner.invoke(main, [
        "--input", sample_sop_path,
        "--output", str(output_dir),
        "--format", "json",
    ])

    assert result.exit_code == 0, result.output
    jsons = list(output_dir.glob("*.json"))
    assert len(jsons) == 1


def test_cli_bad_format_nonzero_exit(sample_sop_path, tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "--input", sample_sop_path,
        "--output", str(tmp_path / "out"),
        "--format", "xapi",
    ])

    assert result.exit_code != 0


def test_cli_missing_input_nonzero_exit(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "--input", str(tmp_path / "does_not_exist.txt"),
        "--output", str(tmp_path / "out"),
    ])

    assert result.exit_code != 0
